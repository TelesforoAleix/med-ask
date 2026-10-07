"""Exercise layout and page provenance using PDFs created only at runtime."""

import pymupdf
import pytest

from med_ask.extract import extract_book, main, passage_label, report, sample_pages


@pytest.fixture
def pdf(tmp_path):
    def make(pages, labels=None, toc=None):
        path = tmp_path / "synthetic.pdf"
        with pymupdf.open() as doc:
            for texts in pages:
                page = doc.new_page(width=600, height=800)
                page.insert_font(
                    fontname="synthetic", fontbuffer=pymupdf.Font("helv").buffer
                )
                for x, y, text, size in texts:
                    page.insert_text((x, y), text, fontsize=size, fontname="synthetic")
            if labels:
                doc.set_page_labels(labels)
            if toc:
                doc.set_toc(toc)
            doc.save(path)
        return path

    return make


def body(text, y=100, x=50, size=11):
    return x, y, text, size


def header(number):
    return 550, 30, str(number), 11


def extract(path):
    return extract_book(path, "synthetic", "en")


def test_cross_page_paragraph_and_label(pdf):
    result = extract(
        pdf(
            [
                [
                    header(10),
                    body("A synthetic paragraph continues over the page", 730),
                ],
                [header(11), body("and finishes on the following page.")],
            ]
        )
    )
    assert len(result.passages) == 1
    passage = result.passages[0]
    assert passage.pdf_pages == (1, 2)
    assert passage.printed_pages == ("10", "11")
    assert passage.order == 1 and passage.book_id == "synthetic"
    assert passage.language == "en"
    assert passage.text.endswith("following page.")
    assert len(passage.metadata["locations"]) == 2
    passage.metadata["future_enrichment"] = True
    assert passage_label(passage) == ("synthetic: pdf pages 1–2 <print pages: 10–11>")


@pytest.mark.parametrize("footer", [False, True])
def test_visible_numbers_override_wrong_labels(pdf, footer):
    numbers = [(550, 785 if footer else 30, str(n), 11) for n in (20, 21)]
    result = extract(
        pdf(
            [
                [numbers[0], body("The first synthetic paragraph is complete.")],
                [numbers[1], body("The second synthetic paragraph is complete.")],
            ],
            labels=[{"startpage": 0, "style": "D", "firstpagenum": 100}],
        )
    )
    assert [p.printed_page for p in result.pages] == ["20", "21"]
    assert all(p.printed_page_method == "read" for p in result.pages)
    assert "'disagree': 2" in report(result, [])
    assert all(p.printed_pages[0] != "100" for p in result.passages)


def test_labels_alone_never_supply_printed_pages(pdf):
    result = extract(
        pdf(
            [[body("A synthetic paragraph ends here.")]],
            labels=[{"startpage": 0, "style": "D"}],
        )
    )
    assert result.pages[0].printed_page is None
    assert result.pages[0].reason == "no-header-number"


def test_numberless_page_inferred_between_accepted_neighbours(pdf):
    result = extract(
        pdf(
            [
                [header(40), body("The first synthetic paragraph is complete.")],
                [body("The middle synthetic paragraph is complete.")],
                [header(42), body("The final synthetic paragraph is complete.")],
            ]
        )
    )
    assert [p.printed_page for p in result.pages] == ["40", "41", "42"]
    assert result.pages[1].printed_page_method == "inferred"
    assert result.pages[1].reason is None


@pytest.mark.parametrize(
    "numbers, methods, reasons",
    [
        ([10, 99, 12], ["none", "none", "none"], ["out-of-sequence"] * 3),
        ([None, None, None], ["none"] * 3, ["no-header-number"] * 3),
        ([10, 11, 99], ["read", "read", "none"], [None, None, "out-of-sequence"]),
        ([None, 11, 12], ["none", "read", "read"], ["no-header-number", None, None]),
    ],
)
def test_unverified_numbers_have_reasons(pdf, numbers, methods, reasons):
    result = extract(
        pdf(
            [
                ([header(n)] if n is not None else [])
                + [body("A standalone synthetic paragraph ends here.")]
                for n in numbers
            ]
        )
    )
    assert [p.printed_page_method for p in result.pages] == methods
    assert [p.reason for p in result.pages] == reasons
    for passage in result.passages:
        if passage.printed_pages is None:
            assert passage.printed_page_reason


