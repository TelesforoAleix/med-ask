"""The med-ask HTTP application."""

import os
from io import BytesIO
from pathlib import Path
from time import perf_counter
from uuid import UUID

import psycopg
import pymupdf
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.utils import safe_join

from med_ask.database import Database
from med_ask.embedding import Endpoint
from med_ask.generation import (
    GenerationEndpoint,
    detect_language,
    generate_answer,
    grade_candidates,
    group_evidence,
    translate_passage,
)
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
        if isinstance(question, str):
            question = question.replace("\x00", "").strip()
        if not isinstance(question, str) or not 1 <= len(question) <= 4000:
            return jsonify(error="Enter a question of 1–4000 characters"), 400
        question = question.strip()
        started = perf_counter()
        identity = None
        try:
            db = database()
            language = detect_language(question)
            identity = db.begin_question(question, who, language)
            endpoint = app.config.get("EMBEDDING_ENDPOINT") or Endpoint()
            candidates, table, embedding_seconds = search(question, endpoint, db, k=30)
            generation = app.config.get("GENERATION_ENDPOINT") or GenerationEndpoint()
            flags, grading_seconds = grade_candidates(question, candidates, generation)
            groups = group_evidence(candidates, flags, language)
            evidence = [item for group in groups for item in group["evidence"]]
            ungraded = flags.count(None)
            db.finish_question(
                identity,
                table,
                evidence,
                [
                    dict(id=e.id, score=e.score, label=e.label, grade=flag)
                    for e, flag in zip(candidates, flags, strict=True)
                ],
                ungraded,
            )
            return jsonify(
                question_id=identity,
                language=language,
                evidence=evidence,
                groups=groups,
                not_found=not evidence,
                ungraded_count=ungraded,
                embedding_seconds=embedding_seconds,
                grading_seconds=grading_seconds,
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

    def owned_question(db, who):
        body = request.get_json(silent=True)
        identity = body.get("question_id") if isinstance(body, dict) else None
        try:
            UUID(identity)
        except (ValueError, TypeError, AttributeError):
            abort(400, description="Invalid question id")
        row = db.question(identity, who)
        if row is None:
            abort(404)
        passing_ids = {
            grade["id"] for grade in row["grades"] if grade.get("grade") is True
        }
        row["evidence"] = [
            item for item in row["evidence"] if item["id"] in passing_ids
        ]
        return identity, row, body

    @app.post("/api/answer")
    def answer_route():
        who = asker()
        try:
            db = database()
            identity, row, _ = owned_question(db, who)
            if not row["evidence"]:
                return jsonify(error="No passing evidence for an answer"), 409
            started = perf_counter()
            text = row["answer"]
            if text is None:
                endpoint = app.config.get("GENERATION_ENDPOINT") or GenerationEndpoint()
                text = generate_answer(
                    row["question"], row["language"], row["evidence"], endpoint
                )
                db.save_answer(identity, who, text)
            return jsonify(
                answer=text, generated=True, answer_seconds=perf_counter() - started
            )
        except (psycopg.Error, ValueError, KeyError, TypeError):
            return jsonify(
                error="A supported, cited answer could not be generated"
            ), 503
        except Exception as error:
            if isinstance(error, HTTPException):
                raise
            return jsonify(error="Answer is temporarily unavailable"), 503

    @app.post("/api/translate")
    def translate_route():
        who = asker()
        try:
            db = database()
            _, row, body = owned_question(db, who)
            passage = next(
                (
                    item
                    for item in row["evidence"]
                    if item["id"] == body.get("passage_id")
                ),
                None,
            )
            if passage is None:
                abort(404)
            if not passage["translation_available"]:
                return jsonify(
                    error="This passage is already in the question's language"
                ), 400
            # Look up the cache before constructing the endpoint.
            cached = db.translation(passage["id"], row["language"])
            if cached is not None:
                return jsonify(translation=cached, generated=True, cached=True)
            endpoint = app.config.get("GENERATION_ENDPOINT") or GenerationEndpoint()
            text, hit = translate_passage(passage, row["language"], db, endpoint)
            return jsonify(translation=text, generated=True, cached=hit)
        except (psycopg.Error, ValueError, KeyError, TypeError):
            return jsonify(error="Translation is temporarily unavailable"), 503
        except Exception as error:
            if isinstance(error, HTTPException):
                raise
            return jsonify(error="Translation is temporarily unavailable"), 503

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
        if isinstance(comment, str):
            comment = comment.replace("\x00", "")
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
