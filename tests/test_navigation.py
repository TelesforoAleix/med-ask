"""Synthetic navigation sources are created at runtime, never stored fixtures."""

import json
from dataclasses import asdict
from unittest.mock import MagicMock

import pymupdf
import pytest

from med_ask import create_app
from med_ask.extract import Extraction, PageAccount, Passage
from med_ask.figures import Figure
from med_ask.manifest import Book
from med_ask.navigation import (
    VERSION,
    apply_precision_gate,
    build_navigation,
    contents_and_pages,
    fingerprint,
    load_navigation,
    navigation_report,
    publish,
    references,
    resolve_references,
)
from med_ask.ocr import METHOD_VERSION, save_reading
from med_ask.retrieval import evidence_from_node, passage_id, passage_nodes


@pytest.mark.parametrize(
    "text,kind,identifiers",
    [
        ("Figure 7-3", "figure", ["7-3"]),
        ("Fig. 7.3", "figure", ["7.3"]),
        ("Figs. 3-2 and 3-4", "figure", ["3-2", "3-4"]),
        ("Figuras 3–2, 3–4 y 3–5", "figure", ["3–2", "3–4", "3–5"]),
        ("véase la tabla 3-2", "table", ["3-2"]),
        ("Table 3-2", "table", ["3-2"]),
        ("Section 7.3", "section", ["7.3"]),
        ("Sección 7.3", "section", ["7.3"]),
        ("Chapter 7", "chapter", ["7"]),
        ("Capítulo 7", "chapter", ["7"]),
        ("p. 412", "page", ["412"]),
        ("pp. 412–414", "page", ["412"]),
        ("pág. 412", "page", ["412"]),
        ("páginas 412 y 414", "page", ["412", "414"]),
    ],
)
def test_patterns_and_exact_offsets(text, kind, identifiers):
    text = "OCR: ¿" + text + "?"
    rows = references(text)
    assert [r["identifier"] for r in rows] == identifiers
    assert all(
        r["kind"] == kind and text[r["start"] : r["end"]] == r["label"] for r in rows
    )


def extraction():
    passages = [
        Passage(
            "synthetic",
            "English",
            (2, 2),
            ("412", "412"),
            None,
            ("Chapter 7", "7.3 Synthetic heading"),
            1,
            "¿See\x00 Figure 7.3; Tabla 3-2; Section 7.3; "
            "Chapter 7; pp. 412–414. Figure 99-1?",
        )
    ]
    pages = [
        PageAccount(n, True, str(410 + n), "read", None, str(n), "born-digital")
        for n in range(1, 5)
    ]
    return Extraction("synthetic", "English", passages, [], pages)


def figure(identity="figure-id", page=3, book="synthetic"):
    return Figure(
        identity,
        book,
        "English",
        "Figure 7-3",
        "7-3",
        "Synthetic caption.",
        page,
        "413",
        None,
        "born-digital",
        1,
    )


def test_contents_merge_exclusions_and_page_inheritance():
    source = extraction()
    tree, pages, first = contents_and_pages(
        source,
        [
            [1, "Contents", 1],
            [1, "Chapter 7", 1],
            [2, "7.3 Synthetic heading", 2],
            [1, "Index", 4],
        ],
    )
    assert len(tree) == 1
    assert tree[0] == dict(
        title="Chapter 7",
        pdf_page=1,
        printed_page="411",
        children=[
            dict(
                title="7.3 Synthetic heading",
                pdf_page=2,
                printed_page="412",
                children=[],
            )
        ],
    )
    assert pages[0]["section_path"] == []
    assert (
        pages[1]["section_path"] == pages[2]["section_path"] == pages[3]["section_path"]
    )
    assert pages[1]["section_path"] == [
        dict(title="Chapter 7", pdf_page=1),
        dict(title="7.3 Synthetic heading", pdf_page=2),
    ]
    assert len(first) == 2