def test_running_heads_and_repeated_footer_dropped(pdf):
    result = extract(
        pdf(
            [
                [
                    body("Synthetic running head", 30),
                    header(n),
                    body("A standalone synthetic paragraph ends here."),
                    body("Repeated synthetic footer", 785),
                ]
                for n in (1, 2, 3)
            ]
        )
    )
    assert len(result.passages) == 3
    assert all(
        "running" not in p.text and "footer" not in p.text for p in result.passages
    )


@pytest.mark.parametrize("prefix", ["Figure", "Fig.", "Figura", "FIGURA"])
def test_caption_is_separate(pdf, prefix):
    result = extract(
        pdf(
            [
                [
                    body("A standalone synthetic paragraph ends here."),
                    body(f"{prefix} 2–3 Synthetic diagram caption.", 250, size=10),
                ]
            ]
        )
    )
    assert len(result.passages) == 1
    assert len(result.captions) == 1
    assert result.captions[0].identifier == "2–3"
    assert result.captions[0].pdf_page == 1


def test_heading_and_bookmark_paths(pdf):
    result = extract(
        pdf(
            [
                [
                    body("Synthetic chapter", 90, size=18),
                    body("A standalone synthetic paragraph ends here.", 140),
                    body("Synthetic subsection", 200, size=14),
                    body("Another standalone synthetic paragraph ends here.", 240),
                ]
            ],
            toc=[[1, "Synthetic bookmark", 1]],
        )
    )
    assert len(result.passages) == 2
    assert result.passages[0].section_path == (
        "Synthetic bookmark",
        "Synthetic chapter",
    )
    assert result.passages[1].section_path == (
        "Synthetic bookmark",
        "Synthetic chapter",
        "Synthetic subsection",
    )


def test_empty_page_breaks_continuation(pdf):
    result = extract(
        pdf(
            [
                [body("An unfinished synthetic sentence")],
                [],
                [body("another synthetic paragraph ends here.")],
            ]
        )
    )
    assert not result.pages[1].has_text
    assert len(result.passages) == 2
    assert [p.pdf_pages for p in result.passages] == [(1, 1), (3, 3)]


def test_new_section_and_indentation_prevent_join(pdf):
    result = extract(
        pdf(
            [
                [body("An unfinished synthetic sentence")],
                [
                    body("New synthetic section", size=16),
                    body("A fresh synthetic paragraph follows.", 140),
                ],
            ]
        )
    )
    assert len(result.passages) == 2


def test_roman_numbers_and_inference(pdf):
    result = extract(
        pdf(
            [
                [body("iv", 30), body("A standalone synthetic paragraph ends here.")],
                [body("A standalone synthetic paragraph ends here.")],
                [body("vi", 30), body("A standalone synthetic paragraph ends here.")],
            ]
        )
    )
    assert [p.printed_page for p in result.pages] == ["iv", "v", "vi"]


def test_sample_is_reproducible_and_contains_crossing(pdf):
    result = extract(
        pdf(
            [
                [body("The synthetic paragraph continues")],
                [body("and finishes here.")],
                *[
                    [body("A standalone synthetic paragraph ends here.")]
                    for _ in range(5)
                ],
            ]
        )
    )
    pages = sample_pages(result, 4, 17)
    assert pages == sample_pages(result, 4, 17)
    assert 1 in pages and 2 in pages
    assert len(pages) == 4
    assert sample_pages(result, 0, 0) == []


def test_cli_writes_only_when_output_requested(pdf, monkeypatch, capsys, tmp_path):
    path = pdf([[body("A standalone synthetic paragraph ends here.")]])
    monkeypatch.setattr(
        "sys.argv",
        ["extract", str(path), "--book", "synthetic", "--lang", "en", "--sample", "1"],
    )
    before = set(tmp_path.iterdir())
    assert main() == 0
    assert set(tmp_path.iterdir()) == before
    assert "Printed pages: read 0; inferred 0; none 1" in capsys.readouterr().out
    output = tmp_path / "output"
    monkeypatch.setattr(
        "sys.argv",
        [
            "extract",
            str(path),
            "--book",
            "synthetic",
            "--lang",
            "en",
            "--output",
            str(output),
        ],
    )
    assert main() == 0
    assert (output / "report.txt").is_file()
    assert (output / "page-1.png").is_file()
    assert all(p.parent == output for p in output.iterdir())


