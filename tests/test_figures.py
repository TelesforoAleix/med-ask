"""Real caption/reading shapes with invented text and PDFs generated at runtime."""

import json
from dataclasses import asdict, replace
from unittest.mock import MagicMock

import pymupdf
import pytest
from llama_index.core.schema import MetadataMode
from test_ocr import kept
from test_retrieval import FakeEndpoint, make_book, make_passage

from med_ask.evaluation import EvalError, figure_gate
from med_ask.extract import Caption, Extraction, PageAccount, extract_book
from med_ask.figures import (
    describe_figures,
    figure_entries,
    load_description,
    save_description,
)
from med_ask.generation import build_answer_prompt, grade_candidates, group_evidence
from med_ask.ocr import VisionEndpoint, VisionPageTimeout, VisionUnavailable
from med_ask.retrieval import (
    evidence_from_node,
    figure_nodes,
    indexed_nodes,
    model_table,
    passage_nodes,
    search,
)


def extraction(caption="Figure 3-12 Synthetic structures."):
    return Extraction(
        "synthetic",
        "English",
        [],
        [Caption("synthetic", "English", "3-12", 1, caption)],
        [PageAccount(1, True, "10", "read", None, "", "born-digital")],
    )


@pytest.mark.parametrize("label", ["Figure 3-12", "Figura 3-12", "Fig. 3-12"])
def test_text_layer_caption_shape(tmp_path, label):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 130), label + " Synthetic structures in sequence.")
        pdf.save(path)
    result = extract_book(path, "synthetic", "English", ocr_root=tmp_path / "ocr")
    entries = figure_entries(result, tmp_path / "figures")
    assert len(entries) == 1
    assert entries[0].label == label
    assert entries[0].caption == label + " Synthetic structures in sequence."
    assert entries[0].pdf_page == 1
    assert not result.passages


def test_kept_reading_inline_and_separate_caption_shapes(tmp_path):
    book = make_book(tmp_path)
    root = tmp_path / "ocr"
    kept(
        root,
        book,
        1,
        "10\n\n[FIGURE 3-12] Figura 3-12 Estructuras sintéticas.\n\n"
        "[FIGURE 3-13]\n\nFigura 3-13 Otras estructuras.\n\n"
        "[FIGURE ?] Una placa sintética sin número.\n\n"
        "Texto original sintético conservado para el lector.",
    )
    result = extract_book(book.path, book.id, "Spanish", ocr_root=root)
    figures = figure_entries(result, tmp_path / "figures")
    assert [f.identifier for f in figures] == ["3-12", "3-13", "?"]
    assert figures[0].caption == "Figura 3-12 Estructuras sintéticas."
    assert figures[1].caption == "Figura 3-13 Otras estructuras."
    assert figures[2].caption == "Una placa sintética sin número."
    assert all(f.text_source == "ocr" for f in figures)
    assert not any(
        "FIGURE" in p.text or "estructuras" in p.text for p in result.passages
    )


def test_entries_identity_printed_page_and_unnumbered(tmp_path):
    source = extraction()
    source.captions += [Caption("synthetic", "English", "?", 1, "Synthetic plate.")] * 2
    entries = figure_entries(source, tmp_path)
    assert len({f.id for f in entries}) == 3
    assert entries == figure_entries(source, tmp_path)
    assert entries[0].printed_page == "10"
    assert (
        figure_entries(extraction("Changed synthetic caption."), tmp_path)[0].id
        != entries[0].id
    )


def test_owner_resolution_supplies_figure_caption_and_printed_page(tmp_path):
    from med_ask.resolutions import save_resolution

    book = make_book(tmp_path)
    readings, resolutions = tmp_path / "ocr", tmp_path / "resolutions"
    kept(
        readings,
        book,
        1,
        "10\n\n[FIGURE 3-12] Figure 3-12 Synthetic original caption.",
        rotated=True,
    )
    original = figure_entries(
        extract_book(
            book.path,
            book.id,
            book.language,
            ocr_root=readings,
            resolutions_root=resolutions,
        ),
        tmp_path / "figures",
    )[0]
    (resolutions / book.id).mkdir(parents=True)
    save_resolution(
        book.id,
        1,
        dict(
            action="text",
            printed_page="25",
            text="25\n\n[FIGURE 3-12] Figure 3-12 Synthetic corrected caption.",
        ),
        resolutions,
    )
    resolved = figure_entries(
        extract_book(
            book.path,
            book.id,
            book.language,
            ocr_root=readings,
            resolutions_root=resolutions,
        ),
        tmp_path / "figures",
    )[0]
    assert resolved.caption == "Figure 3-12 Synthetic corrected caption."
    assert resolved.printed_page == "25" and resolved.rotated
    assert resolved.id != original.id


