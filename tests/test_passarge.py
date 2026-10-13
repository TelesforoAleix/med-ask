"""Atlas structure generated at runtime; no real pages or captions in git."""

from dataclasses import replace
from unittest.mock import MagicMock

import pymupdf
import pytest
from test_retrieval import make_passage

from med_ask.extract import Extraction, PageAccount
from med_ask.figures import (
    PASSARGE_PLATES,
    describe_figures,
    figure_entries,
    passarge_panels,
)
from med_ask.generation import build_answer_prompt, group_evidence
from med_ask.retrieval import (
    evidence_from_node,
    figure_nodes,
    figures_enabled,
    indexed_nodes,
    passage_nodes,
)


@pytest.mark.parametrize(
    "version,enabled",
    [("v4", False), ("v5", True), ("v6", True), ("v10", True), ("v500", True)],
)
def test_numeric_figure_version_rule(version, enabled):
    assert figures_enabled(version) is enabled


@pytest.mark.parametrize("version,count", [("v4", 1), ("v5", 199), ("v6", 391)])
def test_indexed_panels_are_v6_only(atlas, monkeypatch, version, count):
    path, extraction = atlas
    passage = replace(
        make_passage(1, text="Printed source text for the panel."),
        book_id="passarge",
        pdf_pages=(31, 31),
    )
    extraction.passages = [passage]
    monkeypatch.setenv("INDEX_VERSION", version)
    nodes = indexed_nodes(extraction, "Title", path)
    assert len(nodes) == count
    assert nodes[0].text == passage.text
    assert ("figures" in nodes[0].metadata) is (version == "v6")


@pytest.fixture
def atlas(tmp_path):
    path = tmp_path / "synthetic-atlas.pdf"
    with pymupdf.open() as pdf:
        for number in range(1, 427):
            page = pdf.new_page(width=400, height=600)
            page.insert_text((20, 25), str(number))
            page.insert_text((75, 25), f"Synthetic plate {number}")
            page.insert_text((20, 90), f"Printed legend for synthetic page {number}.")
            if number != 199:
                page.insert_text((20, 120), "A. A synthetic panel", fontname="hebo")
                page.insert_text(
                    (20, 145), "Printed source text for the panel. " * 2, fontsize=7
                )
                page.insert_text((20, 200), "B. Another panel", fontname="hebo")
                page.insert_text((20, 225), "Different synthetic source paragraph.")
            page.insert_text((20, 585), "Synthetic publisher footer")
        pdf.save(path)
    extraction = Extraction(
        "passarge",
        "English",
        [],
        [],
        [
            PageAccount(n, True, str(n), "read", None, "", "born-digital")
            for n in range(1, 427)
        ],
    )
    return path, extraction


def test_exact_set_titles_legends_and_two_locations(atlas, tmp_path):
    path, extraction = atlas
    figures = figure_entries(extraction, tmp_path / "figures", pdf_path=path)
    assert len(figures) == 390
    assert tuple(dict.fromkeys(f.pdf_page for f in figures)) == PASSARGE_PLATES
    assert not {15, 16, 17, 18, 244, 274} & {f.pdf_page for f in figures}
    assert len({f.id for f in figures}) == 390
    first = figures[0]
    assert first.label == "Synthetic plate 32 A"
    assert first.legend_pdf_page == 31 and first.legend_printed_page == "31"
    assert first.printed_page == "32"
    assert "legend for synthetic page 31" not in first.caption
    assert "Printed source text" in first.caption
    assert "publisher footer" not in first.caption
    assert "Synthetic plate 31" not in first.caption
    assert all(f.legend_pdf_page == 421 for f in figures[-5:])
    assert len({f.caption for f in figures[-5:]}) == 1
    assert all(f.label == "Synthetic plate 422" for f in figures[-5:])
    assert figures == figure_entries(extraction, tmp_path / "figures", pdf_path=path)
    assert sum(f.panel_letter is not None for f in figures) == 384
    whole = [f for f in figures if not f.panel_letter]
    assert [f.pdf_page for f in whole] == [200, 422, 423, 424, 425, 426]
    assert all(not f.description for f in figures)
    old = figure_entries(extraction, pdf_path=path, panels=False)
    assert len(old) == 198 and old[0].label == "Synthetic plate 32"
    assert old[-5:] == figures[-5:] or all(
        a.id == b.id for a, b in zip(old[-5:], figures[-5:], strict=True)
    )


def test_panel_column_order_opening_bounds_and_references():
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=600)
        page.insert_text((20, 70), "Brief opening.")
        page.insert_text((20, 140), "A. First panel", fontname="hebo")
        page.insert_text(
            (20, 165), "Synthetic paragraph with enough detail. " * 2, fontsize=4
        )
        # The right column starts above A: it must still follow A's paragraph.
        page.insert_text((220, 60), "B. Second panel", fontname="hebo")
        page.insert_text((220, 90), "Short paragraph.")
        page.insert_text((220, 140), "References", fontname="hebo")
        page.insert_text(
            (220, 165), "Synthetic reference excluded from the panel.", fontsize=4
        )
        parts = passarge_panels(page)
        assert [p[0] for p in parts] == ["A", "B"]
        assert parts[0][1].startswith("Brief opening.")
        assert parts[1][1].startswith("B.")  # Opening would dominate this panel.
        assert "reference excluded" not in parts[1][1]
        assert "Second panel" not in parts[0][1]
        assert all(p[3] for p in parts)


