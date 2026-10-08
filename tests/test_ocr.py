"""Synthetic pages and fake vision replies; never load books or contact a model."""

import base64
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pymupdf
import pytest
from openai import OpenAI

from med_ask import ingest, ocr
from med_ask.extract import extract_book
from med_ask.manifest import Book
from med_ask.retrieval import evidence_from_node, model_table, passage_nodes


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        for _ in range(3):
            page = doc.new_page(width=120, height=160)
            page.draw_rect(pymupdf.Rect(10, 20, 30, 40), fill=(0, 0, 0))
        doc.set_toc([[1, "Synthetic chapter", 1]])
        doc.save(path)
    return Book("synthetic", path.name, "Synthetic book", "English", path)


def reply(text="10\n\nSynthetic text is preserved exactly as printed.", finish="stop"):
    return dict(text=text, model="synthetic-reported", finish_reason=finish)


def endpoint(*replies):
    fake = MagicMock()
    fake.read.side_effect = list(replies)
    return fake


def kept(root, book, page, text, **overrides):
    reading = {
        **reply(text),
        "rung": "vision",
        "rotated": False,
        "method_version": ocr.METHOD_VERSION,
        "rules_failed": [],
        "seconds": 0.01,
        "time": "2026-10-08T00:00:00+00:00",
    }
    reading.update(overrides)
    record = {
        **reading,
        "readings": [reading.copy()],
        "complete": True,
        "ink": True,
        "previous_versions": [],
    }
    ocr.save_reading(book.id, page, record, root)
    return record


def rules(text, source="ocr", ink=True, candidates=None, neighbours=(), finish="stop"):
    return ocr.failed_rules(
        text,
        "English",
        source,
        ink,
        ocr.reading_candidates(text) if candidates is None else candidates,
        neighbours,
        finish,
    )


def test_empty_ink_blank_and_publisher_exceptions(monkeypatch):
    assert rules("", source="no-text") == ["empty-ink"]
    assert rules("", ink=False) == []
    assert rules("", source="born-digital") == ["empty-ink"]
    monkeypatch.setattr(ocr, "detect_language", lambda text: "es")
    garbled = "xzz% " * 300
    assert rules(garbled, source="born-digital", finish="length") == []
    assert (
        rules("A chapter opener without a printed page number.", source="born-digital")
        == []
    )
    assert set(rules(garbled)) == {"language", "nonwords", "number-missing"}


def test_each_rule_and_thresholds(monkeypatch):
    monkeypatch.setattr(ocr, "detect_language", lambda text: "es")
    assert "language" not in rules("the " * 249)
    assert "language" in rules("the " * 250)
    assert "nonwords" not in rules("xzz% " * 49)
    assert "nonwords" in rules("xzz% " * 50)
    assert "nonwords" not in rules("xzz% " * 25 + "word " * 25)
    assert "empty-ink" in rules("a" * 39)
    assert "empty-ink" not in rules("a" * 40)
    assert "token-limit" in rules("10\n" + "word " * 20, finish="length")
    assert "number-missing" in rules("Synthetic paragraph without a margin number.")
    assert "number-order" in rules(
        "99\n" + "word " * 20, neighbours=[(-1, {("arabic", 9): "9"})]
    )
    assert not rules("10\n" + "word " * 20, neighbours=[(-1, {("arabic", 9): "9"})])
    assert "nonwords" in rules("xzz% " * 50, source="inherited-ocr")


def test_nonwords_allow_accents_numbers_and_scientific_shapes():
    assert ocr.nonword_share("bioquímica nucleotides 42 amino-acids") == (0, 3)
    assert ocr.nonword_share("%%% zzz x@x") == (1, 3)


def test_ink_generated_pages():
    with pymupdf.open() as doc:
        page = doc.new_page(width=120, height=160)
        assert not ocr.has_ink(page)
        page.draw_rect(pymupdf.Rect(0, 0, 1, 160), fill=(0, 0, 0))
        assert not ocr.has_ink(page)  # scanner border outside the inner margins
        page.draw_rect(pymupdf.Rect(10, 20, 30, 40), fill=(0, 0, 0))
        assert ocr.has_ink(page)


