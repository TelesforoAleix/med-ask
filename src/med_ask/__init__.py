"""The med-ask HTTP application."""

import os
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from time import perf_counter
from uuid import UUID

import psycopg
import pymupdf
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import safe_join

from med_ask.database import Database
from med_ask.embedding import Endpoint
from med_ask.manifest import load_manifest
from med_ask.retrieval import NoIndex, search


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
    app.config["FRONTEND_DIST"] = Path(
        os.environ.get(
            "FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend/dist")
        )
    ).resolve()

    app.config["SOURCES_DIR"] = Path(os.environ.get("SOURCES_DIR", "/data/sources"))
    app.config["MAX_CONTENT_LENGTH"] = 32_000

    private_routes_enabled = os.environ.get("PRIVATE_ROUTES_ENABLED") == "true"

    @app.before_request
    def hide_private_routes():
        if not private_routes_enabled and request.path.startswith("/api/private/"):
            return jsonify(error="Unknown API route"), 404

    if private_routes_enabled:

        @app.get("/api/private/ping")
        def private_ping():
            return jsonify(private="ok")

    def asker():
        if private_routes_enabled:
            return "tailnet"
        email = request.headers.get("Cf-Access-Authenticated-User-Email", "").strip()
        if not email:
            abort(401, description="Sign in through Access to search or give feedback")
        return email

    def database():
        instance = app.config.get("QUESTION_DATABASE") or Database(
            app.config["DATABASE_URL"]
        )
        instance.ensure()
        return instance

    @app.errorhandler(401)
    @app.errorhandler(400)
    @app.errorhandler(413)
    def invalid_request(error):
        return jsonify(error=error.description), error.code

    @app.post("/api/search")
    def search_route():
        who = asker()
        body = request.get_json(silent=True)
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 4000:
            return jsonify(error="Enter a question of 1–4000 characters"), 400
        question = question.strip()
        started = perf_counter()
        identity = None
        try:
            db = database()
            identity = db.begin_question(question, who)
            endpoint = app.config.get("EMBEDDING_ENDPOINT") or Endpoint()
            evidence, table, embedding_seconds = search(question, endpoint, db)
            db.finish_question(
                identity,
                table,
                [dict(id=e.id, score=e.score, label=e.label) for e in evidence],
            )
            return jsonify(
                question_id=identity,
                evidence=[asdict(e) for e in evidence],
                embedding_seconds=embedding_seconds,
                search_seconds=perf_counter() - started,
            )
        except NoIndex as error:
            db.finish_question(identity, error.table, [])
            return jsonify(error=str(error), question_id=identity), 409
        except Exception:
            # Library exceptions may contain credentials or original passages.
            return jsonify(
                error="Search is temporarily unavailable. Please try again.",
                question_id=identity,
            ), 503

    @app.post("/api/feedback")
    def feedback_route():
        who = asker()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Supply a question id and feedback"), 400
        identity, thumbs, comment = (
            body.get(k) for k in ("question_id", "thumbs", "comment")
        )
        try:
            UUID(identity)
        except (ValueError, TypeError, AttributeError):
            return jsonify(error="Invalid question id"), 400
        if thumbs not in ("up", "down", None):
            return jsonify(error="Thumbs must be up, down, or null"), 400
        if comment is not None and (
            not isinstance(comment, str) or len(comment) > 4000
        ):
            return jsonify(error="Comment must be at most 4000 characters"), 400
        try:
            if not database().feedback(identity, who, thumbs, comment):
                return jsonify(error="Question not found"), 404
        except psycopg.Error:
            return jsonify(error="Feedback is temporarily unavailable"), 503
        return jsonify(saved=True)

    @app.get("/api/page/<book_id>/<int:page_number>")
    def page_route(book_id, page_number):
        try:
            books = load_manifest(app.config["SOURCES_DIR"])
            book = books.get(book_id)
            if book is None:
                abort(404)
            with pymupdf.open(book.path) as pdf:
                if not 1 <= page_number <= len(pdf):
                    abort(404)
                page = pdf[page_number - 1]
                image = page.get_pixmap(
                    matrix=pymupdf.Matrix(
                        1000 / page.rect.width, 1000 / page.rect.width
                    ),
                    alpha=False,
                ).tobytes("png")
            response = send_file(BytesIO(image), mimetype="image/png", max_age=3600)
            response.headers["Cache-Control"] = "private, max-age=3600"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except (OSError, ValueError, KeyError):
            abort(404)

    @app.get("/api/health")
    def health():
        result = {"postgres": "unavailable", "vector": "unavailable"}
        if not app.config["DATABASE_URL"]:
            return jsonify(result), 503
        try:
            with psycopg.connect(
                app.config["DATABASE_URL"], connect_timeout=3
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_extension "
                        "WHERE extname = 'vector')"
                    )
                    installed = cursor.fetchone()[0]
            result = {"postgres": "ok", "vector": "ok" if installed else "missing"}
        except psycopg.Error:
            # Connection errors can contain credentials; never return or log them.
            return jsonify(result), 503
        return jsonify(result), 200 if installed else 503

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def frontend(path):
        if path == "api" or path.startswith("api/"):
            return jsonify(error="Unknown API route"), 404
        directory = app.config["FRONTEND_DIST"]
        requested = safe_join(str(directory), path)
        if requested is None:
            abort(404)
        if path and Path(requested).is_file():
            return send_from_directory(directory, path)
        return send_from_directory(directory, "index.html")

    return app