def test_figure_request_keeps_its_prompt_and_timeout_after_hinted_read(monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    monkeypatch.setattr("med_ask.ocr.OpenAI", MagicMock())
    endpoint = VisionEndpoint()
    endpoint.read(b"synthetic PNG", hint="Read the lower margin.")
    endpoint.describe(b"synthetic PNG", "Describe synthetic Figure 3-12.")
    call = endpoint.client.chat.completions.create
    assert call.call_count == 2
    assert call.call_args.kwargs["model"] == "vision"
    assert call.call_args.kwargs["timeout"] == 180
    assert (
        call.call_args.kwargs["messages"][0]["content"][0]["text"]
        == "Describe synthetic Figure 3-12."
    )


def test_descriptions_resume_and_whole_page(tmp_path):
    book = make_book(tmp_path)
    figures = figure_entries(extraction(), tmp_path)
    endpoint = MagicMock()
    endpoint.describe.return_value = dict(
        text="Synthetic labels joined by arrows.", finish_reason="stop"
    )
    first = describe_figures(
        book, figures, endpoint, tmp_path, output=lambda *a, **k: None
    )
    again = describe_figures(
        book, figures, endpoint, tmp_path, output=lambda *a, **k: None
    )
    assert first["described"] == 1 and again["skipped"] == 1
    assert endpoint.describe.call_count == 1
    png, prompt = endpoint.describe.call_args.args
    pixmap = pymupdf.Pixmap(png)
    assert pixmap.width > 2000 and pixmap.height > 3000  # Whole A4 page at 300 DPI.
    assert figures[0].label in prompt and figures[0].caption in prompt
    record = load_description(figures[0], tmp_path)
    assert record["generated"] and record["method_version"]
    loaded = figure_entries(extraction(), tmp_path, require_records=True)
    assert loaded[0].description == "Synthetic labels joined by arrows."
    # An older method stays usable; a rebuild never triggers a new description.
    record["method_version"] = "synthetic-earlier-method"
    save_description(figures[0], record, tmp_path)
    describe_figures(book, figures, endpoint, tmp_path, output=lambda *a, **k: None)
    assert endpoint.describe.call_count == 1


@pytest.mark.parametrize(
    "reply,reason",
    [
        (VisionPageTimeout(), "page-timeout"),
        (dict(text="partial", finish_reason="length"), "incomplete"),
        (dict(text="NOT_LOCATED", finish_reason="stop"), "not-located"),
        (dict(text="", finish_reason="stop"), "empty"),
    ],
)
def test_failures_are_kept_and_not_outages(tmp_path, reply, reason):
    book = make_book(tmp_path)
    figures = figure_entries(extraction(), tmp_path)
    endpoint = MagicMock()
    endpoint.describe.side_effect = [reply]
    result = describe_figures(
        book, figures, endpoint, tmp_path, output=lambda *a, **k: None
    )
    assert result["failed"] == 1
    record = load_description(figures[0], tmp_path)
    assert record["reason"] == reason and record["description"] == ""
    describe_figures(book, figures, endpoint, tmp_path, output=lambda *a, **k: None)
    assert endpoint.describe.call_count == 1
    assert (
        figure_entries(extraction(), tmp_path, require_records=True)[0].description
        == ""
    )


def test_outage_keeps_progress_and_unattempted_can_resume(tmp_path):
    book = make_book(tmp_path)
    first = figure_entries(extraction(), tmp_path)[0]
    figures = [first, replace(first, id="synthetic-second-id")]
    endpoint = MagicMock()
    endpoint.describe.side_effect = [
        dict(text="Generated synthetic text.", finish_reason="stop"),
        VisionUnavailable(),
    ]
    with pytest.raises(VisionUnavailable):
        describe_figures(book, figures, endpoint, tmp_path, output=lambda *a, **k: None)
    assert load_description(first, tmp_path)
    assert load_description(figures[1], tmp_path) is None


def test_atomic_failure_leaves_previous_record(tmp_path, monkeypatch):
    figure = figure_entries(extraction(), tmp_path)[0]
    path = tmp_path / figure.book_id / f"{figure.id}.json"
    save_description(figure, {"synthetic": "previous"}, tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(
        "med_ask.figures.json.dump", MagicMock(side_effect=OSError("synthetic failure"))
    )
    with pytest.raises(OSError):
        save_description(figure, {"synthetic": "replacement"}, tmp_path)
    assert path.read_bytes() == before
    assert len(list(path.parent.iterdir())) == 1


def test_default_entries_ignore_and_preserve_existing_description_file(tmp_path):
    source = extraction()
    figure = figure_entries(source, tmp_path)[0]
    record = {
        **asdict(figure),
        "status": "described",
        "method_version": "synthetic-method",
        "description": "Unused generated text.",
    }
    save_description(figure, record, tmp_path)
    path = tmp_path / figure.book_id / f"{figure.id}.json"
    before = path.read_bytes()
    assert figure_entries(source, tmp_path)[0].description == ""
    assert (
        figure_entries(source, tmp_path, load_descriptions=True)[0].description
        == record["description"]
    )
    assert path.read_bytes() == before


def test_figure_nodes_and_grading_ignore_supplied_descriptions(tmp_path):
    figure = replace(
        figure_entries(extraction(), tmp_path)[0],
        description="Secret generated structures: spindle.",
    )
    node = figure_nodes([figure], "Synthetic title")[0]
    assert (
        node.get_content(metadata_mode=MetadataMode.EMBED)
        == figure.label + "\n\n" + figure.caption
    )
    assert "description" not in node.metadata
    evidence = evidence_from_node(node, 0.8)
    assert evidence.text == figure.caption and evidence.kind == "figure"
    assert evidence.description == "" and not evidence.found_by_description
    # Even a stale caller cannot pass generated discovery text into grading.
    evidence = replace(
        evidence, description=figure.description, found_by_description=True
    )
    endpoint = MagicMock()
    endpoint.complete.return_value = "yes"
    flags, _ = grade_candidates("Where is the spindle?", [evidence], endpoint)
    assert flags == [True]
    grade = json.loads(endpoint.complete.call_args.args[1][1]["content"])
    assert grade == {
        "question": "Where is the spindle?",
        "passage": figure.caption,
        "figure_label": figure.label,
    }
    groups = group_evidence([evidence], flags, "en")
    assert "description" not in groups[0]["evidence"][0]
    assert not groups[0]["evidence"][0]["found_by_description"]
    assert figure.description not in json.dumps(groups)
    # Even a raw evidence dictionary containing a description cannot leak it.
    prompt = build_answer_prompt(
        "Synthetic question?", "en", [{**asdict(evidence), "number": 1}]
    )
    assert figure.description not in json.dumps(prompt)
    assert figure.caption in prompt[1]["content"]


def test_same_book_explicit_references_only(tmp_path):
    figure = figure_entries(extraction(), tmp_path)[0]
    other = replace(figure, book_id="other-book", id="other-id")
    passage = make_passage(1, text="See Figura 3–12 and Fig. 3-12.")
    node = passage_nodes([passage], "Title", [figure, other])[0]
    assert node.metadata["figures"] == [figure.id]
    assert (
        "figures"
        not in passage_nodes(
            [make_passage(1, text="A value of 3-12.")], "Title", [figure]
        )[0].metadata
    )


@pytest.mark.parametrize("version", ["v5", "v6", "v10"])
def test_v5_needs_no_descriptions_old_versions_unchanged(
    tmp_path, monkeypatch, version
):
    source = extraction()
    source.passages = [make_passage(1)]
    monkeypatch.setenv("INDEX_VERSION", "v4")
    assert len(indexed_nodes(source, "Title")) == 1
    monkeypatch.setenv("INDEX_VERSION", version)
    monkeypatch.setattr(
        "med_ask.figures.load_description",
        MagicMock(side_effect=AssertionError("must not read")),
    )
    nodes = indexed_nodes(source, "Title")
    assert len(nodes) == 2 and nodes[1].metadata["kind"] == "figure"
    assert model_table("synthetic", True, "embed-large-bulk") == model_table(
        "synthetic", True, "embed-large"
    )


@pytest.mark.parametrize("version", ["v5", "v6", "v10"])
def test_search_preserves_ranks_and_attaches_figures(tmp_path, monkeypatch, version):
    monkeypatch.setenv("INDEX_VERSION", version)
    figure = replace(
        figure_entries(extraction(), tmp_path)[0], description="Generated spindle."
    )
    foreign = replace(figure, book_id="other-book", id="foreign-id")
    passage = make_passage(1, text="See Figure 3-12.")
    node = passage_nodes([passage], "Title", [figure])[0]
    fnode = figure_nodes([figure], "Title")[0]
    foreign_node = figure_nodes([foreign], "Other title")[0]
    store, index, database = MagicMock(), MagicMock(), MagicMock()

    async def close():
        pass

    store.close = close
    store.get_nodes.return_value = [fnode, foreign_node, fnode]
    index.as_retriever.return_value.retrieve.side_effect = [
        [MagicMock(node=node, score=0.9)],
        [],
    ]
    monkeypatch.setattr(
        "med_ask.retrieval.VectorStoreIndex.from_vector_store", lambda *a, **k: index
    )
    monkeypatch.setattr("med_ask.retrieval.neighbouring_passages", lambda *a: [])
    database.model.return_value = ("synthetic-v1", 3, True)
    results, _, _ = search(
        "Where is the spindle?", FakeEndpoint(), database, lambda *a: store
    )
    assert [r.id for r in results] == [node.node_id, figure.id]
    assert results[1].text == figure.caption and not results[1].found_by_description
    assert index.as_retriever.call_args_list[0].kwargs["filters"].filters[0].value == [
        "content",
        "summary",
        "glossary",
    ]
    assert index.as_retriever.call_args.kwargs["filters"].filters[0].value == "figure"
    # A figure returned directly is not appended twice by a linked passage.
    index.as_retriever.return_value.retrieve.side_effect = [
        [MagicMock(node=node, score=0.9)],
        [MagicMock(node=fnode, score=0.99)],
    ]
    results, _, _ = search(
        "Synthetic question?", FakeEndpoint(), database, lambda *a: store
    )
    assert [r.id for r in results] == [node.node_id, figure.id]


@pytest.mark.parametrize("figure_limit", [4, 6])
def test_separate_search_preserves_passages_and_bounds_both_figure_sources(
    tmp_path, monkeypatch, figure_limit
):
    monkeypatch.setenv("INDEX_VERSION", "v5")
    if figure_limit == 4:
        monkeypatch.delenv("FIGURE_CANDIDATES", raising=False)
    else:
        monkeypatch.setenv("FIGURE_CANDIDATES", str(figure_limit))
    figure = figure_entries(extraction(), tmp_path)[0]
    direct = figure_nodes(
        [replace(figure, id=f"direct-{i}") for i in range(10)], "Title"
    )
    linked = figure_nodes(
        [replace(figure, id=f"linked-{i}") for i in range(10)], "Title"
    )
    passages = passage_nodes([make_passage(i) for i in range(26)], "Title")
    passages[0].metadata["figures"] = [n.node_id for n in linked] + [direct[0].node_id]
    store, index, database = MagicMock(), MagicMock(), MagicMock()

    async def close():
        pass

    store.close = close
    store.get_nodes.return_value = linked + [direct[0]]
    index.as_retriever.return_value.retrieve.side_effect = [
        [MagicMock(node=n, score=0.5) for n in passages],
        [MagicMock(node=n, score=0.99) for n in direct],
    ]
    monkeypatch.setattr(
        "med_ask.retrieval.VectorStoreIndex.from_vector_store", lambda *a, **k: index
    )
    monkeypatch.setattr("med_ask.retrieval.neighbouring_passages", lambda *a: [])
    database.model.return_value = ("synthetic-v1", 3, True)
    endpoint = FakeEndpoint()
    results, _, _ = search(
        "Synthetic question?", endpoint, database, lambda *a: store, k=26
    )
    half = figure_limit // 2
    assert [r.id for r in results[:26]] == [n.node_id for n in passages]
    assert [r.id for r in results[26:]] == [
        n.node_id for n in direct[:half] + linked[:half]
    ]
    assert len(results) == 26 + figure_limit
    assert len({r.id for r in results}) == len(results)
    assert index.as_retriever.call_args_list[0].kwargs["similarity_top_k"] == 26
    assert (
        index.as_retriever.call_args_list[1].kwargs["similarity_top_k"] == figure_limit
    )
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize("versions", [("v4", "v5"), ("v5", "v6")])
def test_figure_gate_preserves_both_hit10_totals(versions):
    def run(version):
        return {
            "set_version": 2,
            "table": f"e_{version}_synthetic",
            "purpose": "embed-large",
            "roles": False,
            "ranking_scope": "passages",
            "questions": [
                dict(id=f"q{n:03}", scored=True, hit5=n < 29, hit10=True)
                for n in range(32)
            ],
        }

    baseline, candidate = (run(version) for version in versions)
    cohort = [q["id"] for q in baseline["questions"][:20]]
    result = figure_gate(baseline, candidate, cohort)
    assert result["passed"] and result[versions[1]]["overall"] == dict(
        n=32, hit5=29, hit10=32
    )
    candidate["questions"][0]["hit10"] = False
    assert not figure_gate(baseline, candidate, cohort)["passed"]
    with pytest.raises(EvalError):
        figure_gate(baseline, candidate, cohort[:-1])
    if versions == ("v5", "v6"):
        candidate.pop("ranking_scope")
        with pytest.raises(EvalError):
            figure_gate(baseline, candidate, cohort)
