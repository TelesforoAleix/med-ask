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


class QuestionMemory:
    def __init__(self):
        self.rows = {}
        self.url = "postgresql://unused"

    def ensure(self):
        pass

    def model(self, table):
        return None

    def begin_question(self, question, asker):
        from uuid import uuid4

        identity = str(uuid4())
        self.rows[identity] = dict(question=question, asker=asker)
        return identity

    def finish_question(self, identity, table, evidence):
        self.rows[identity].update(table=table, evidence=evidence)

    def feedback(self, identity, asker, thumbs, comment):
        row = self.rows.get(identity)
        if row is None or row["asker"] != asker:
            return False
        row.update(thumbs=thumbs, comment=comment)
        return True


@pytest.mark.parametrize(
    "private,expected", [("true", "tailnet"), ("false", "synthetic@example.invalid")]
)
def test_search_logs_identity_and_no_index(monkeypatch, private, expected):
    from med_ask.embedding import Embedded

    monkeypatch.setenv("PRIVATE_ROUTES_ENABLED", private)
    instance = create_app()
    database = QuestionMemory()
    endpoint = MagicMock()
    endpoint.request.return_value = Embedded("synthetic-v1", [[1.0, 2.0]], 0.001)
    instance.config.update(QUESTION_DATABASE=database, EMBEDDING_ENDPOINT=endpoint)
    response = instance.test_client().post(
        "/api/search",
        json={"question": "Synthetic question?"},
        headers={"Cf-Access-Authenticated-User-Email": "synthetic@example.invalid"},
    )
    assert response.status_code == 409
    assert "No index yet" in response.json["error"]
    row = database.rows[response.json["question_id"]]
    assert row["asker"] == expected
    assert row["question"] == "Synthetic question?"
    assert row["evidence"] == [] and row["table"]


def test_public_requires_access_identity(app):
    response = app.test_client().post(
        "/api/search", json={"question": "Synthetic question?"}
    )
    assert response.status_code == 401
    assert "Sign in" in response.json["error"]


def test_feedback_belongs_to_existing_question(app):
    from uuid import uuid4

    database = QuestionMemory()
    identity = database.begin_question(
        "Synthetic question?", "synthetic@example.invalid"
    )
    app.config["QUESTION_DATABASE"] = database
    client = app.test_client()
    body = dict(question_id=identity, thumbs="down", comment="Synthetic test feedback")
    headers = {"Cf-Access-Authenticated-User-Email": "other@example.invalid"}
    assert client.post("/api/feedback", json=body, headers=headers).status_code == 404
    headers["Cf-Access-Authenticated-User-Email"] = "synthetic@example.invalid"
    assert client.post("/api/feedback", json=body, headers=headers).json == {
        "saved": True
    }
    assert database.rows[identity]["thumbs"] == "down"
    assert database.rows[identity]["comment"] == "Synthetic test feedback"
    body["question_id"] = str(uuid4())
    assert client.post("/api/feedback", json=body, headers=headers).status_code == 404
    body["question_id"] = "invalid"
    assert client.post("/api/feedback", json=body, headers=headers).status_code == 400
    body.update(question_id=identity, thumbs="sideways")
    assert client.post("/api/feedback", json=body, headers=headers).status_code == 400
    body.update(thumbs="up", comment="x" * 4001)
    assert client.post("/api/feedback", json=body, headers=headers).status_code == 400


def test_page_render_validates_book_and_page(app, tmp_path):
    from io import BytesIO

    import pymupdf
    from PIL import Image

    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Synthetic page for the HTTP render test.")
        pdf.save(path)
    (tmp_path / "books.toml").write_text(
        '[[books]]\nid="synthetic"\nfilename="synthetic.pdf"\ntitle="Test"\nlanguage="English"'
    )
    app.config["SOURCES_DIR"] = tmp_path
    client = app.test_client()
    for url in (
        "/api/page/unknown/1",
        "/api/page/synthetic/0",
        "/api/page/synthetic/2",
        "/api/page/../1",
        "/api/page/%2e%2e%2fsynthetic/1",
        "/api/page/synthetic/-1",
        "/api/page/synthetic/not-a-number",
    ):
        assert client.get(url).status_code == 404
    response = client.get("/api/page/synthetic/1")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, max-age=3600"
    with Image.open(BytesIO(response.data)) as image:
        assert 999 <= image.width <= 1001
        assert image.height > image.width
    # Decoding the generated image is a test, not a human page view.


def test_successful_search_logs_labels_and_scores(app, monkeypatch):
    from med_ask.retrieval import Evidence

    database = QuestionMemory()
    app.config["QUESTION_DATABASE"] = database
    app.config["EMBEDDING_ENDPOINT"] = MagicMock()
    evidence = Evidence(
        "synthetic-id",
        "Synthetic source sentence.",
        "synthetic",
        "Synthetic title",
        "English",
        (1, 1),
        None,
        "no margin number",
        False,
        (),
        0,
        "pdf page 1 <print page unknown: no margin number>",
        0.5,
    )
    monkeypatch.setattr(
        "med_ask.search", lambda *args: ([evidence], "synthetic_table", 0.01)
    )
    response = app.test_client().post(
        "/api/search",
        json={"question": "Synthetic question?"},
        headers={"Cf-Access-Authenticated-User-Email": "synthetic@example.invalid"},
    )
    assert response.status_code == 200
    assert response.json["evidence"][0]["text"] == evidence.text
    assert response.json["embedding_seconds"] == 0.01
    row = database.rows[response.json["question_id"]]
    assert row["evidence"] == [dict(id=evidence.id, score=0.5, label=evidence.label)]


@pytest.mark.parametrize("question", [None, "", "   ", "x" * 4001, 23, []])
def test_search_validates_question_before_logging(app, question):
    app.config["QUESTION_DATABASE"] = QuestionMemory()
    response = app.test_client().post(
        "/api/search",
        json={"question": question},
        headers={"Cf-Access-Authenticated-User-Email": "synthetic@example.invalid"},
    )
    assert response.status_code == 400
    assert app.config["QUESTION_DATABASE"].rows == {}
