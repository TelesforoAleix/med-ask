"""Resolve only synthetic pages generated at runtime, without models or databases."""

import json
from unittest.mock import MagicMock

import pymupdf
import pytest

from med_ask import create_app, ocr
from med_ask.extract import extract_book
from med_ask.manifest import Book
from med_ask.resolutions import load_resolution, save_resolution
from med_ask.resolve_cache import build_cache


@pytest.fixture
def owner(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        for _ in range(3):
            page = pdf.new_page(width=120, height=160)
            page.draw_rect(pymupdf.Rect(10, 20, 30, 40), fill=(0, 0, 0))
        pdf.save(path)
    book = Book("synthetic", path.name, "Synthetic book", "English", path)
    monkeypatch.setenv("PRIVATE_ROUTES_ENABLED", "true")
    monkeypatch.setattr("med_ask.resolutions.load_manifest", lambda _: {book.id: book})
    app = create_app()
    app.config.update(
        TESTING=True,
        OCR_ROOT=tmp_path / "ocr",
        RESOLUTIONS_ROOT=tmp_path / "resolutions",
        RESOLVE_CACHE_ROOT=tmp_path / "resolve-cache",
    )
    reading = dict(
        text="A synthetic original paragraph with enough words to be read.",
        model="synthetic",
        finish_reason="length",
        rotated=False,
        rules_failed=["token-limit", "number-missing"],
        seconds=0,
        method_version=ocr.METHOD_VERSION,
        complete=True,
        ink=True,
        readings=[],
        previous_versions=[],
    )
    ocr.save_reading(book.id, 1, reading, app.config["OCR_ROOT"])
    build_cache(book, app.config["OCR_ROOT"], app.config["RESOLVE_CACHE_ROOT"])
    return app, book


def route(suffix=""):
    return "/api/private/resolve/page/synthetic/1" + suffix


@pytest.mark.parametrize(
    "action,fields,expected_number",
    [
        ("confirm", {}, None),
        ("number", {"printed_page": "XII"}, "XII"),
        (
            "text",
            {"text": "25\n\nA synthetic replacement paragraph, as typed by the owner."},
            "25",
        ),
    ],
)
def test_resolution_actions_apply_without_changing_kept(
    owner, action, fields, expected_number
):
    app, book = owner
    client = app.test_client()
    root = app.config["OCR_ROOT"]
    resolutions = app.config["RESOLUTIONS_ROOT"]
    path = root / book.id / "1.json"
    before = path.read_bytes()
    assert (
        client.get(
            "/api/private/resolve/queue?book=synthetic&reason=number-missing"
        ).json["queue"][0]["pdf_page"]
        == 1
    )
    detail = client.get(route()).json
    assert detail["generated"] and detail["reason_messages"]
    assert (
        client.post(
            route("/resolution"), json=dict(action=action, **fields)
        ).status_code
        == 200
    )
    assert path.read_bytes() == before
    resolution = load_resolution(book.id, 1, resolutions)
    assert resolution["printed_page"] == expected_number
    assert resolution["text"] == fields.get("text", detail["text"])
    assert 1 not in [
        row["pdf_page"]
        for row in client.get("/api/private/resolve/queue").json["queue"]
    ]
    result = extract_book(
        book.path, book.id, book.language, ocr_root=root, resolutions_root=resolutions
    )
    passage = result.passages[0]
    assert passage.text == resolution["text"].split("\n\n")[-1]
    assert not passage.check_page and passage.ocr_reasons == []
    assert result.pages[0].printed_page == expected_number
    assert (
        client.post(
            route("/resolution"), json={"action": "number", "printed_page": "26"}
        ).status_code
        == 200
    )
    assert (
        len(json.loads((resolutions / book.id / "1.json").read_text())["history"]) == 2
    )


def test_hint_is_one_call_append_only_until_accepted(owner):
    app, book = owner
    client = app.test_client()
    endpoint = MagicMock()
    endpoint.read.return_value = dict(
        text="30\n\nA synthetic hinted source paragraph ends here.",
        model="synthetic",
        finish_reason="stop",
    )
    app.config["VISION_ENDPOINT"] = endpoint
    before = ocr.load_reading(book.id, 1, app.config["OCR_ROOT"])
    response = client.post(
        route("/hint"), json={"hint": "The number is at the bottom."}
    )
    assert response.status_code == 200
    endpoint.read.assert_called_once()
    assert endpoint.read.call_args.kwargs == {"hint": "The number is at the bottom."}
    after = ocr.load_reading(book.id, 1, app.config["OCR_ROOT"])
    assert {k: v for k, v in after.items() if k != "readings"} == {
        k: v for k, v in before.items() if k != "readings"
    }
    assert after["readings"][-1]["hinted"]
    assert load_resolution(book.id, 1, app.config["RESOLUTIONS_ROOT"]) is None
    # Leaving/discarding the candidate cannot change extraction.
    result = extract_book(
        book.path,
        book.id,
        book.language,
        ocr_root=app.config["OCR_ROOT"],
        resolutions_root=app.config["RESOLUTIONS_ROOT"],
    )
    assert result.passages[0].check_page
    assert (
        client.post(
            route("/resolution"),
            json={"action": "hinted", "attempt_id": response.json["attempt_id"]},
        ).status_code
        == 200
    )
    result = extract_book(
        book.path,
        book.id,
        book.language,
        ocr_root=app.config["OCR_ROOT"],
        resolutions_root=app.config["RESOLUTIONS_ROOT"],
    )
    assert result.passages[0].text == "A synthetic hinted source paragraph ends here."
    assert result.pages[0].printed_page == "30"
    assert not result.passages[0].check_page


@pytest.mark.parametrize(
    "error",
    [
        ocr.VisionUnavailable("private endpoint details"),
        ocr.VisionPageTimeout("private timeout"),
    ],
)
def test_hint_failure_leaves_files_and_other_actions_working(owner, error):
    app, book = owner
    endpoint = MagicMock()
    endpoint.read.side_effect = error
    app.config["VISION_ENDPOINT"] = endpoint
    path = app.config["OCR_ROOT"] / book.id / "1.json"
    before = path.read_bytes()
    client = app.test_client()
    response = client.post(route("/hint"), json={"hint": "Synthetic hint"})
    assert response.status_code == 503
    assert "private" not in response.json["error"]
    assert path.read_bytes() == before
    assert endpoint.read.call_count == 1
    assert client.get(route()).status_code == 200
    assert (
        client.post(route("/resolution"), json={"action": "confirm"}).status_code == 200
    )


def test_validation_and_origin(owner):
    app, _ = owner
    client = app.test_client()
    for body in (
        {},
        {"action": "text", "text": ""},
        {"action": "number", "printed_page": "../1"},
        {"action": "hinted", "attempt_id": "missing"},
    ):
        assert client.post(route("/resolution"), json=body).status_code == 400
    assert client.post(route("/hint"), json={"hint": ""}).status_code == 400
    assert (
        client.post(
            route("/resolution"),
            json={"action": "confirm"},
            headers={"Origin": "https://unrelated.invalid"},
        ).status_code
        == 400
    )
    assert client.get("/api/private/resolve/page/unknown/1").status_code == 404
    assert client.get("/api/private/resolve/page/synthetic/99").status_code == 404


def test_resolution_without_kept_reading(owner):
    app, book = owner
    root = app.config["RESOLUTIONS_ROOT"]
    root.joinpath(book.id).mkdir(parents=True)
    save_resolution(
        book.id,
        2,
        dict(
            action="text",
            text="An owner typed synthetic source paragraph ends here.",
            printed_page="11",
        ),
        root,
    )
    result = extract_book(
        book.path,
        book.id,
        book.language,
        ocr_root=app.config["OCR_ROOT"],
        resolutions_root=root,
    )
    passage = next(p for p in result.passages if p.pdf_pages == (2, 2))
    assert passage.printed_pages == ("11", "11") and not passage.check_page


def test_confirm_preserves_an_uncorroborated_printed_number(owner):
    app, book = owner
    root = app.config["OCR_ROOT"]
    record = ocr.load_reading(book.id, 1, root)
    record["text"] = "30\n\nA synthetic source paragraph with enough words ends here."
    ocr.save_reading(book.id, 1, record, root)
    build_cache(book, root, app.config["RESOLVE_CACHE_ROOT"])
    assert (
        app.test_client()
        .post(route("/resolution"), json={"action": "confirm"})
        .status_code
        == 200
    )
    result = extract_book(
        book.path,
        book.id,
        book.language,
        ocr_root=root,
        resolutions_root=app.config["RESOLUTIONS_ROOT"],
    )
    assert result.pages[0].printed_page == "30"
    assert not result.passages[0].check_page


def test_cached_queue_observes_another_workers_resolution(owner, monkeypatch):
    app, book = owner
    inspect = MagicMock(wraps=ocr.inspect_book)
    monkeypatch.setattr("med_ask.resolve_cache.inspect_book", inspect)
    client = app.test_client()
    assert len(client.get("/api/private/resolve/queue").json["queue"]) == 3
    assert client.get(route()).status_code == 200
    assert inspect.call_count == 0
    root = app.config["RESOLUTIONS_ROOT"]
    root.joinpath(book.id).mkdir(parents=True)
    save_resolution(
        book.id,
        1,
        dict(action="confirm", text="Synthetic source.", printed_page="10"),
        root,
    )
    assert len(client.get("/api/private/resolve/queue").json["queue"]) == 2
    assert inspect.call_count == 0


def test_atomic_resolution_failure_keeps_previous_history(owner, monkeypatch):
    app, book = owner
    client = app.test_client()
    assert (
        client.post(route("/resolution"), json={"action": "confirm"}).status_code == 200
    )
    root = app.config["RESOLUTIONS_ROOT"]
    path = root / book.id / "1.json"
    before = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("med_ask.ocr.os.fsync", fail)
    assert (
        client.post(
            route("/resolution"), json={"action": "number", "printed_page": "12"}
        ).status_code
        == 503
    )
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_all_read_and_resolution_requests_work_without_opening_pdf(owner, monkeypatch):
    app, _ = owner

    def forbidden(*args, **kwargs):
        raise AssertionError("PDF opened inside a resolve request")

    monkeypatch.setattr("med_ask.resolve_cache.inspect_book", forbidden)
    monkeypatch.setattr("pymupdf.open", forbidden)
    client = app.test_client()
    assert client.get("/api/private/resolve/queue").status_code == 200
    assert client.get(route()).status_code == 200
    assert (
        client.post(route("/resolution"), json={"action": "confirm"}).status_code == 200
    )


@pytest.mark.parametrize("change", ["missing", "pdf", "reading", "language", "version"])
def test_missing_or_stale_cache_fails_fast_without_scan(owner, monkeypatch, change):
    app, book = owner
    path = app.config["RESOLVE_CACHE_ROOT"] / book.id / "diagnostics.json"
    if change == "missing":
        path.unlink()
    elif change == "pdf":
        book.path.touch()
    elif change == "reading":
        (app.config["OCR_ROOT"] / book.id / "1.json").touch()
    else:
        data = json.loads(path.read_text())
        data["fingerprint"][change] = "changed"
        path.write_text(json.dumps(data))

    def forbidden(*args, **kwargs):
        raise AssertionError("request tried to rebuild diagnostics")

    monkeypatch.setattr("med_ask.resolve_cache.inspect_book", forbidden)
    for path in ("/api/private/resolve/queue", route()):
        response = app.test_client().get(path)
        assert response.status_code == 503
        assert "ingest resolve-cache synthetic" in response.json["error"]


def test_hint_leaves_cache_valid_for_the_next_request(owner):
    app, _ = owner
    endpoint = MagicMock()
    endpoint.read.return_value = dict(
        text="A synthetic hinted reading.", model="synthetic", finish_reason="stop"
    )
    app.config["VISION_ENDPOINT"] = endpoint
    client = app.test_client()
    assert (
        client.post(route("/hint"), json={"hint": "Synthetic hint"}).status_code == 200
    )
    assert client.get(route()).status_code == 200
    assert client.get("/api/private/resolve/queue").status_code == 200


def test_failed_cache_build_preserves_previous_cache(owner, monkeypatch):
    app, book = owner
    path = app.config["RESOLVE_CACHE_ROOT"] / book.id / "diagnostics.json"
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic build interruption")

    monkeypatch.setattr("med_ask.resolve_cache.inspect_book", fail)
    with pytest.raises(OSError):
        build_cache(book, app.config["OCR_ROOT"], app.config["RESOLVE_CACHE_ROOT"])
    assert path.read_bytes() == before


def test_cache_command_uses_neither_database_nor_model(owner, monkeypatch, capsys):
    from med_ask import ingest

    app, book = owner
    monkeypatch.setattr(ingest, "load_manifest", lambda _: {book.id: book})
    monkeypatch.setattr(ingest, "OCR_ROOT", app.config["OCR_ROOT"])
    monkeypatch.setattr(
        ingest,
        "build_cache",
        lambda book, root: build_cache(book, root, app.config["RESOLVE_CACHE_ROOT"]),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic command constructed a database or model")

    for name in ("Database", "Endpoint", "VisionEndpoint"):
        monkeypatch.setattr(ingest, name, forbidden)
    monkeypatch.setattr("sys.argv", ["ingest", "resolve-cache", book.id])
    ingest.main()
    assert json.loads(capsys.readouterr().out)[0]["queued"] == 3


def test_hint_cannot_mask_an_unrelated_reading_change(owner):
    app, _ = owner
    endpoint = MagicMock()

    def read(*args, **kwargs):
        ocr.save_reading(
            "synthetic",
            2,
            dict(method_version=ocr.METHOD_VERSION),
            app.config["OCR_ROOT"],
        )
        return dict(
            text="Synthetic hinted text.", model="synthetic", finish_reason="stop"
        )

    endpoint.read.side_effect = read
    app.config["VISION_ENDPOINT"] = endpoint
    client = app.test_client()
    assert (
        client.post(route("/hint"), json={"hint": "Synthetic hint"}).status_code == 200
    )
    response = client.get("/api/private/resolve/queue")
    assert response.status_code == 503
    assert "resolve-cache synthetic" in response.json["error"]


def test_changed_inputs_during_cache_build_preserve_previous_cache(owner, monkeypatch):
    from med_ask.resolve_cache import DiagnosticsUnavailable

    app, book = owner
    path = app.config["RESOLVE_CACHE_ROOT"] / book.id / "diagnostics.json"
    before = path.read_bytes()

    def changed(*args, **kwargs):
        pages = ocr.inspect_book(*args, **kwargs)
        book.path.touch()
        return pages

    monkeypatch.setattr("med_ask.resolve_cache.inspect_book", changed)
    with pytest.raises(DiagnosticsUnavailable, match="Inputs changed"):
        build_cache(book, app.config["OCR_ROOT"], app.config["RESOLVE_CACHE_ROOT"])
    assert path.read_bytes() == before
