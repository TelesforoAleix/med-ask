from unittest.mock import MagicMock

import psycopg
import pytest

from med_ask import create_app


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.delenv("PRIVATE_ROUTES_ENABLED", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FRONTEND_DIST", str(tmp_path))
    app = create_app()
    app.config["TESTING"] = True
    return app


def test_health_without_database_configuration(app):
    response = app.test_client().get("/api/health")
    assert response.status_code == 503
    assert response.json == {"postgres": "unavailable", "vector": "unavailable"}


def test_health_with_unreachable_database(app, monkeypatch):
    app.config["DATABASE_URL"] = "postgresql://unused"

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("connection failed with confidential details")

    monkeypatch.setattr("med_ask.psycopg.connect", unavailable)
    response = app.test_client().get("/api/health")
    assert response.status_code == 503
    assert response.json == {"postgres": "unavailable", "vector": "unavailable"}
    assert b"confidential" not in response.data


@pytest.mark.parametrize("installed, status", [(True, 200), (False, 503)])
def test_health_checks_vector_extension(app, monkeypatch, installed, status):
    app.config["DATABASE_URL"] = "postgresql://unused"
    connect = MagicMock()
    cursor = connect.return_value.__enter__.return_value.cursor.return_value
    cursor.__enter__.return_value.fetchone.return_value = (installed,)
    monkeypatch.setattr("med_ask.psycopg.connect", connect)
    response = app.test_client().get("/api/health")
    assert response.status_code == status
    assert response.json == {
        "postgres": "ok",
        "vector": "ok" if installed else "missing",
    }
    connect.assert_called_once_with("postgresql://unused", connect_timeout=3)
    assert "pg_extension" in cursor.__enter__.return_value.execute.call_args.args[0]


def test_serves_built_page_and_client_routes(app):
    (app.config["FRONTEND_DIST"] / "index.html").write_text(
        "<!doctype html><title>med-ask</title><div id='root'></div>"
    )
    for path in ("/", "/search", "/search/example"):
        response = app.test_client().get(path)
        assert response.status_code == 200
        assert b"<title>med-ask</title>" in response.data


def test_serves_built_assets(app):
    assets = app.config["FRONTEND_DIST"] / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('med-ask')")
    response = app.test_client().get("/assets/app.js")
    assert response.status_code == 200
    assert b"console.log" in response.data


def test_missing_build_is_not_a_success(app):
    assert app.test_client().get("/").status_code == 404


@pytest.mark.parametrize("path", ["/api", "/api/unknown", "/api/health/extra"])
def test_unknown_api_routes_do_not_serve_react(app, path):
    (app.config["FRONTEND_DIST"] / "index.html").write_text("<title>med-ask</title>")
    response = app.test_client().get(path)
    assert response.status_code == 404
    assert response.is_json


def test_static_path_cannot_escape_build_directory(app):
    response = app.test_client().get("/../pyproject.toml")
    assert response.status_code == 404


@pytest.mark.parametrize("setting", ["true", "false", None, "", "TRUE", "1", "invalid"])
def test_private_boundary_and_shared_routes(app, monkeypatch, setting):
    if setting is not None:
        monkeypatch.setenv("PRIVATE_ROUTES_ENABLED", setting)
    instance = create_app()
    instance.config["TESTING"] = True
    instance.config["DATABASE_URL"] = "postgresql://unused"
    connect = MagicMock()
    cursor = connect.return_value.__enter__.return_value.cursor.return_value
    cursor.__enter__.return_value.fetchone.return_value = (True,)
    monkeypatch.setattr("med_ask.psycopg.connect", connect)
    (instance.config["FRONTEND_DIST"] / "index.html").write_text(
        "<title>med-ask</title>"
    )
    client = instance.test_client()
    assert client.get("/").status_code == 200
    assert b"<title>med-ask</title>" in client.get("/").data
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").json == {"postgres": "ok", "vector": "ok"}
    unknown = client.get("/api/unknown")
    ping = client.get("/api/private/ping")
    if setting == "true":
        assert ping.status_code == 200
        assert ping.json == {"private": "ok"}
    else:
        for path in (
            "/api/private/ping",
            "/api/private/",
            "/api/private/anything/deeper",
        ):
            for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
                response = client.open(path, method=method)
                assert response.status_code == unknown.status_code == 404
                assert response.content_type == unknown.content_type
                if method != "HEAD":
                    assert response.data == unknown.data