def test_no_text_page_yields_no_passages(pdf):
    result = extract(pdf([[]]))
    assert not result.passages and not result.captions
    assert not result.pages[0].has_text
    assert result.pages[0].reason == "no-header-number"


def test_scan_provenance_and_mixed_page_passage(tmp_path):
    path = tmp_path / "synthetic-scan.pdf"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 800))
    pix.clear_with(245)
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
        page.insert_text(
            (50, 700),
            "A synthetic paragraph continues across the page",
            fontsize=11,
            render_mode=3,
        )
        page = doc.new_page(width=600, height=800)
        page.insert_text(
            (50, 100), "and ends in a born-digital text layer.", fontsize=11
        )
        doc.save(path)
    result = extract(path)
    assert [p.text_source for p in result.pages] == ["inherited-ocr", "born-digital"]
    assert len(result.passages) == 1
    assert result.passages[0].inherited_ocr
    assert result.passages[0].metadata["page_text_sources"] == {
        1: "inherited-ocr",
        2: "born-digital",
    }
    assert "inherited-OCR pages 1; inherited-OCR passages 1" in report(result, [])


@pytest.mark.parametrize("raster", [True, False])
def test_figure_labels_are_never_passages(tmp_path, raster):
    path = tmp_path / "synthetic-figure.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        box = pymupdf.Rect(350, 100, 550, 300)
        if raster:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200))
            pix.clear_with(240)
            page.insert_image(box, stream=pix.tobytes("png"))
        else:
            page.draw_rect(box)
        page.insert_text((360, 150), "A synthetic figure label.", fontsize=11)
        page.insert_text(
            (50, 400), "A standalone synthetic paragraph ends here.", fontsize=11
        )
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert "figure label" not in result.passages[0].text
    assert result.pages[0].text_source == "born-digital"


def test_scan_without_layer_is_no_text(tmp_path):
    path = tmp_path / "synthetic-empty-scan.pdf"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 800))
    pix.clear_with(245)
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
        doc.save(path)
    result = extract(path)
    assert result.pages[0].text_source == "no-text"
    assert not result.passages


def test_visible_text_on_background_image_is_born_digital(tmp_path):
    path = tmp_path / "synthetic-background.pdf"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 800))
    pix.clear_with(245)
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
        page.insert_text((50, 100), "A standalone synthetic paragraph ends here.")
        doc.save(path)
    assert extract(path).pages[0].text_source == "born-digital"


def test_invisible_line_fragments_reassembled(tmp_path):
    path = tmp_path / "synthetic-fragments.pdf"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 800))
    pix.clear_with(245)
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
        for y, text in [
            (100, "A synthetic paragraph begins with a long line"),
            (114, "and continues in a separate invisible text block"),
            (128, "before ending here."),
        ]:
            page.insert_text((50, y), text, fontsize=11, render_mode=3)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert result.passages[0].inherited_ocr
    assert result.passages[0].text.endswith("before ending here.")


def test_prefixed_visible_numbers_ignore_pdf_labels(pdf):
    result = extract(
        pdf(
            [
                [body("G:5", 30), body("A standalone synthetic paragraph ends here.")],
                [body("G:6", 30), body("A standalone synthetic paragraph ends here.")],
            ],
            labels=[{"startpage": 0, "style": "D", "firstpagenum": 1}],
        )
    )
    assert [p.printed_page for p in result.pages] == ["G:5", "G:6"]


def test_cross_column_paragraph_stays_one_passage(pdf):
    result = extract(
        pdf(
            [
                [
                    body("A synthetic paragraph continues into the next column", 700),
                    body("and finishes at the top of that column.", 100, x=320),
                ]
            ]
        )
    )
    assert len(result.passages) == 1
    assert result.passages[0].pdf_pages == (1, 1)
    assert len(result.passages[0].metadata["locations"]) == 2