@pytest.mark.parametrize("better", [False, True])
def test_rotation_better_choice_tie_and_resume(book, tmp_path, better):
    root = tmp_path / "ocr"
    upright = reply("Synthetic incomplete text without a margin number.")
    rotated = reply() if better else reply("Another incomplete text without a number.")
    fake = endpoint(upright, rotated)
    assert ocr.read_book(book, fake, root, limit=1, output=lambda *a, **k: None)
    record = ocr.load_reading(book.id, 1, root)
    assert len(record["readings"]) == 2
    assert record["rotated"] is better
    assert record["text"] == (rotated if better else upright)["text"]
    images = [pymupdf.Pixmap(call.args[0]) for call in fake.read.call_args_list]
    assert images[0].width == 500 and images[0].height == 667  # 300 DPI
    # Ink moves from the top-left to bottom-right on the retry.
    assert images[0].pixel(70, 110) == (0, 0, 0)
    assert images[1].pixel(70, 110) == (255, 255, 255)
    fresh = endpoint(
        reply("11\n\nSynthetic text preserved for another page in the book.")
    )
    assert ocr.read_book(book, fresh, root, limit=1, output=lambda *a, **k: None)
    assert fresh.read.call_count == 1
    assert ocr.load_reading(book.id, 1, root) == record
    summary = ocr.queue_summary(book, root)
    assert summary["rotations"] == 1
    assert summary["rotation_rescues"] == int(better)


def test_failed_both_readings_still_extract_and_queue(book, tmp_path):
    root = tmp_path / "ocr"
    fake = endpoint(
        reply("A synthetic unfinished reading without a number."),
        reply("Another synthetic unfinished reading without a number."),
    )
    ocr.read_book(book, fake, root, limit=1, output=lambda *a, **k: None)
    passage = extract_book(book.path, book.id, book.language, ocr_root=root).passages[0]
    assert passage.check_page and passage.ocr_reasons == ["number-missing"]
    assert passage.text_source == "ocr"
    node = passage_nodes([passage], book.title)[0]
    evidence = evidence_from_node(node)
    assert evidence.check_page and evidence.ocr_reasons == passage.ocr_reasons
    assert node.metadata["text_source"] == "ocr"
    summary = ocr.queue_summary(book, root)
    assert summary["queued"] == 3 and summary["kept"] == summary["read_passing"] == 0
    assert summary["queue"][0] == {"pdf_page": 1, "reasons": ["number-missing"]}
    assert summary["reasons"]["number-missing"] == 1