def test_long_opening_is_excluded():
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=600)
        page.insert_textbox((20, 60, 380, 150), "Synthetic opening. " * 15, fontsize=8)
        page.insert_text((20, 180), "A. Panel", fontname="hebo")
        page.insert_textbox(
            (20, 210, 190, 550), "Synthetic paragraph. " * 60, fontsize=8
        )
        parts = passarge_panels(page)
        assert len(parts) == 1 and parts[0][1].startswith("A.")


def test_legend_passage_owns_panel_or_brings_all_in_letter_order(atlas):
    path, extraction = atlas
    figures = figure_entries(extraction, pdf_path=path)
    panels = figures[:2]
    passage = replace(
        make_passage(1, text="Printed source text for the panel."),
        book_id="passarge",
        pdf_pages=(31, 31),
    )
    node = passage_nodes([passage], "Title", figures)[0]
    assert node.metadata["figures"] == [panels[0].id]
    assert node.text == passage.text
    # Spatial ownership also works when the passage does not repeat its heading.
    passage.text = "Synthetic differently joined paragraph fragment."
    passage.metadata = {"locations": [dict(pdf_page=31, bbox=(20, 130, 390, 150))]}
    assert passage_nodes([passage], "Title", figures)[0].metadata["figures"] == [
        panels[0].id
    ]
    passage.metadata = {}
    assert passage_nodes([passage], "Title", figures)[0].metadata["figures"] == [
        p.id for p in panels
    ]
    passage.metadata = {"locations": [dict(pdf_page=31, bbox=(20, 100, 390, 250))]}
    assert passage_nodes([passage], "Title", figures)[0].metadata["figures"] == [
        p.id for p in panels
    ]
    passage.pdf_pages = (199, 199)
    assert len(passage_nodes([passage], "Title", figures)[0].metadata["figures"]) == 1
    passage.pdf_pages = (421, 421)
    assert len(passage_nodes([passage], "Title", figures)[0].metadata["figures"]) == 5


def test_legend_is_cited_and_plate_is_described(atlas, tmp_path):
    from med_ask.manifest import Book

    path, extraction = atlas
    first = figure_entries(extraction, tmp_path / "figures", pdf_path=path)[0]
    node = figure_nodes(
        [replace(first, description="Generated synthetic structures.")], "Title"
    )[0]
    evidence = evidence_from_node(node)
    assert evidence.pdf_pages == (31, 31) and evidence.printed_pages == ("31", "31")
    assert evidence.figure_pdf_page == 32 and evidence.figure_printed_page == "32"
    assert "pdf page 31" in evidence.label
    displayed = group_evidence([evidence], [True], "en")[0]["evidence"]
    assert "description" not in displayed[0]
    prompt = str(build_answer_prompt("Synthetic question?", "en", displayed))
    assert (
        "Printed source text" in prompt
        and "Generated synthetic structures" not in prompt
    )
    rendered = []

    class Page:
        def get_pixmap(self, **kwargs):
            return MagicMock(tobytes=lambda format: b"synthetic plate PNG")

    class PDF:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __getitem__(self, index):
            rendered.append(index + 1)
            return Page()

    endpoint = MagicMock()
    endpoint.describe.return_value = dict(
        text="Generated synthetic structures.", finish_reason="stop"
    )
    book = Book("passarge", path.name, "Title", "English", path)
    from unittest.mock import patch

    with patch("med_ask.figures.pymupdf.open", return_value=PDF()):
        describe_figures(
            book, [first], endpoint, tmp_path / "figures", output=lambda *a, **k: None
        )
    assert rendered == [32]
    assert endpoint.describe.call_args.args[0] == b"synthetic plate PNG"
    assert "Synthetic plate 32" in endpoint.describe.call_args.args[1]
    again = figure_entries(
        extraction, tmp_path / "figures", pdf_path=path, load_descriptions=True
    )[0]
    assert again.description == "Generated synthetic structures."


def test_plate_title_reference_links_only_its_book(atlas, tmp_path):
    path, extraction = atlas
    first = figure_entries(extraction, tmp_path / "figures", pdf_path=path)[0]
    passage = make_passage(1, text="See Synthetic plate 32 for these structures.")
    passage.book_id = "passarge"
    other = replace(first, id="other", book_id="another-book")
    assert passage_nodes([passage], "Title", [first, other])[0].metadata["figures"] == [
        first.id
    ]


def test_wrong_atlas_or_missing_path_refuses(atlas, tmp_path):
    path, extraction = atlas
    with pytest.raises(ValueError, match="PDF path"):
        figure_entries(extraction, tmp_path)
    short = tmp_path / "short.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(short)
    with pytest.raises(ValueError, match="settled plate pages"):
        figure_entries(extraction, tmp_path, pdf_path=short)