def test_hanging_indent_reference_stays_one_paragraph(tmp_path):
    path = tmp_path / "synthetic-hanging-indent.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text(
            (50, 100), "Synthetic Author. A synthetic reference.", fontsize=11
        )
        page.insert_text(
            (65, 114), "A continuation with hanging indentation.", fontsize=11
        )
        page.insert_text((65, 128), "The synthetic reference ends here.", fontsize=11)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1


def test_indentation_prevents_cross_page_join(pdf):
    result = extract(
        pdf(
            [
                [body("A synthetic paragraph is unfinished at this point")],
                [
                    body("A new synthetic paragraph begins separately", 100, x=65),
                    body("and ends here.", 114, x=50),
                ],
            ]
        )
    )
    assert all(p.pdf_pages[0] == p.pdf_pages[1] for p in result.passages)


def test_cli_rejects_output_inside_git_checkout(pdf, tmp_path, monkeypatch):
    path = pdf([[body("A standalone synthetic paragraph ends here.")]])
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: synthetic")
    output = checkout / "reports"
    monkeypatch.setattr(
        "sys.argv",
        [
            "extract",
            str(path),
            "--book",
            "synthetic",
            "--lang",
            "en",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not output.exists()


def test_numbered_question_with_bold_first_line_is_a_paragraph(tmp_path):
    path = tmp_path / "synthetic-question.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text(
            (50, 100),
            "1-2 A synthetic question begins with bold text",
            fontsize=11,
            fontname="hebo",
        )
        page.insert_text(
            (50, 114), "and continues in normal text to its end?", fontsize=11
        )
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert result.passages[0].text.startswith("1-2 A synthetic question")
    assert result.passages[0].text.endswith("to its end?")


def test_ocr_indented_first_line_and_following_lines_stay_one_paragraph(tmp_path):
    path = tmp_path / "synthetic-indented-ocr.pdf"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 800))
    pix.clear_with(245)
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
        page.insert_text(
            (65, 100),
            "An indented synthetic paragraph begins here",
            fontsize=11,
            render_mode=3,
        )
        page.insert_text(
            (50, 114),
            "and continues in another OCR block to its end.",
            fontsize=11,
            render_mode=3,
        )
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert result.passages[0].inherited_ocr