def test_resolve_same_book_uniquely_and_stored_offsets():
    source = extraction()
    _, _, first = contents_and_pages(source, [[1, "Chapter 7", 1]])
    links, counts, _ = resolve_references(
        source,
        [figure(), figure("foreign", 4, "other")],
        first,
        ["", "", "Tabla 3-2 Synthetic caption.", ""],
    )
    node = passage_nodes(source.passages, "Synthetic")[0]
    assert node.node_id == passage_id(source.passages[0])
    rows = links[node.node_id]
    assert [r["kind"] for r in rows] == [
        "figure",
        "table",
        "section",
        "chapter",
        "page",
    ]
    assert [r["target_pdf_page"] for r in rows] == [3, 3, 2, 1, 2]
    assert all(node.text[r["start"] : r["end"]] == r["label"] for r in rows)
    assert rows[0]["figure_id"] == "figure-id"
    assert counts["figure"] == dict(resolved=1, unresolved=1)
    assert all("figure_id" not in row for row in rows[1:])


def test_ambiguous_targets_are_omitted():
    source = extraction()
    source.pages[3].printed_page = "412"
    _, _, first = contents_and_pages(source, [])
    first[("Chapter 7", "7.3 Other heading")] = 4
    first[("Chapter 7 Other chapter",)] = 3
    links, counts, _ = resolve_references(
        source,
        [figure(), figure("duplicate")],
        first,
        ["Tabla 3-2 Duplicate", "", "Table 3-2 Duplicate", ""],
    )
    assert links == {}
    assert all(counts[k]["unresolved"] >= 1 for k in counts)


