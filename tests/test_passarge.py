"""Atlas structure generated at runtime; no real pages or captions in git."""

from dataclasses import replace
from unittest.mock import MagicMock

import pymupdf
import pytest
from test_retrieval import make_passage

from med_ask.extract import Extraction, PageAccount
from med_ask.figures import PASSARGE_PLATES, describe_figures, figure_entries
from med_ask.generation import build_answer_prompt, group_evidence
from med_ask.retrieval import evidence_from_node, figure_nodes, passage_nodes


@pytest.fixture
def atlas(tmp_path):
    path = tmp_path / "synthetic-atlas.pdf"
    with pymupdf.open() as pdf:
        for number in range(1, 427):
            page = pdf.new_page(width=400, height=600)
            page.insert_text((20, 25), str(number))
            page.insert_text((75, 25), f"Synthetic plate {number}")
            page.insert_text((20, 90), f"Printed legend for synthetic page {number}.")
            page.insert_text((20, 120), "A. A synthetic panel")
            page.insert_text((20, 145), "Printed source text for the panel.")
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
    assert len(figures) == 198
    assert tuple(f.pdf_page for f in figures) == PASSARGE_PLATES
    assert not {15, 16, 17, 18, 244, 274} & {f.pdf_page for f in figures}
    assert len({f.id for f in figures}) == 198
    first = figures[0]
    assert first.label == "Synthetic plate 32"
    assert first.legend_pdf_page == 31 and first.legend_printed_page == "31"
    assert first.printed_page == "32"
    assert "legend for synthetic page 31" in first.caption
    assert "Printed source text" in first.caption
    assert "publisher footer" not in first.caption
    assert "Synthetic plate 31" not in first.caption
    assert all(f.legend_pdf_page == 421 for f in figures[-5:])
    assert len({f.caption for f in figures[-5:]}) == 1
    assert all(f.label == "Synthetic plate 422" for f in figures[-5:])
    assert figures == figure_entries(extraction, tmp_path / "figures", pdf_path=path)


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