def test_atomic_write_cleanup_and_preserve_old_file(book, tmp_path, monkeypatch):
    root = tmp_path / "ocr"
    original = kept(root, book, 1, "Synthetic original reading.")
    replace = Path.replace
    observed = []

    def fail(path, destination):
        observed.append((path.parent, destination.parent, json.loads(path.read_text())))
        assert json.loads(destination.read_text()) == original
        raise OSError("Synthetic rename failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="rename"):
        ocr.save_reading(
            book.id, 1, {**original, "text": "Synthetic replacement."}, root
        )
    assert observed[0][0] == observed[0][1]
    assert json.loads((root / book.id / "1.json").read_text()) == original
    assert list((root / book.id).glob("*.tmp")) == []
    monkeypatch.setattr(Path, "replace", replace)
    ocr.save_reading(book.id, 1, {**original, "text": "Synthetic replacement."}, root)
    assert ocr.load_reading(book.id, 1, root)["text"] == "Synthetic replacement."


def test_version_mismatch_reread_preserves_previous_reading(book, tmp_path):
    root = tmp_path / "ocr"
    old = kept(root, book, 1, "10\n\nAn older synthetic reading.", method_version="old")
    assert ocr.load_reading(book.id, 1, root) is None
    fake = endpoint(reply())
    ocr.read_book(book, fake, root, limit=1, output=lambda *a, **k: None)
    assert fake.read.call_count == 1
    record = ocr.load_reading(book.id, 1, root)
    assert record["previous_versions"] == [old]
    assert record["method_version"] == ocr.METHOD_VERSION


def test_stop_without_marking_page_and_resume_pending_retry(book, tmp_path):
    root = tmp_path / "ocr"
    unavailable = ocr.VisionUnavailable("Synthetic unavailable service")
    output = MagicMock()
    assert not ocr.read_book(book, endpoint(unavailable), root, output=output)
    assert not root.exists()
    assert "vision is not serving" in output.call_args.args[0]
    assert not ocr.read_book(
        book,
        endpoint(reply("Synthetic failed reading without a number."), unavailable),
        root,
        output=output,
    )
    partial = ocr.load_reading(book.id, 1, root)
    assert not partial["complete"] and len(partial["readings"]) == 1
    fake = endpoint(reply())
    assert ocr.read_book(book, fake, root, limit=1, output=output)
    assert fake.read.call_count == 1
    assert ocr.load_reading(book.id, 1, root)["rotated"]


def test_compatible_vision_call_and_clean_500(book, tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    seen = []

    def respond(request):
        body = json.loads(request.content)
        assert body["model"] == "vision"
        assert body["temperature"] == 0 and body["max_tokens"] == 4096
        content = body["messages"][0]["content"]
        assert content[0]["text"] == ocr.PROMPT
        url = content[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",")[1]).startswith(b"\x89PNG")
        seen.append(body)
        return httpx.Response(500, json={"error": {"message": "Synthetic offline"}})

    fake = ocr.VisionEndpoint()
    fake.client = OpenAI(
        base_url="http://synthetic.invalid/v1",
        api_key="unused",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    root = tmp_path / "ocr"
    assert not ocr.read_book(book, fake, root, output=lambda *a, **k: None)
    assert len(seen) == 1 and not root.exists()


def test_extraction_paragraphs_captions_watermark_number_and_heading(book, tmp_path):
    root = tmp_path / "ocr"
    kept(
        root,
        book,
        1,
        "10\n\nSynthetic opening paragraph.\n\nSUMMARY\n"
        "Synthetic summary paragraph.\n\n[FIGURE 2-1]\n"
        "Figure 2-1 Synthetic caption.\n\nCopyrighted material",
    )
    kept(root, book, 2, "11\n\nAnother complete synthetic paragraph is preserved here.")
    extraction = extract_book(book.path, book.id, book.language, ocr_root=root)
    assert [p.text for p in extraction.passages] == [
        "Synthetic opening paragraph.",
        "Synthetic summary paragraph.",
        "Another complete synthetic paragraph is preserved here.",
    ]
    assert extraction.passages[0].section_path == ("Synthetic chapter",)
    assert extraction.passages[1].kind == "summary"
    assert extraction.passages[1].section_path == ("Synthetic chapter", "SUMMARY")
    assert extraction.captions[0].identifier == "2-1"
    assert extraction.captions[0].text == "Figure 2-1 Synthetic caption."
    assert extraction.pages[0].printed_page == "10"
    assert extraction.passages[0].printed_pages == ("10", "10")
    assert all(p.text_source == "ocr" and not p.check_page for p in extraction.passages)


@pytest.mark.parametrize("queued", [False, True])
def test_reading_cross_page_join_and_queue_exception(book, tmp_path, queued):
    root = tmp_path / "ocr"
    kept(
        root, book, 1, "10\n\nA complete synthetic paragraph continues across the page"
    )
    kept(
        root,
        book,
        2,
        ("" if queued else "11\n\n")
        + "and finishes on another page of this synthetic book.",
    )
    extraction = extract_book(book.path, book.id, book.language, ocr_root=root)
    assert len(extraction.passages) == (2 if queued else 1)
    if not queued:
        assert extraction.passages[0].pdf_pages == (1, 2)
        assert extraction.passages[0].printed_pages == ("10", "11")
    else:
        assert extraction.passages[-1].check_page


def test_no_readings_and_old_version_preserve_extraction(book, tmp_path):
    empty = tmp_path / "empty"
    before = asdict(extract_book(book.path, book.id, book.language, ocr_root=empty))
    old_root = tmp_path / "old"
    kept(old_root, book, 1, "An unrelated synthetic reading.", method_version="old")
    assert (
        asdict(extract_book(book.path, book.id, book.language, ocr_root=old_root))
        == before
    )
    assert asdict(extract_book(book.path, book.id, book.language)) == before


def test_ocr_cli_queue_and_resume_without_database(book, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(ingest, "load_manifest", lambda path: {book.id: book})
    monkeypatch.setattr(
        ingest, "queue_summary", lambda b: {"book_id": b.id, "queued": 3}
    )
    monkeypatch.setattr("sys.argv", ["ingest", "ocr-queue"])
    ingest.main()
    assert json.loads(capsys.readouterr().out)[0]["queued"] == 3
    read = MagicMock(return_value=False)
    monkeypatch.setattr(ingest, "VisionEndpoint", MagicMock())
    monkeypatch.setattr(ingest, "read_book", read)
    monkeypatch.setattr("sys.argv", ["ingest", "ocr", book.id, "--limit", "20"])
    with pytest.raises(SystemExit) as error:
        ingest.main()
    assert error.value.code == 3
    assert read.call_args.kwargs["limit"] == 20
    monkeypatch.setattr("sys.argv", ["ingest", "ocr", book.id, "--limit", "0"])
    with pytest.raises(SystemExit) as error:
        ingest.main()
    assert error.value.code == 2


def test_index_version_setting_preserves_role_rule(monkeypatch):
    monkeypatch.delenv("INDEX_VERSION", raising=False)
    plain = model_table("synthetic-model")
    monkeypatch.setenv("INDEX_VERSION", "v3")
    assert model_table("synthetic-model") == plain.replace("e_v2_", "e_v3_", 1)
    assert model_table("synthetic-model", True) == plain.replace("e_v2_", "e_v3r_", 1)
    assert model_table("synthetic-model", True, "embed-large") == model_table(
        "synthetic-model"
    )
    monkeypatch.setenv("INDEX_VERSION", "invalid/table")
    with pytest.raises(ValueError, match="INDEX_VERSION"):
        model_table("synthetic-model")


def test_reported_model_and_token_limit_are_kept(book, tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic-reported",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": "10\n\nAn unfinished synthetic reading "
                            "with sufficient words.",
                        },
                    }
                ],
            },
        )

    fake = ocr.VisionEndpoint()
    fake.client = OpenAI(
        base_url="http://synthetic.invalid/v1",
        api_key="unused",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    root = tmp_path / "ocr"
    assert ocr.read_book(book, fake, root, limit=1, output=lambda *a, **k: None)
    record = ocr.load_reading(book.id, 1, root)
    assert record["model"] == "synthetic-reported"
    assert record["finish_reason"] == "length"
    assert record["rules_failed"] == ["token-limit"]
    assert len(calls) == len(record["readings"]) == 2
    assert not record["rotated"]  # upright wins the tie


def test_caption_after_blank_line_and_inferred_printed_page(book, tmp_path):
    root = tmp_path / "ocr"
    kept(root, book, 1, "10\n\nSynthetic source text, sufficiently long for the rules.")
    kept(
        root,
        book,
        2,
        "Synthetic source text, sufficiently long for the rules.\n\n"
        "[FIGURE ?]\n\nAn unnumbered synthetic figure caption.",
    )
    kept(root, book, 3, "12\n\nSynthetic source text, sufficiently long for the rules.")
    result = extract_book(book.path, book.id, book.language, ocr_root=root)
    assert result.pages[1].printed_page == "11"
    assert result.pages[1].printed_page_method == "inferred"
    assert result.captions[0].identifier == "?"
    assert result.captions[0].text == "An unnumbered synthetic figure caption."
    assert all("caption" not in p.text for p in result.passages)
    assert result.passages[1].ocr_reasons == ["number-missing"]


def test_remaining_inherited_ocr_is_marked_and_does_not_join(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        for page_number in range(3):
            page = doc.new_page(width=600, height=800)
            if page_number < 2:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 800))
                pix.clear_with(245)
                page.insert_image(page.rect, stream=pix.tobytes("png"))
                page.insert_text(
                    (50, 100),
                    "A synthetic inherited paragraph continues",
                    render_mode=3,
                )
            else:
                page.draw_rect(pymupdf.Rect(40, 80, 100, 120), fill=(0, 0, 0))
        doc.save(path)
    book = Book("synthetic", path.name, "Synthetic book", "English", path)
    root = tmp_path / "ocr"
    kept(root, book, 3, "12\n\nA complete synthetic reading of another page ends here.")
    result = extract_book(path, book.id, book.language, ocr_root=root)
    inherited = [p for p in result.passages if p.inherited_ocr]
    assert len(inherited) == 2
    assert all(p.pdf_pages[0] == p.pdf_pages[1] for p in inherited)
    assert all(p.text_source == "inherited-ocr" and p.check_page for p in inherited)
    assert all(p.ocr_reasons == ["number-missing"] for p in inherited)