def make_book(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        for number in range(1, 5):
            page = pdf.new_page()
            page.insert_text((72, 140), f"Synthetic paragraph on page {number}.")
        pdf.set_toc([[1, "Chapter 7", 1]])
        pdf.save(path)
    return Book("synthetic", path.name, "Synthetic book", "English", path)


def test_build_publish_report_and_runtime_reading(tmp_path, monkeypatch):
    book = make_book(tmp_path)
    source = extraction()
    monkeypatch.setattr("med_ask.navigation.extract_book", lambda *a, **k: source)
    monkeypatch.setattr("med_ask.navigation.figure_entries", lambda *a, **k: [figure()])
    ocr, resolutions, root = (
        tmp_path / name for name in ("ocr", "resolutions", "navigation")
    )
    save_reading(
        book.id,
        3,
        dict(method_version=METHOD_VERSION, text="Tabla 3-2 Synthetic table"),
        ocr,
    )
    report = build_navigation(book, root, ocr, resolutions)
    assert report["counts"]["table"]["resolved"] == 1
    assert report["checks"]["figure"]["precision"] == 1
    assert report["contents_coverage"] == 1
    data = load_navigation(book, root, {})
    assert data["fingerprint"] == fingerprint(book, ocr, resolutions)
    assert data["references"][passage_id(source.passages[0])]
    assert "Synthetic paragraph" not in (root / "synthetic.json").read_text()
    assert list(root.iterdir()) == [root / "synthetic.json"]


def test_changed_inputs_preserve_previous_file(tmp_path, monkeypatch):
    book = make_book(tmp_path)
    root = tmp_path / "navigation"
    root.mkdir()
    destination = root / "synthetic.json"
    destination.write_text("previous file")

    def changing_extract(*args, **kwargs):
        book.path.touch()
        return extraction()

    monkeypatch.setattr("med_ask.navigation.extract_book", changing_extract)
    monkeypatch.setattr("med_ask.navigation.figure_entries", lambda *a, **k: [])
    with pytest.raises(ValueError, match="changed"):
        build_navigation(book, root, tmp_path / "ocr", tmp_path / "resolutions")
    assert destination.read_text() == "previous file"


def test_reader_absent_stale_cached_and_replaced(tmp_path, monkeypatch):
    book = make_book(tmp_path)
    root, memory = tmp_path / "nav", {}
    assert load_navigation(book, root, memory) is None
    data = dict(
        version=VERSION,
        book_id=book.id,
        fingerprint=fingerprint(book),
        contents=[],
        pages=[],
        references={},
    )
    path = root / "synthetic.json"
    publish(path, data)
    loaded = load_navigation(book, root, memory)
    assert loaded == data
    monkeypatch.setattr(
        "med_ask.navigation.extract_book",
        lambda *a, **k: pytest.fail("Request extraction"),
    )
    assert load_navigation(book, root, memory) is loaded
    publish(path, dict(data, contents=[dict(title="new")]))
    assert load_navigation(book, root, memory)["contents"] != loaded["contents"]
    publish(path, dict(data, version=0))
    assert load_navigation(book, root, memory) is None
    publish(path, data)
    book.path.touch()
    assert load_navigation(book, root, memory) is None
    path.write_text("{broken")
    assert load_navigation(book, root, memory) is None


@pytest.mark.parametrize("private", ["true", "false"])
@pytest.mark.parametrize("state", ["present", "absent", "stale"])
def test_routes_and_search_links_preserve_evidence(
    tmp_path, monkeypatch, private, state
):
    from test_app import QuestionMemory

    book = make_book(tmp_path)
    monkeypatch.setenv("PRIVATE_ROUTES_ENABLED", private)
    monkeypatch.setattr("med_ask.load_manifest", lambda *a: {book.id: book})
    monkeypatch.setattr(
        "med_ask.navigation.extract_book",
        lambda *a, **k: pytest.fail("Request extraction"),
    )
    source = extraction()
    tree, pages, first = contents_and_pages(source, [])
    links, _, _ = resolve_references(source, [figure()], first, ["", "", "", ""])
    data = dict(
        version=VERSION if state != "stale" else 0,
        book_id=book.id,
        fingerprint=fingerprint(book),
        contents=tree,
        pages=pages,
        references=links,
    )
    root = tmp_path / "nav"
    if state != "absent":
        publish(root / "synthetic.json", data)
    app = create_app()
    app.config.update(
        TESTING=True,
        NAVIGATION_ROOT=root,
        QUESTION_DATABASE=QuestionMemory(),
        EMBEDDING_ENDPOINT=MagicMock(),
        GENERATION_ENDPOINT=MagicMock(),
    )
    app.config["GENERATION_ENDPOINT"].complete.return_value = "yes"
    evidence = evidence_from_node(passage_nodes(source.passages, book.title)[0], 0.5)
    monkeypatch.setattr(
        "med_ask.search", lambda *a, **k: ([evidence], "synthetic-table", 0.01)
    )
    client = app.test_client()
    status = 200 if state == "present" else 404
    assert client.get("/api/book/synthetic/contents").status_code == status
    response = client.get("/api/book/synthetic/page/2")
    assert response.status_code == status
    if state == "present":
        assert response.json == dict(pages[1], page_count=4)
        assert client.get("/api/book/synthetic/contents").json == dict(contents=tree)
    for route in (
        "/api/book/unknown/contents",
        "/api/book/synthetic/page/0",
        "/api/book/synthetic/page/5",
    ):
        assert client.get(route).status_code == 404
    result = client.post(
        "/api/search",
        json=dict(question="Synthetic question?"),
        headers={"Cf-Access-Authenticated-User-Email": "synthetic@example.invalid"},
    )
    assert result.status_code == 200
    item = result.json["evidence"][0]
    expected = links[evidence.id] if state == "present" else []
    assert item["links"] == expected
    before = asdict(evidence)
    for field, value in before.items():
        if field not in {
            "links",
            "description",
            "pdf_pages",
            "printed_pages",
            "section_path",
        }:
            assert item[field] == value
    assert item["pdf_pages"] == list(evidence.pdf_pages)
    assert item["section_path"] == list(evidence.section_path)
    assert item["printed_pages"] == list(evidence.printed_pages)


def test_seeded_sample_covers_every_kind_and_reports_thin_contents():
    source = extraction()
    tree, pages, first = contents_and_pages(source, [[1, "Chapter 7", 1]])
    texts = [
        "Chapter 7",
        "7.3 Synthetic heading\n412",
        "Tabla 3-2 Synthetic caption.",
        "",
    ]
    links, counts, checks = resolve_references(source, [figure()], first, texts)
    checks = checks * 30
    data = dict(contents=tree, pages=pages, references=links)
    report = navigation_report(source, data, [figure()], counts, checks, texts)
    assert report == navigation_report(source, data, [figure()], counts, checks, texts)
    assert sum(r["sampled"] for r in report["checks"].values()) == 100
    assert all(r["sampled"] > 0 for r in report["checks"].values())
    assert report["precision"] == 1
    assert report["thin_contents"] is False
    data["contents"] = []
    assert navigation_report(source, data, [], counts, [], texts)["thin_contents"]


def test_real_synthetic_build_and_resolution_fingerprint(tmp_path):
    book = make_book(tmp_path)
    root = tmp_path / "navigation"
    ocr, resolutions = tmp_path / "ocr", tmp_path / "resolutions"
    build_navigation(book, root, ocr, resolutions)
    data = load_navigation(book, root)
    assert len(data["pages"]) == 4
    before = fingerprint(book, ocr, resolutions)
    from med_ask.resolutions import save_resolution

    save_resolution(
        book.id,
        3,
        dict(text="Tabla 3-2 Synthetic replacement.", printed_page="3"),
        resolutions,
    )
    after = fingerprint(book, ocr, resolutions)
    assert after != before
    # Readers deliberately don't inspect originals: rebuilds are explicit.
    assert load_navigation(book, root)["fingerprint"] == before
    build_navigation(book, root, ocr, resolutions)
    assert load_navigation(book, root)["fingerprint"] == after


def test_failed_atomic_replace_preserves_file_and_cleans_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "synthetic.json"
    publish(path, dict(previous=True))

    def fail_replace(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr("pathlib.Path.replace", fail_replace)
    with pytest.raises(OSError):
        publish(path, dict(replacement=True))
    assert json.loads(path.read_text()) == dict(previous=True)
    assert list(tmp_path.iterdir()) == [path]


def test_navigate_cli_does_not_create_database_or_endpoints(
    tmp_path, monkeypatch, capsys
):
    from med_ask.ingest import main

    book = make_book(tmp_path)
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv", ["ingest", "navigate", book.id, "--output", str(output)]
    )
    monkeypatch.setattr("med_ask.ingest.load_manifest", lambda *a: {book.id: book})
    monkeypatch.setattr(
        "med_ask.ingest.build_navigation", lambda *a, **k: dict(book_id=book.id)
    )
    monkeypatch.setattr("med_ask.ingest._external_output", lambda path: path)

    def forbidden(*args, **kwargs):
        pytest.fail("navigate constructed a database or endpoint")

    for name in ("Database", "Endpoint", "VisionEndpoint"):
        monkeypatch.setattr("med_ask.ingest." + name, forbidden)
    main()
    assert json.loads(output.read_text()) == [dict(book_id=book.id)]
    assert json.loads(capsys.readouterr().out) == [dict(book_id=book.id)]


@pytest.mark.parametrize("passed, excluded", [(8, True), (9, False), (10, False)])
def test_precision_gate_withholds_entire_kind_and_preserves_raw_diagnostics(
    passed, excluded
):
    page_link = dict(start=0, end=6, kind="page", label="p. 412", target_pdf_page=2)
    figure_link = dict(
        start=8,
        end=18,
        kind="figure",
        label="Figure 7-3",
        target_pdf_page=3,
        figure_id="figure-id",
    )
    data = dict(
        references={"mixed": [page_link, figure_link], "pages-only": [page_link]}
    )
    counts = {
        kind: dict(
            resolved=2 if kind == "page" else 1 if kind == "figure" else 0, unresolved=3
        )
        for kind in ("figure", "table", "section", "chapter", "page")
    }
    checks = {
        kind: dict(
            sampled=10 if kind == "page" else 1 if kind == "figure" else 0,
            passed=passed if kind == "page" else 1 if kind == "figure" else 0,
            precision=passed / 10
            if kind == "page"
            else 1
            if kind == "figure"
            else None,
        )
        for kind in counts
    }
    report = dict(counts=counts, checks=checks, precision=(passed + 1) / 11)
    apply_precision_gate(data, report)
    assert (
        data["excluded_kinds"]
        == report["excluded_kinds"]
        == (["page"] if excluded else [])
    )
    assert report["checks"]["page"]["passed"] == passed
    assert report["counts"]["page"] == dict(
        resolved=2,
        unresolved=3,
        published=0 if excluded else 2,
        excluded=2 if excluded else 0,
    )
    assert data["references"]["mixed"] == (
        [figure_link] if excluded else [page_link, figure_link]
    )
    assert ("pages-only" in data["references"]) is not excluded
    assert report["precision"] == (1 if excluded else (passed + 1) / 11)
    assert report["pre_filter_precision"] == (passed + 1) / 11
    assert report["published_checks"]["page"]["sampled"] == (0 if excluded else 10)


def noisy_extraction(ocr_pages=3):
    source = extraction()
    paths = [
        ("Acknowledgments", "A C G T"),
        ("Capítulo 1", "Noise", "1.1 Synthetic section", "Equation"),
        ("Noise", "1.1.1 Synthetic subsection"),
        ("Chapter 2", "2.1 Synthetic section"),
    ]
    source.passages = [
        Passage(
            source.book_id,
            source.language,
            (n, n),
            None,
            None,
            path,
            n,
            "Synthetic paragraph.",
        )
        for n, path in enumerate(paths, 1)
    ]
    for page in source.pages[:ocr_pages]:
        page.text_source = "ocr"
    return source


def test_scanned_contents_and_breadcrumbs_use_numbered_tree_only():
    source = noisy_extraction()
    tree, pages, first = contents_and_pages(source, [])
    assert [node["title"] for node in tree] == ["Capítulo 1", "Chapter 2"]
    assert tree[0]["children"][0]["title"] == "1.1 Synthetic section"
    assert (
        tree[0]["children"][0]["children"][0]["title"] == "1.1.1 Synthetic subsection"
    )
    assert pages[0]["section_path"] == []
    assert [p["title"] for p in pages[1]["section_path"]] == [
        "Capítulo 1",
        "1.1 Synthetic section",
    ]
    assert [p["title"] for p in pages[2]["section_path"]] == [
        "Capítulo 1",
        "1.1 Synthetic section",
        "1.1.1 Synthetic subsection",
    ]
    assert [p["title"] for p in pages[3]["section_path"]] == [
        "Chapter 2",
        "2.1 Synthetic section",
    ]
    assert len(first) == 5
    report = navigation_report(
        source, dict(contents=tree, pages=pages), [], {}, [], [""] * 4
    )
    assert report["numbered_contents_only"]
    assert report["contents_levels"] == {1: 2, 2: 2, 3: 1}
    assert report["chapter_entry_coverage"] == {"1": True, "2": True}


@pytest.mark.parametrize("ocr_pages", [0, 2])
def test_books_without_majority_kept_content_pages_keep_original_contents(ocr_pages):
    source = noisy_extraction(ocr_pages)
    tree, pages, _ = contents_and_pages(source, [])
    assert tree[0]["title"] == "Acknowledgments"
    assert pages[0]["section_path"][1]["title"] == "A C G T"
    assert pages[1]["section_path"][-1]["title"] == "Equation"
    report = navigation_report(
        source, dict(contents=tree, pages=pages), [], {}, [], [""] * 4
    )
    assert not report["numbered_contents_only"]


def test_scanned_breadcrumbs_carry_past_noise_and_missing_chapters_are_reported():
    source = noisy_extraction(4)
    source.passages[2].section_path = ("Equation", "Index letters")
    source.passages[3].section_path = ("Noise", "3.2 Synthetic orphan")
    tree, pages, _ = contents_and_pages(source, [])
    assert [node["title"] for node in tree] == ["Capítulo 1"]
    assert (
        pages[1]["section_path"] == pages[2]["section_path"] == pages[3]["section_path"]
    )
    report = navigation_report(
        source, dict(contents=tree, pages=pages), [], {}, [], [""] * 4
    )
    assert report["chapter_entry_coverage"] == {"1": True, "2": False, "3": False}
    assert report["chapter_numbers_observed"] == [1, 3]


def test_build_publishes_filtered_links_and_excluded_kinds(tmp_path, monkeypatch):
    book = make_book(tmp_path)
    source = extraction()
    monkeypatch.setattr("med_ask.navigation.extract_book", lambda *a, **k: source)
    monkeypatch.setattr("med_ask.navigation.figure_entries", lambda *a, **k: [figure()])
    root = tmp_path / "nav"
    report = build_navigation(book, root, tmp_path / "ocr", tmp_path / "resolutions")
    data = load_navigation(book, root)
    assert data["version"] == 2
    assert (
        data["excluded_kinds"]
        == report["excluded_kinds"]
        == ["section", "chapter", "page"]
    )
    assert list(data["references"]) == [passage_id(source.passages[0])]
    assert [
        link["kind"] for link in data["references"][passage_id(source.passages[0])]
    ] == ["figure"]
    assert report["checks"]["page"]["precision"] == 0
    assert report["counts"]["page"]["published"] == 0
    assert report["precision"] == 1
    assert report["precision_passed"]
