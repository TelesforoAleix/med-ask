"""Synthetic private labels, atomic saves and logged candidate recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pymupdf
import pytest

from med_ask import create_app, labels
from med_ask.evaluation import EvalError, load_eval, run_real_eval, score_results
from med_ask.manifest import Book

FIRST = str(UUID(int=0x123456789ABCDEF00000000000000001))
SECOND = str(UUID(int=0xABCDEF12345678900000000000000002))
BASE = "/api/private/label"


def logged(identity=FIRST):
    return dict(
        id=identity,
        question="Synthetic labelling question?",
        language="en",
        asked_at="2026-10-16T12:00:00+00:00",
        answer="Synthetic generated answer.",
        evidence=[],
        vector_table="e_v6_synthetic",
        grades=[
            dict(id="a", grade=False),
            dict(id="b", grade=True),
            dict(id="missing", grade=None),
        ],
    )


@pytest.fixture
def owner(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        for _ in range(5):
            pdf.new_page()
        pdf.save(path)
    book = Book("bio", path.name, "Synthetic book", "English", path, "bio-1-en")
    books = {book.id: book}
    monkeypatch.setenv("PRIVATE_ROUTES_ENABLED", "true")
    monkeypatch.setattr(labels, "load_manifest", lambda _: books)
    monkeypatch.setattr(
        labels, "logged_questions", lambda _: [logged(), logged(SECOND)]
    )
    monkeypatch.setattr(
        labels,
        "recover_candidates",
        lambda *_: [
            dict(id="a", rank=1, grade=False, book_id="bio", pdf_pages=[1, 2]),
            dict(id="b", rank=2, grade=True, book_id="bio", pdf_pages=[2, 3]),
            dict(id="missing", rank=3, grade=None),
        ],
    )
    monkeypatch.setattr(
        labels,
        "search",
        lambda *a, **kw: (
            [SimpleNamespace(book_id="bio", pdf_pages=[3, 4])],
            "e_v6_synthetic",
            0,
        ),
    )
    app = create_app()
    app.config.update(
        TESTING=True,
        REAL_EVAL_FILE=tmp_path / "eval" / "real.jsonl",
        EMBEDDING_ENDPOINT=object(),
    )
    return app, books


def route(suffix=""):
    return BASE + "/question/" + FIRST + suffix


@pytest.mark.parametrize("setting", ["false", "", "TRUE"])
def test_all_label_routes_hidden_on_public(monkeypatch, setting):
    monkeypatch.setenv("PRIVATE_ROUTES_ENABLED", setting)
    app = create_app()
    assert not any(r.rule.startswith(BASE) for r in app.url_map.iter_rules())
    client = app.test_client()
    for path in [
        BASE + "/questions",
        route(),
        route("/suggestions"),
        route("/label"),
        BASE + "/unknown",
    ]:
        for method in ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]:
            assert client.open(path, method=method).status_code == 404


def test_guards_and_filters(owner):
    app, _ = owner
    client = app.test_client()
    response = client.get(BASE + "/questions")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.json["questions"]) == 2
    assert all(r["label"] == "unlabelled" for r in response.json["questions"])
    assert not client.get(BASE + "/questions?from=2026-10-17").json["questions"]
    assert not client.get(BASE + "/questions?to=2026-10-15").json["questions"]
    assert (
        len(
            client.get(BASE + "/questions?from=2026-10-16&to=2026-10-16").json[
                "questions"
            ]
        )
        == 2
    )
    assert client.get(BASE + "/questions?from=bad").status_code == 400
    assert client.get(BASE + "/question/bad").status_code == 404
    assert (
        client.post(
            route("/label"),
            json={"action": "outside"},
            headers={"Origin": "https://example.org"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            route("/label"), data="x" * 32001, content_type="application/json"
        ).status_code
        == 413
    )
    assert client.post(route("/label"), json=[]).status_code == 400
    assert not app.config["REAL_EVAL_FILE"].exists()


def test_mapping_page_actions_and_clear(owner):
    app, books = owner
    client = app.test_client()
    path = app.config["REAL_EVAL_FILE"]

    def act(action, **fields):
        response = client.post(
            route("/label"),
            json=dict(action=action, **fields),
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        return response.json

    assert act("outside")["label"] == "out of the sources"
    saved = labels.read_labels(path)[0]
    assert saved["expect_not_found"] and not saved["evidence"]
    assert saved["log_id"] == FIRST and saved["id"] == "r123456789abc"
    assert saved["type"] == saved["subject"] == "unclassified"
    assert saved["status"] == "confirmed" and saved["language"] == "en"
    assert "asker" not in saved
    for page in [1, 2, 2]:
        assert act("add", book_id="bio", pdf_page=page)["label"] == "in scope"
    saved = labels.read_labels(path)[0]
    assert saved["evidence"] == [
        dict(
            book="bio-1-en",
            work="bio",
            state="confirmed",
            pages=[dict(pdf=1, text="ok"), dict(pdf=2, text="ok")],
        )
    ]
    assert len(load_eval(path)[0]) == 1
    assert len(act("remove", book_id="bio", pdf_page=1)["expected"]) == 1
    assert act("remove", book_id="bio", pdf_page=2)["label"] == "unlabelled"
    assert not labels.read_labels(path)
    act("outside")
    assert act("clear")["label"] == "unlabelled"
    assert not labels.read_labels(path)
    for page in [0, 6, True, "1"]:
        assert (
            client.post(
                route("/label"), json=dict(action="add", book_id="bio", pdf_page=page)
            ).status_code
            == 400
        )
    assert (
        client.post(
            route("/label"), json=dict(action="add", book_id="unknown", pdf_page=1)
        ).status_code
        == 400
    )
    labels.save_label(path, logged(SECOND), "outside", books)
    act("outside")
    act("clear")
    assert [r["log_id"] for r in labels.read_labels(path)] == [SECOND]


def test_suggestions_keep_passing_first_and_deduplicate(owner):
    app, _ = owner
    client = app.test_client()
    data = client.get(route()).json
    assert data["answer"] == "Synthetic generated answer."
    assert data["unresolved"] == 1
    assert [p["pdf_page"] for p in data["suggestions"]] == [2, 3, 1]
    assert data["suggestions"][0]["passed"]
    wider = client.get(route("/suggestions")).json
    assert [p["pdf_page"] for p in wider["suggestions"]] == [2, 3, 1, 4]


def test_atomic_failure_and_concurrent_saves(owner, monkeypatch):
    app, books = owner
    path = app.config["REAL_EVAL_FILE"]
    labels.save_label(path, logged(), "outside", books)
    before = path.read_bytes()
    original = labels.Path.replace

    def fail(*_):
        assert path.read_bytes() == before
        raise OSError("Synthetic replacement failure")

    monkeypatch.setattr(labels.Path, "replace", fail)
    with pytest.raises(OSError):
        labels.save_label(path, logged(SECOND), "outside", books)
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr(labels.Path, "replace", original)
    with ThreadPoolExecutor(2) as workers:
        list(
            workers.map(
                lambda identity: labels.save_label(
                    path, logged(identity), "outside", books
                ),
                [FIRST, SECOND],
            )
        )
    assert {r["log_id"] for r in labels.read_labels(path)} == {FIRST, SECOND}


def test_recovery_reads_recorded_table_then_matching_figures(monkeypatch):
    database = MagicMock(url="postgresql://synthetic.invalid/unused")
    database.model.return_value = ("synthetic", 3, True)
    connection = database.connect.return_value.__enter__.return_value
    connection.execute.return_value.fetchone.return_value = (True,)
    calls = []

    @contextmanager
    def store(factory, url, table, dimensions):
        calls.append((table, dimensions))
        nodes = [
            SimpleNamespace(
                node_id="a" if table.startswith("e_") else "b",
                metadata=dict(book_id="bio", pdf_pages=[1, 2]),
            )
        ]
        yield SimpleNamespace(get_nodes=lambda node_ids: nodes)

    monkeypatch.setattr(labels, "managed_store", store)
    candidates = labels.recover_candidates(logged(), database)
    assert calls == [("e_v6_synthetic", 3), ("f_v6_synthetic", 3)]
    assert [c["id"] for c in candidates] == ["a", "b", "missing"]
    assert [c["rank"] for c in candidates] == [1, 2, 3]
    assert candidates[1]["grade"] is True
    assert candidates[0]["pdf_pages"] == [1, 2]
    assert "pdf_pages" not in candidates[-1]
    database.model.return_value = None
    assert all(
        "pdf_pages" not in c for c in labels.recover_candidates(logged(), database)
    )


def test_served_and_end_to_end_separate_measures(owner):
    app, books = owner
    path = app.config["REAL_EVAL_FILE"]
    labels.save_label(path, logged(), "add", books, dict(book_id="bio", pdf_page=1))
    labels.save_label(path, logged(SECOND), "outside", books)
    printed = []
    report = run_real_eval(
        path,
        books,
        [logged(), logged(SECOND)],
        lambda row: labels.recover_candidates(row, None),
        output=lambda line, **_: printed.append(line),
    )
    inside, outside = report["in_scope"][0], report["out_of_sources"][0]
    assert (
        inside["returned"]
        and not inside["passed"]
        and inside["hit10"]
        and inside["hit30"]
    )
    assert inside["unresolved"] == 1 and inside["ungraded"] == 1
    assert not outside["correct_not_found"] and outside["wrongly_passed"] == ["b"]
    calls = []

    def retrieve(question):
        calls.append("search")
        return (
            [SimpleNamespace(id="new", book_id="bio", pdf_pages=[1, 1])],
            "e_v6_today",
            0,
        )

    def grade(question, hits):
        calls.append("grade")
        return [len(calls) == 2], 0

    report = run_real_eval(
        path,
        books,
        [logged(), logged(SECOND)],
        lambda _: pytest.fail("served lookup in rerun"),
        retrieve=retrieve,
        grade=grade,
        output=lambda line, **_: printed.append(line),
    )
    assert calls == ["search", "grade", "search", "grade"]
    assert report["in_scope"][0]["passed"]
    assert report["out_of_sources"][0]["correct_not_found"]
    assert all("Synthetic" not in line for line in printed)
    assert "Synthetic" not in json.dumps(report)
    with pytest.raises(EvalError, match="Missing log"):
        run_real_eval(path, books, [], lambda _: [])

    def failure(question):
        raise RuntimeError(question)

    with pytest.raises(EvalError) as error:
        run_real_eval(path, books, [logged()], lambda _: [], retrieve=failure)
    assert "Synthetic" not in str(error.value)


def test_hit30_boundary_and_empty_real_set(tmp_path):
    expected = {"bio-1-en": dict(work="bio", pages={5})}
    mapping = {"bio": "bio-1-en"}
    miss = SimpleNamespace(id="miss", book_id="bio", pdf_pages=[1, 1])
    hit = SimpleNamespace(id="hit", book_id="bio", pdf_pages=[5, 5])
    at30 = score_results([miss] * 29 + [hit], expected, mapping)
    assert at30["hit30"] and not at30["hit10"]
    assert not score_results([miss] * 30 + [hit], expected, mapping)["hit30"]
    lines = []
    result = run_real_eval(
        tmp_path / "absent", {}, [], None, output=lambda line, **_: lines.append(line)
    )
    assert result["in_scope"] == result["out_of_sources"] == []
    assert lines == ["Nothing labelled yet."]


def test_legacy_logs_are_unscorable_but_labelled_and_rerunnable(owner, monkeypatch):
    app, books = owner
    path = app.config["REAL_EVAL_FILE"]
    old = {
        **logged(),
        "grades": [],
        "evidence": [dict(book_id="bio", pdf_pages=[1, 2])],
    }
    empty = {**logged(SECOND), "grades": []}
    logs = [old, empty]
    monkeypatch.setattr(labels, "logged_questions", lambda _: logs)
    client = app.test_client()
    data = client.get(route()).json
    assert [p["pdf_page"] for p in data["suggestions"]] == [1, 2]
    assert not any(p["passed"] for p in data["suggestions"])
    wider = client.get(route("/suggestions")).json
    assert [p["pdf_page"] for p in wider["suggestions"]] == [1, 2, 3, 4]
    assert (
        client.post(
            route("/label"), json=dict(action="add", book_id="bio", pdf_page=1)
        ).status_code
        == 200
    )
    labels.save_label(path, empty, "outside", books)
    lines = []
    report = run_real_eval(
        path,
        books,
        logs,
        lambda _: pytest.fail("No recovery for legacy grades"),
        output=lambda line, **_: lines.append(line),
    )
    assert not report["in_scope"] and not report["out_of_sources"]
    assert report["unscorable"] == {
        "in_scope": [
            dict(id="r123456789abc", set="in_scope", reason="logged before grading")
        ],
        "out_of_sources": [
            dict(
                id="rabcdef123456", set="out_of_sources", reason="logged before grading"
            )
        ],
    }
    assert "in_scope=0" in lines[-2] and "unscorable=1" in lines[-2]
    assert "correct_not_found=0" in lines[-1] and "unscorable=1" in lines[-1]
    report = run_real_eval(
        path,
        books,
        logs,
        None,
        retrieve=lambda _: (
            [SimpleNamespace(id="hit", book_id="bio", pdf_pages=[1, 1])],
            "e_v6_synthetic",
            0,
        ),
        grade=lambda *_: ([True], 0),
        output=lambda *a, **kw: None,
    )
    assert report["in_scope"][0]["passed"]
    assert not report["out_of_sources"][0]["correct_not_found"]
    assert report["unscorable"] == dict(in_scope=[], out_of_sources=[])


def test_null_language_uses_local_detector_at_save_time(owner, monkeypatch):
    app, books = owner
    path = app.config["REAL_EVAL_FILE"]
    row = {**logged(), "language": None, "grades": []}
    calls = []

    def detect(question):
        calls.append(question)
        return "ca"

    monkeypatch.setattr(labels, "detect_language", detect)
    monkeypatch.setattr(labels, "logged_questions", lambda _: [row])
    client = app.test_client()
    assert client.get(route()).json["language"] is None
    assert not calls
    response = client.post(route("/label"), json=dict(action="outside"))
    assert response.status_code == 200
    saved = labels.read_labels(path)[0]
    assert saved["language"] == "ca" and saved["language_source"] == "detected"
    assert calls == [row["question"]]
    assert row["language"] is None
    assert load_eval(path)[0][0].language == "ca"
    labels.save_label(path, logged(SECOND), "outside", books)
    second = next(r for r in labels.read_labels(path) if r["log_id"] == SECOND)
    assert second["language"] == "en" and second["language_source"] == "log"
    assert len(calls) == 1