@pytest.mark.parametrize("recorded_model", [False, True])
def test_hidden_pages_are_not_read_or_queued_and_status_counts_them(
    tmp_path, recorded_model
):
    from med_ask.retrieval import ingest_status

    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        for _ in range(2):
            page = doc.new_page(width=120, height=160)
            page.insert_text((10, 60), "Hidden page")
            # Even an inked placeholder must never be sent to vision.
            page.draw_rect(pymupdf.Rect(10, 80, 30, 100), fill=(0, 0, 0))
        doc.save(path)
    book = Book("synthetic", path.name, "Synthetic book", "English", path)
    root = tmp_path / "ocr"
    kept(root, book, 1, "Synthetic kept reading which must be ignored.")
    kept(
        root,
        book,
        2,
        "Synthetic older reading which must not be repeated.",
        method_version="old",
    )
    before = {p.name: p.read_bytes() for p in (root / book.id).iterdir()}
    fake = endpoint()
    assert ocr.read_book(book, fake, root, output=lambda *a, **k: None)
    fake.read.assert_not_called()
    assert {p.name: p.read_bytes() for p in (root / book.id).iterdir()} == before
    unused_root = tmp_path / "unused"
    assert ocr.read_book(book, fake, unused_root, output=lambda *a, **k: None)
    fake.read.assert_not_called()
    assert not unused_root.exists()
    summary = ocr.queue_summary(book, root)
    assert summary["hidden"] == summary["pages"] == 2
    assert summary["kept"] == summary["queued"] == summary["read_passing"] == 0
    assert summary["queue"] == [] and summary["reasons"] == {}
    assert summary["rotations"] == 0 and summary["seconds_per_reading"] == []
    extraction = extract_book(path, book.id, book.language, ocr_root=root)
    assert not extraction.passages and not extraction.captions
    assert all(
        not page.has_text and page.text_source == "hidden" for page in extraction.pages
    )
    database = MagicMock()
    database.models.return_value = (
        [("synthetic-table", "synthetic-reported", 3)] if recorded_model else []
    )
    database.model.return_value = None
    rows = ingest_status([book], database)
    assert rows[0]["hidden"] == 2
    assert rows[0]["stored"] == rows[0]["extracted"] == 0
    assert ocr.failed_rules("Hidden page", "English", "hidden", True, {}) == []