def test_reference_start_at_end_of_pdf_block_is_preserved(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text(
            (50, 100), "Synthetic author, A.: First reference", fontsize=10
        )
        page.insert_text((65, 112), "Synthetic journal ends here.", fontsize=10)
        page.insert_text(
            (50, 124), "Synthetic author, B.: Second reference", fontsize=10
        )
        page.insert_text((65, 136), "Another synthetic journal ends here.", fontsize=10)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 2
    assert result.passages[0].text.endswith("journal ends here.")
    assert result.passages[1].text.startswith("Synthetic author, B.")


def test_shaded_table_cells_do_not_become_paragraphs(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.draw_rect(pymupdf.Rect(50, 180, 550, 300), fill=(0.8, 0.8, 0.9))
        for y in (210, 240, 270):
            for x in (60, 120, 230):
                page.insert_text((x, y), "Synthetic cell", fontsize=10)
        page.insert_text(
            (50, 400), "A synthetic body paragraph ends here.", fontsize=11
        )
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert "cell" not in result.passages[0].text


def test_caption_continuation_blocks_remain_a_caption(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text(
            (50, 100), "A synthetic body paragraph ends here.", fontsize=11
        )
        page.insert_text((350, 200), "Figure 2 Synthetic caption starts.", fontsize=10)
        page.insert_text((350, 215), "A second caption paragraph follows.", fontsize=10)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert len(result.captions) == 1
    assert "second caption" in result.captions[0].text


def test_glossary_terms_are_kept_with_their_definitions(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((50, 100), "Synthetic term", fontname="hebo", fontsize=11)
        page.insert_text((65, 114), "A synthetic definition ends here.", fontsize=11)
        page.insert_text((50, 138), "Another term", fontname="hebo", fontsize=11)
        page.insert_text(
            (65, 152), "Another synthetic definition ends here.", fontsize=11
        )
        doc.set_toc([[1, "Glossary", 1]])
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 2
    assert result.passages[0].text.startswith("Synthetic term A synthetic")
    assert result.passages[1].text.startswith("Another term Another synthetic")
    assert all(p.section_path == ("Glossary",) for p in result.passages)


def test_neighbouring_column_caption_fragments_do_not_enter_body(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text(
            (50, 100), "A synthetic body paragraph starts here", fontsize=11
        )
        page.insert_text((50, 112), "and ends in the main column.", fontsize=11)
        page.insert_text((400, 105), "Figure 3 Synthetic caption", fontsize=9)
        page.insert_text((400, 117), "continues beside the body.", fontsize=9)
        page.insert_text((400, 129), "Its final line ends here.", fontsize=9)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert "caption" not in result.passages[0].text
    assert len(result.captions) == 1
    assert result.captions[0].text.endswith("ends here.")


@pytest.mark.parametrize("new_entry", [True, False])
def test_glossary_page_break_distinguishes_terms_from_continuations(
    tmp_path, new_entry
):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        first = doc.new_page(width=600, height=800)
        first.insert_text((50, 100), "Synthetic term", fontname="hebo", fontsize=11)
        first.insert_text((65, 114), "A synthetic definition continues", fontsize=11)
        second = doc.new_page(width=600, height=800)
        if new_entry:
            second.insert_text((50, 60), "Another term", fontname="hebo", fontsize=11)
        second.insert_text(
            (65, 74 if new_entry else 60),
            "and the synthetic definition finishes on this page.",
            fontsize=11,
        )
        doc.set_toc([[1, "Glossary", 1]])
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == (2 if new_entry else 1)
    assert result.passages[-1].pdf_pages == ((2, 2) if new_entry else (1, 2))


def test_glossary_inline_definitions_keep_their_native_paragraphs(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_textbox(
            pymupdf.Rect(50, 100, 270, 180),
            "Synthetic term means a definition that wraps across several lines "
            "at the same margin and ends as one paragraph.",
            fontsize=11,
        )
        page.insert_textbox(
            pymupdf.Rect(50, 200, 270, 280),
            "Another term means another definition that also wraps across lines "
            "at the same margin and ends here.",
            fontsize=11,
        )
        doc.set_toc([[1, "Glossary", 1]])
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 2
    assert result.passages[0].text.endswith("one paragraph.")
    assert result.passages[1].text.endswith("ends here.")


def test_small_image_components_exclude_intervening_figure_labels(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 12, 12))
        pix.clear_with(240)
        for box in [(400, 100, 420, 120), (500, 110, 520, 125), (400, 170, 420, 180)]:
            page.insert_image(pymupdf.Rect(box), stream=pix.tobytes("png"))
        page.insert_text((430, 150), "Synthetic diagram label", fontsize=11)
        page.insert_text((50, 250), "A synthetic paragraph ends here.", fontsize=11)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert result.passages[0].text == "A synthetic paragraph ends here."


def test_figure_words_are_removed_from_a_mixed_body_line(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        text = "A synthetic paragraph ends here. Diagram label"
        boundary = 50 + pymupdf.get_text_length(
            "A synthetic paragraph ends here. ", fontsize=11
        )
        page.draw_rect(pymupdf.Rect(boundary, 90, 550, 150))
        page.insert_text((50, 110), text, fontsize=11)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert result.passages[0].text == "A synthetic paragraph ends here."


def test_short_photo_labels_below_images_are_not_passages(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200))
        pix.clear_with(240)
        page.insert_image(pymupdf.Rect(350, 100, 550, 300), stream=pix.tobytes("png"))
        page.insert_text((350, 314), "Synthetic photo label", fontsize=11)
        page.insert_text((50, 400), "A synthetic paragraph ends here.", fontsize=11)
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert result.passages[0].text == "A synthetic paragraph ends here."


def test_bold_glossary_line_is_part_of_its_inline_definition(tmp_path):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text(
            (50, 100), "Synthetic term with a long name", fontname="hebo", fontsize=11
        )
        page.insert_text((50, 114), "means a definition that ends here.", fontsize=11)
        doc.set_toc([[1, "Glossary", 1]])
        doc.save(path)
    result = extract(path)
    assert len(result.passages) == 1
    assert result.passages[0].text.endswith("ends here.")
