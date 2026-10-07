"""The med-ask HTTP application."""

import os
from pathlib import Path

import psycopg
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.utils import safe_join


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
    app.config["FRONTEND_DIST"] = Path(
        os.environ.get(
            "FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "frontend/dist")
        )
    ).resolve()

    private_routes_enabled = os.environ.get("PRIVATE_ROUTES_ENABLED") == "true"

    @app.before_request
    def hide_private_routes():
        if not private_routes_enabled and request.path.startswith("/api/private/"):
            return jsonify(error="Unknown API route"), 404

    if private_routes_enabled:

        @app.get("/api/private/ping")
        def private_ping():
            return jsonify(private="ok")

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
