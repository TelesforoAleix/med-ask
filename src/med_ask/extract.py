"""Extract source paragraphs with kept OCR, without network access or default writes.

PDF positions are one-based inclusive ranges. Printed positions come exclusively
from visible margin numbers and independently verified neighbours. PDF labels are
retained for diagnostics only. Layout heuristics are deliberately inspectable.
"""

import argparse
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

import pymupdf

CAPTION = re.compile(
    r"^\s*(?:fig(?:ure|ura)?s?\.?|ilustraci[oó]n|abbildung)\s*"
    r"(?P<identifier>[A-Za-z]?\d+(?:[.\-–—]\d+)*(?:[A-Za-z])?)\b",
    re.IGNORECASE,
)
ROMAN = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
LIST_START = re.compile(
    r"^(?:\d+[.)]|\d+[–\-]\d+(?=\s+[A-Z])|[A-Za-z][.)]|\([A-Za-z]\))\s+"
)
END_SENTENCE = re.compile(r"[.!?…][\s\"'’”\)\]]*$")
TEXT_FLAGS = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
KIND_HEADINGS = {
    "summary": {"summary", "resumen"},
    "glossary": {"glossary", "glosario"},
    "exercise": {
        "problems",
        "problemas",
        "respuestas a problemas",
        "respuestas a los problemas",
    },
    "references": {
        "reference",
        "references",
        "bibliografía",
        "general references",
        "referencias generales",
        "selected introductory reading",
    },
}


def passage_kind(section_path: tuple[str, ...]) -> str:
    """1. Read headings from the nearest one upward, ignoring case and whitespace.
    2. Match the fixed book vocabulary, including headings with spaced letters.
    3. Return content when none of the headings is recognised.
    """
    for heading in reversed(section_path):
        normalized = "".join(heading.casefold().split())
        for kind, titles in KIND_HEADINGS.items():
            if normalized in {"".join(title.split()) for title in titles}:
                return kind
    return "content"


@dataclass
class Passage:
    book_id: str
    language: str
    pdf_pages: tuple[int, int]
    printed_pages: tuple[str, str] | None
    printed_page_reason: str | None
    section_path: tuple[str, ...]
    order: int
    text: str
    inherited_ocr: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: str = field(init=False)
    text_source: str = field(init=False)
    check_page: bool = False
    ocr_reasons: list[str] = field(default_factory=list)

    def __post_init__(self):
        """1. Assign the kind from its nearest recognised section heading.
        2. Record whether the original text layer came from inherited OCR.
        """
        self.kind = passage_kind(self.section_path)
        self.text_source = "inherited-ocr" if self.inherited_ocr else "born-digital"


@dataclass
class Caption:
    book_id: str
    language: str
    identifier: str
    pdf_page: int
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageAccount:
    pdf_page: int
    has_text: bool
    printed_page: str | None
    printed_page_method: str  # read, inferred, or none
    reason: str | None
    pdf_label: str
    text_source: str


@dataclass
class Extraction:
    book_id: str
    language: str
    passages: list[Passage]
    captions: list[Caption]
    pages: list[PageAccount]


@dataclass
class _Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool
    words: list[tuple[str, tuple[float, float, float, float]]] = field(
        default_factory=list
    )


@dataclass
class _Block:
    lines: list[_Line]
    bbox: tuple[float, float, float, float]
    size: float
    source: int

    @property
    def text(self) -> str:
        """1. Join the lines while repairing only line-ending hyphenation."""
        return _join_lines([line.text for line in self.lines])


def _join_lines(lines: list[str]) -> str:
    """1. Trim each line.
    2. Remove a hyphen between letters at a line break; join other lines with spaces.
    """
    text = ""
    for line in lines:
        line = line.strip()
        if text.endswith(("-", "\u00ad")) and line[:1].islower():
            text = text[:-1] + line
        else:
            text = (text + " " + line).strip()
    return text


def _number(text: str) -> tuple[str, int] | None:
    """1. Accept whole decimal, prefixed decimal, or well-formed roman tokens.
    2. Return their numbering family and value for sequence comparisons.
    """
    if re.fullmatch(r"[0-9]{1,5}", text):
        return "arabic", int(text)
    prefixed = re.fullmatch(r"([A-Za-z]{1,8}[:\-])(\d{1,5})", text)
    if prefixed:
        return "prefix:" + prefixed[1], int(prefixed[2])
    upper = text.upper()
    if not upper or not ROMAN.fullmatch(upper):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, previous = 0, 0
    for character in reversed(upper):
        value = values[character]
        total += -value if value < previous else value
        previous = max(value, previous)
    return "roman", total


def _format_number(kind: str, value: int, example: str) -> str:
    """1. Format decimal values directly.
    2. Preserve prefixes or build roman numerals in the accepted neighbour's case.
    """
    if kind == "arabic":
        return str(value)
    if kind.startswith("prefix:"):
        return kind.removeprefix("prefix:") + str(value)
    text = ""
    for amount, token in (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    ):
        count, value = divmod(value, amount)
        text += token * count
    return text.lower() if example.islower() else text


def _read_blocks(page: pymupdf.Page) -> list[_Block]:
    """1. Read positioned text blocks without loading image bytes.
    2. Keep word positions for removing labels mixed into a body line.
    3. Combine fragments on the same baseline and measure the dominant font.
    4. Rotate positions into the displayed page and keep each block identity.
    """
    blocks = []
    words = defaultdict(list)
    for word in page.get_text("words"):
        box = pymupdf.Rect(word[:4]) * page.rotation_matrix
        words[(word[5], word[6])].append((word[4], tuple(box)))
    for index, raw in enumerate(page.get_text("dict", flags=TEXT_FLAGS)["blocks"]):
        lines = []
        for line_index, raw_line in enumerate(raw.get("lines", [])):
            spans = [s for s in raw_line["spans"] if s["text"].strip()]
            if not spans:
                continue
            weights = Counter()
            for span in spans:
                weights[round(span["size"], 1)] += len(span["text"])
            box = pymupdf.Rect(raw_line["bbox"]) * page.rotation_matrix
            line = _Line(
                "".join(s["text"] for s in raw_line["spans"]).strip(),
                tuple(box),
                weights.most_common(1)[0][0],
                sum(len(s["text"]) for s in spans if s["flags"] & 16)
                > sum(len(s["text"]) for s in spans) / 2,
                words[(raw.get("number", index), line_index)],
            )
            if (
                lines
                and abs(lines[-1].bbox[1] - line.bbox[1]) < line.size * 0.35
                and -line.size * 0.25
                <= line.bbox[0] - lines[-1].bbox[2]
                < line.size * 1.2
            ):
                previous = lines[-1]
                previous.text += " " + line.text
                previous.words.extend(line.words)
                previous.bbox = (
                    min(previous.bbox[0], line.bbox[0]),
                    min(previous.bbox[1], line.bbox[1]),
                    max(previous.bbox[2], line.bbox[2]),
                    max(previous.bbox[3], line.bbox[3]),
                )
            else:
                lines.append(line)
        if lines:
            weights = Counter()
            for line in lines:
                weights[line.size] += len(line.text)
            blocks.append(
                _Block(
                    sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0])),
                    tuple(pymupdf.Rect(raw["bbox"]) * page.rotation_matrix),
                    weights.most_common(1)[0][0],
                    index,
                )
            )
    return blocks


def _edge(line: _Line, height: float) -> bool:
    """1. Identify lines wholly in the top 7.5% or starting in the bottom 5.5%."""
    return line.bbox[3] <= height * 0.075 or line.bbox[1] >= height * 0.945


def _margin_key(line: _Line) -> str:
    """1. Fold case and spacing and replace decimal numbers for repeated margins."""
    return re.sub(r"\d+", "#", " ".join(line.text.casefold().split()))


def _candidates(blocks: list[_Block], height: float) -> dict[tuple[str, int], str]:
    """1. Inspect only header and footer lines.
    2. Read whole numbers or tightly spaced individual digits, never PDF labels.
    """
    numbers = {}
    for block in blocks:
        for line in block.lines:
            if _edge(line, height):
                words = line.text.split()
                if (
                    1 < len(words) <= 5
                    and all(re.fullmatch(r"[0-9]", word) for word in words)
                    and line.bbox[2] - line.bbox[0] <= len(words) * line.size * 0.85
                ):
                    words = ["".join(words)]
                for word in (words[0], words[-1]):
                    parsed = _number(word)
                    if parsed:
                        numbers[parsed] = word
    return numbers


def _printed_pages(
    candidates: list[dict[tuple[str, int], str]],
) -> list[tuple[str | None, str, str | None]]:
    """1. Accept visible numbers corroborated by a neighbouring sequence.
    2. Bridge one numberless page only when both accepted neighbours differ by two.
    3. Give every remaining page a short reason, without consulting PDF labels.
    """
    decisions = []
    for index, numbers in enumerate(candidates):
        supported = []
        for (kind, value), text in numbers.items():
            for offset in (-2, -1, 1, 2):
                other = index + offset
                if not 0 <= other < len(candidates):
                    continue
                if abs(offset) == 2 and candidates[index + offset // 2]:
                    continue
                if (kind, value + offset) in candidates[other]:
                    supported.append(text)
                    break
        if len(supported) == 1:
            decisions.append((supported[0], "read", None))
        else:
            reason = (
                "ambiguous-header-number"
                if len(supported) > 1
                else "out-of-sequence"
                if numbers
                else "no-header-number"
            )
            decisions.append((None, "none", reason))
    accepted = decisions.copy()
    for index in range(1, len(candidates) - 1):
        if candidates[index]:
            continue
        left, right = accepted[index - 1][0], accepted[index + 1][0]
        if left is None or right is None:
            continue
        a, b = _number(left), _number(right)
        if a and b and a[0] == b[0] and b[1] == a[1] + 2:
            decisions[index] = (_format_number(a[0], a[1] + 1, left), "inferred", None)
    return decisions


def _block(lines: list[_Line], source: int) -> _Block:
    """1. Enclose the retained lines and use their character-weighted font size."""
    fonts = Counter()
    for line in lines:
        fonts[line.size] += len(line.text)
    box = (
        min(line.bbox[0] for line in lines),
        min(line.bbox[1] for line in lines),
        max(line.bbox[2] for line in lines),
        max(line.bbox[3] for line in lines),
    )
    return _Block(lines, box, fonts.most_common(1)[0][0], source)


def _paragraphs(block: _Block, split_titles: bool = True) -> list[_Block]:
    """1. Preserve the PDF block boundaries except for visible paragraph breaks.
    2. Split at column changes, titles, list items, spacing, and paragraph endings.
    3. Keep mixed scientific fonts within their paragraph.
    """
    groups: list[list[_Line]] = []
    gaps = [
        b.bbox[1] - a.bbox[3]
        for a, b in zip(block.lines, block.lines[1:], strict=False)
    ]
    typical_gap = median(gaps) if gaps else 0
    left = min(line.bbox[0] for line in block.lines)
    right = max(line.bbox[2] for line in block.lines)
    for index, line in enumerate(block.lines):
        split = False
        if groups:
            previous = groups[-1][-1]
            short = previous.bbox[2] < right - block.size * 1.7
            next_left = (
                block.lines[index + 1].bbox[0]
                if index + 1 < len(block.lines)
                else line.bbox[0]
            )
            indented = line.bbox[0] - next_left > block.size * 0.7
            at_left = line.bbox[0] - left < block.size * 0.4
            split = (
                abs(line.bbox[0] - previous.bbox[0]) > block.size * 8
                or (
                    split_titles
                    and previous.bold
                    and not line.bold
                    and not line.text[:1].islower()
                    and len(previous.text) < 100
                    and not END_SENTENCE.search(previous.text)
                    and (
                        not LIST_START.match(previous.text)
                        or previous.size >= line.size * 1.15
                    )
                )
                or (
                    at_left
                    and previous.bbox[0] - line.bbox[0] > block.size * 0.7
                    and (
                        END_SENTENCE.search(previous.text)
                        or len(groups[-1]) >= 2
                        or (previous.text[:1].islower() and line.text[:1].isupper())
                    )
                )
                or bool(LIST_START.match(line.text))
                or line.bbox[1] - previous.bbox[3]
                > max(typical_gap + block.size * 0.25, block.size * 0.7)
                or (short and indented)
                or (
                    short
                    and (at_left or indented)
                    and END_SENTENCE.search(previous.text)
                )
            )
        if not groups or split:
            groups.append([])
        groups[-1].append(line)
    return [_block(lines, block.source) for lines in groups]


def is_hidden_text(text: str) -> bool:
    """1. Recognise the whole Hidden page placeholder after folding whitespace."""
    return " ".join(text.split()) == "Hidden page"


def _classify(page: pymupdf.Page, blocks: list[_Block], images: list[dict]) -> str:
    """1. Treat the whole Hidden page placeholder as missing source content.
    2. Report no text when the PDF has no text spans.
    3. Identify a page-covering scan with mostly invisible text as inherited OCR.
    4. Treat the remaining text layers as born-digital.
    """
    if is_hidden_text(page.get_text()):
        return "hidden"
    if not blocks:
        return "no-text"
    area = page.rect.get_area()
    scan = any(
        (pymupdf.Rect(image["bbox"]) * page.rotation_matrix)
        .intersect(page.rect)
        .get_area()
        >= area * 0.85
        for image in images
    )
    if scan:
        trace = page.get_texttrace()
        total = sum(len(span["chars"]) for span in trace)
        hidden = sum(len(span["chars"]) for span in trace if span["type"] == 3)
        if total and hidden / total >= 0.5:
            return "inherited-ocr"
    return "born-digital"


def _tabular(box: pymupdf.Rect, blocks: list[_Block]) -> bool:
    """1. Collect text positions inside the candidate image or drawing rectangle.
    2. Identify at least two rows with three widely separated cell starts.
    3. Treat that layout as a table rather than a raster patch of prose.
    """
    rows: dict[int, list[float]] = defaultdict(list)
    for block in blocks:
        for line in block.lines:
            rect = pymupdf.Rect(line.bbox)
            if box.contains((rect.tl + rect.br) / 2) and len(line.text) >= 3:
                rows[round(rect.y0 / max(line.size, 1))].append(rect.x0)
    aligned_rows = 0
    for starts in rows.values():
        columns = []
        for start in sorted(starts):
            if not columns or start - columns[-1] > box.width * 0.08:
                columns.append(start)
        aligned_rows += len(columns) >= 3
    return aligned_rows >= 2


def _figure_boxes(
    page: pymupdf.Page,
    blocks: list[_Block],
    images: list[dict],
    body_size: float | None = None,
) -> list[pymupdf.Rect]:
    """1. Collect image objects and clustered vector drawings as figure candidates.
    2. Ignore scan backgrounds, thin rules, and simple borders around paragraphs.
    3. Group nearby small image components without enclosing body prose.
    4. Preserve body prose patches; exclude graphics, small annotations, and tables.
    """
    area = page.rect.get_area()
    drawings = page.get_drawings()
    book_body_size = body_size
    if body_size is not None:
        body_size = _body_size(blocks, body_size, page.rect.width)
    boxes = [
        (pymupdf.Rect(image["bbox"]) * page.rotation_matrix, "image", 0)
        for image in images
    ]
    for rect in page.cluster_drawings(drawings=drawings):
        count = sum(rect.intersects(drawing["rect"]) for drawing in drawings)
        boxes.append((rect * page.rotation_matrix, "vector", count))
    small_images = [
        box
        for box, kind, _ in boxes
        if kind == "image" and box.get_area() < area * 0.01
    ]
    prose_centres = [
        (pymupdf.Rect(line.bbox).tl + pymupdf.Rect(line.bbox).br) / 2
        for block in blocks
        for line in block.lines
        if len(line.text) >= 35 and line.bbox[2] - line.bbox[0] > page.rect.width * 0.3
    ]
    clusters: list[pymupdf.Rect] = []
    for box in small_images:
        merged = box
        for existing in clusters.copy():
            nearby = pymupdf.Rect(existing)
            nearby.x0 -= page.rect.width * 0.2
            nearby.x1 += page.rect.width * 0.2
            nearby.y0 -= page.rect.height * 0.1
            nearby.y1 += page.rect.height * 0.1
            union = merged | existing
            if nearby.intersects(merged) and not any(
                union.contains(centre) for centre in prose_centres
            ):
                merged = union
                clusters.remove(existing)
        clusters.append(merged)
    boxes.extend((box, "image", 0) for box in clusters)
    figures = []
    for block in blocks:
        if not re.match(r"^(?:table|tabla)\s+\d+(?:[.\-–]\d+)*\b", block.text, re.I):
            continue
        title = pymupdf.Rect(block.bbox)
        candidates = [
            box
            for box, _, _ in boxes
            if box.get_area() < area * 0.85
            and (
                box.contains((title.tl + title.br) / 2)
                or (
                    0 <= box.y0 - title.y1 <= block.size * 2
                    and box.x0 - block.size <= title.x0 <= box.x1
                )
            )
            and box.y1 - title.y1 > block.size * 2
        ]
        if candidates:
            box = max(candidates, key=lambda rect: rect.x1)
            rows: list[list[_Line]] = []
            for line in sorted(
                (
                    line
                    for candidate in blocks
                    for line in candidate.lines
                    if line.bbox[1] >= title.y1
                    and box.contains(
                        (pymupdf.Rect(line.bbox).tl + pymupdf.Rect(line.bbox).br) / 2
                    )
                ),
                key=lambda line: line.bbox[1],
            ):
                if not rows or line.bbox[1] - rows[-1][0].bbox[1] > line.size * 0.6:
                    rows.append([])
                rows[-1].append(line)
            bottom = title.y1
            for index, row in enumerate(rows):
                separated_cells = (
                    max(line.bbox[0] for line in row)
                    - min(line.bbox[0] for line in row)
                    > box.width * 0.08
                )
                if (
                    index
                    and row[0].bbox[1] - bottom > block.size * 1.3
                    and not separated_cells
                ):
                    break
                bottom = max(bottom, max(line.bbox[3] for line in row))
            figures.append(
                pymupdf.Rect(
                    min(title.x0, box.x0), title.y0, box.x1, min(bottom, box.y1)
                )
            )
            if body_size is not None:
                for note in blocks:
                    if (
                        0 <= note.bbox[1] - box.y1 <= block.size * 2
                        and note.size < body_size * 0.95
                        and box.x0 <= note.bbox[0] < note.bbox[2] <= box.x1
                    ):
                        figures.append(pymupdf.Rect(note.bbox))
    for box, kind, count in boxes:
        if (
            box.get_area() >= area * 0.85
            or box.get_area() < area * 0.002
            or box.width < page.rect.width * 0.06
            or box.height < page.rect.height * (0.008 if kind == "image" else 0.02)
        ):
            continue
        prose = [
            line
            for block in blocks
            for line in block.lines
            if len(line.text) >= 35
            and len(re.findall(r"[^\W\d_]{3,}", line.text)) >= 4
            and sum(c.islower() for c in line.text) > len(line.text) * 0.3
            and box.contains(
                (pymupdf.Rect(line.bbox).tl + pymupdf.Rect(line.bbox).br) / 2
            )
            and line.bbox[2] - line.bbox[0] > box.width * 0.4
        ]
        body_prose = [
            line
            for line in prose
            if body_size is not None and abs(line.size - body_size) <= body_size * 0.12
        ]
        if (
            kind == "image"
            and book_body_size is not None
            and box.height < page.rect.height * 0.12
            and box.width < page.rect.width * 0.5
            and prose
            and all(line.size < book_body_size * 0.94 for line in prose)
        ):
            figures.append(box)
            continue
        if _tabular(box, blocks) and len(body_prose) < 5:
            figures.append(box)
            continue
        if (
            len(body_prose) >= 5 and sum(len(line.text) for line in body_prose) >= 250
        ) or (
            box.height < page.rect.height * 0.65
            and len(body_prose) >= 2
            and sum(len(line.text) for line in body_prose) >= 100
        ):
            continue
        if (
            (kind == "image" or count <= 10)
            and len(prose) >= 3
            and sum(len(line.text) for line in prose) >= 180
        ):
            continue
        if not any(existing == box for existing in figures):
            figures.append(box)
    # A ruled table is layout content rather than a paragraph.
    rules = sorted(
        (
            drawing["rect"] * page.rotation_matrix
            for drawing in drawings
            if (drawing["rect"] * page.rotation_matrix).height < 3
            and (drawing["rect"] * page.rotation_matrix).width >= page.rect.width * 0.45
            and (drawing["rect"] * page.rotation_matrix).y0 > page.rect.height * 0.075
        ),
        key=lambda rect: rect.y0,
    )
    bands: list[list[pymupdf.Rect]] = []
    for rule in rules:
        if (
            not bands
            or rule.y0 - bands[-1][-1].y0 > page.rect.height * 0.25
            or abs(rule.x0 - bands[-1][-1].x0) > page.rect.width * 0.06
        ):
            bands.append([])
        bands[-1].append(rule)
    for band in bands:
        if (
            len(band) >= 3
            and band[-1].y0 - band[0].y0 > page.rect.height * 0.08
            and band[0].y0 > page.rect.height * 0.075
        ):
            figures.append(
                pymupdf.Rect(
                    min(rect.x0 for rect in band),
                    band[0].y0 - (body_size or 10) * 2.5,
                    max(rect.x1 for rect in band),
                    band[-1].y1 + 3,
                )
            )
    for box, kind, _ in boxes:
        if kind != "image" or box not in figures or box.width >= page.rect.width * 0.45:
            continue
        for block in blocks:
            for line in block.lines:
                rect = pymupdf.Rect(line.bbox)
                if (
                    len(line.text) < 100
                    and not END_SENTENCE.search(line.text)
                    and rect.width < box.width * 0.9
                    and 0 <= rect.y0 - box.y1 < line.size * 1.5
                    and box.x0 - line.size <= rect.x0 <= rect.x1 <= box.x1 + line.size
                ):
                    figures.append(rect)
    return figures


def _in_figure(line: _Line, figures: list[pymupdf.Rect]) -> bool:
    """1. Reject text whose centre lies inside a figure's object rectangle."""
    rect = pymupdf.Rect(line.bbox)
    centre = (rect.tl + rect.br) / 2
    return any(box.contains(centre) for box in figures)


def _without_figure_words(line: _Line, figures: list[pymupdf.Rect]) -> _Line | None:
    """1. Remove words whose centres lie inside a figure object or component group.
    2. Retain the original line when unaffected; rebuild only a partially covered line.
    """
    if not line.words:
        return None if _in_figure(line, figures) else line
    if _in_figure(line, figures):
        return None
    retained = [
        (text, bbox)
        for text, bbox in line.words
        if not any(
            box.contains((pymupdf.Rect(bbox).tl + pymupdf.Rect(bbox).br) / 2)
            for box in figures
        )
    ]
    if len(retained) == len(line.words):
        return line
    if not retained:
        return None
    box = pymupdf.Rect(retained[0][1])
    for _, bbox in retained[1:]:
        box |= pymupdf.Rect(bbox)
    return _Line(
        " ".join(text for text, _ in retained),
        tuple(box),
        line.size,
        line.bold,
        retained,
    )


def _clean_blocks(
    blocks: list[_Block],
    height: float,
    repeated: set[str],
    figures: list[pymupdf.Rect],
    top_fraction: float = 0.075,
) -> list[_Block]:
    """1. Drop top running heads and repeated or numbered footer lines.
    2. Remove lines inside figure boxes while retaining recognised captions.
    3. Split retained blocks only at visible paragraph boundaries.
    """
    clean = []
    for block in blocks:
        caption = CAPTION.match(block.text)
        lines = []
        for line in block.lines:
            words = line.text.split()
            margin_number = _number(words[0]) or _number(words[-1])
            if line.bbox[3] <= height * top_fraction or (
                _edge(line, height) and (margin_number or _margin_key(line) in repeated)
            ):
                continue
            if not caption:
                line = _without_figure_words(line, figures)
                if line is None:
                    continue
            lines.append(line)
        if lines:
            retained = _block(lines, block.source)
            clean.extend(
                [retained]
                if caption
                else _paragraphs(retained, split_titles=top_fraction != 0.04)
            )
    return clean


def _body_size(blocks: list[_Block], fallback: float, width: float) -> float:
    """1. Measure long prose lines, excluding caption blocks and enlarged titles.
    2. Use the local dominant font when compatible with the book's body family.
    """
    fonts = Counter()
    for block in blocks:
        if CAPTION.match(block.text):
            continue
        for line in block.lines:
            if (
                len(line.text) >= 40
                and line.bbox[2] - line.bbox[0] >= width * 0.25
                and fallback * 0.75 <= line.size <= fallback * 1.15
            ):
                fonts[line.size] += len(line.text)
    return fonts.most_common(1)[0][0] if fonts else fallback


def _coalesce(
    blocks: list[_Block],
    body_size: float,
    width: float,
    hanging_only: bool = False,
) -> list[_Block]:
    """1. Reassemble captions, OCR fragments, hanging entries, and split PDF blocks.
    2. Preserve paragraph breaks and captions; attach indented definitions to terms.
    3. Keep gaps larger than one body-font size as separate paragraph fragments.
    """
    result: list[_Block] = []
    for block in blocks:
        if result:
            previous = result[-1]
            gap = block.bbox[1] - previous.bbox[3]
            if (
                CAPTION.match(previous.text)
                and not CAPTION.match(block.text)
                and -previous.size <= gap <= previous.size * 1.5
                and abs(block.bbox[0] - previous.bbox[0]) < previous.size * 1.2
                and abs(previous.size - block.size) < previous.size * 0.06
                and previous.size < body_size * 0.94
            ):
                result[-1] = _block(previous.lines + block.lines, previous.source)
                continue
            same_column = abs(block.bbox[0] - previous.bbox[0]) < body_size * 1.2
            right = max(previous.bbox[2], block.bbox[2])
            last = previous.lines[-1]
            first = block.lines[0]
            overlapping_fragment = (
                not hanging_only
                and abs(first.bbox[1] - last.bbox[1]) < body_size * 0.65
                and previous.bbox[0] <= block.bbox[0] < previous.bbox[2]
            )
            short_end = (
                END_SENTENCE.search(last.text)
                and last.bbox[2] < right - body_size * 1.7
            )
            indented = (
                first.bbox[0] - min(line.bbox[0] for line in block.lines)
                > body_size * 0.7
            )
            hanging = (
                body_size * 0.7 < block.bbox[0] - previous.bbox[0] < body_size * 2.5
                and all(
                    line.bbox[0] - previous.lines[0].bbox[0] > body_size * 0.7
                    for line in block.lines
                )
                and (
                    len(previous.lines) == 1
                    or all(
                        line.bbox[0] - previous.lines[0].bbox[0] > body_size * 0.7
                        for line in previous.lines[1:]
                    )
                )
            )
            hanging_reset = (
                len(previous.lines) >= 2
                and all(
                    line.bbox[0] - previous.lines[0].bbox[0] > body_size * 0.5
                    for line in previous.lines[1:]
                )
                and first.bbox[0] <= previous.lines[0].bbox[0] + body_size * 0.5
                and last.bbox[0] - first.bbox[0] > body_size * 0.7
            )
            if (
                (
                    same_column
                    or hanging
                    or overlapping_fragment
                    or (
                        len(previous.lines) == 1
                        and body_size * 0.7
                        < previous.bbox[0] - block.bbox[0]
                        < body_size * 3
                    )
                )
                and (
                    not hanging_only
                    or hanging
                    or previous.source == block.source
                    or (
                        not END_SENTENCE.search(previous.text)
                        and (first.text[:1].islower() or first.text.startswith("("))
                    )
                )
                and -body_size * 2 <= gap <= body_size
                and abs(previous.size - block.size) < body_size * 0.15
                and (hanging or not short_end)
                and (
                    hanging
                    or not indented
                    or overlapping_fragment
                    or (not hanging_only and first.text[:1].islower())
                )
                and (hanging or not _heading(previous, body_size))
                and not _heading(block, body_size)
                and not CAPTION.match(previous.text)
                and (
                    not CAPTION.match(block.text)
                    or (
                        not END_SENTENCE.search(previous.text)
                        and block.size >= body_size * 0.94
                        and previous.size >= body_size * 0.94
                    )
                )
                and not LIST_START.match(block.text)
                and not hanging_reset
                and (
                    hanging
                    or overlapping_fragment
                    or block.bbox[2] - block.bbox[0] > width * 0.2
                )
            ):
                result[-1] = _block(previous.lines + block.lines, previous.source)
                continue
        result.append(block)
    return result


def _definitions(blocks: list[_Block], body_size: float, width: float) -> list[_Block]:
    """1. Read glossary lines in column order, retaining terms with their definitions.
    2. Start entries at short margin terms and after completed definitions.
    3. Preserve available OCR fragments without inventing missing terms or words.
    """
    groups: list[list[_Line]] = []
    column_left = min((b.bbox[0] for b in blocks), default=0)
    previous = None
    for block in blocks:
        for line in block.lines:
            new_column = (
                previous is not None
                and previous.bbox[0] < width * 0.5 < line.bbox[0]
                and line.bbox[1] < previous.bbox[1]
            )
            if new_column:
                column_left = min(
                    item.bbox[0]
                    for candidate in blocks
                    for item in candidate.lines
                    if item.bbox[0] > width * 0.5
                )
            at_margin = line.bbox[0] - column_left < body_size * 1.65
            starts = (
                previous is not None
                and (
                    (line.size >= body_size * 1.15 and len(line.text) < 160)
                    or (
                        line.bbox[0] - column_left < body_size * 0.9
                        and len(line.text) < 160
                    )
                    or (
                        at_margin
                        and len(line.text.split()) <= 3
                        and not re.search(r"\d", line.text)
                        and (
                            END_SENTENCE.search(previous.text)
                            or (line.bold and len(line.text) < 160)
                            or (
                                len(line.text) < 160
                                and line.bbox[2] - line.bbox[0] < width * 0.35
                                and not END_SENTENCE.search(line.text)
                            )
                        )
                    )
                )
                and not (
                    len(groups[-1]) == 1
                    and previous.bbox[0] - column_left < body_size * 1.3
                    and line.bbox[1] < previous.bbox[3]
                )
            )
            if not groups or new_column or starts:
                groups.append([])
            groups[-1].append(line)
            previous = line
    return [_block(lines, 0) for lines in groups]


def _bibliography(blocks: list[_Block], body_size: float) -> list[_Block]:
    """1. Preserve ordinary blocks until the bibliography heading is reached.
    2. Start a reference at an author name followed by initials, or at a heading.
    3. Attach continuation lines while preserving captions and later exercise blocks.
    """
    result: list[_Block] = []
    active = False
    author = re.compile(r"^[A-Z][\w’'\-]+,?\s+[A-Z]{1,3}(?:[.,&\s]|\()")
    for block in blocks:
        title = block.text.strip().casefold()
        if title in {"references", "bibliography", "bibliografía", "bibliografia"}:
            active = True
            result.append(block)
            continue
        if title in {"problems", "problemas"}:
            active = False
        if not active or CAPTION.match(block.text) or _heading(block, body_size):
            result.append(block)
            continue
        for line in block.lines:
            if (
                not result
                or author.match(line.text)
                or _heading(result[-1], body_size)
                or CAPTION.match(result[-1].text)
                or line.bbox[0] - result[-1].bbox[0] > body_size * 8
                or line.bbox[1] - result[-1].bbox[3] > body_size * 2
            ):
                result.append(_block([line], block.source))
            else:
                result[-1] = _block(result[-1].lines + [line], result[-1].source)
    return result


def _reading_order(blocks: list[_Block], width: float) -> list[_Block]:
    """1. Separate full-width blocks as boundaries between horizontal bands.
    2. Group overlapping or aligned blocks into columns; read down them left to right.
    """
    wide = sorted(
        (b for b in blocks if b.bbox[2] - b.bbox[0] > width * 0.65),
        key=lambda b: b.bbox[1],
    )
    remaining = [b for b in blocks if b not in wide]
    ordered = []
    for boundary in [*wide, None]:
        band = [
            b for b in remaining if boundary is None or b.bbox[1] < boundary.bbox[1]
        ]
        remaining = [b for b in remaining if b not in band]
        columns: list[list[_Block]] = []
        for block in sorted(band, key=lambda b: b.bbox[0]):
            column = next(
                (
                    c
                    for c in columns
                    if abs(c[0].bbox[0] - block.bbox[0]) < width * 0.12
                    or any(b.bbox[0] <= block.bbox[0] < b.bbox[2] for b in c)
                ),
                None,
            )
            if column is None:
                columns.append([block])
            else:
                column.append(block)
        for column in columns:
            ordered.extend(sorted(column, key=lambda b: (b.bbox[1], b.bbox[0])))
        if boundary:
            ordered.append(boundary)
    return ordered


def _heading(block: _Block, body_size: float) -> bool:
    """1. Recognise short, enlarged text as a section heading.
    2. Exclude caption openings and sentence-like paragraphs from this rule.
    """
    return (
        (
            block.size >= body_size * 1.15
            or (
                block.lines[0].bold
                and block.size >= body_size * 0.8
                and not LIST_START.match(block.text)
            )
        )
        and len(block.text) < 220
        and len(block.lines) <= 4
        and not CAPTION.match(block.text)
        and not END_SENTENCE.search(block.text)
        and (
            block.size >= body_size * 1.15
            or (
                not block.text[:1].islower()
                and not block.text.rstrip().endswith((",", ";"))
            )
        )
    )


def _continues(
    previous: _Block,
    current: _Block,
    column_left: float | None = None,
) -> bool:
    """1. Require an unfinished sentence with a matching body font.
    2. Require the next paragraph to start at its column margin without indentation.
    """
    if column_left is None:
        column_left = min(line.bbox[0] for line in current.lines)
    unindented = current.lines[0].bbox[0] - column_left < current.size * 0.7
    return (
        not END_SENTENCE.search(previous.text)
        and abs(previous.size - current.size) <= previous.size * 0.12
        and unindented
        and bool(current.text)
    )


def _positions(
    start: int,
    end: int,
    accounts: list[PageAccount],
) -> tuple[tuple[str, str] | None, str | None]:
    """1. Require a confident printed number on every page of the passage.
    2. Preserve the missing-page reasons when any part of the range is unknown.
    """
    pages = accounts[start - 1 : end]
    reasons = sorted({p.reason for p in pages if p.reason})
    if reasons:
        return None, ";".join(reasons)
    return (pages[0].printed_page, pages[-1].printed_page), None


def extract_book(
    pdf_path: str | Path, book_id: str, language: str, *, ocr_root: Path | None = None
) -> Extraction:
    """1. Read positioned text, margin numbers, bookmarks, and diagnostic labels.
    2. Classify provenance, exclude hidden placeholders, and verify numbering.
    3. Substitute kept readings, separating captions, headings, and failed rules.
    4. Remove margins and figure labels from PDF text; assign its sections.
    5. Reassemble PDF fragments, preserving legacy boundaries without readings.
    6. Assign kinds and join passing content sentences across body-free pages.
    7. Return plain records in memory, including an account of every PDF page.
    """
    from med_ask.ocr import (
        ROOT,
        failed_rules,
        has_ink,
        load_reading,
        reading_candidates,
        reading_lines,
        reading_number,
    )

    root = ROOT if ocr_root is None else ocr_root
    passages: list[Passage] = []
    captions: list[Caption] = []
    with pymupdf.open(pdf_path) as doc:
        if not doc.is_pdf:
            raise ValueError("Extraction requires a PDF")
        raw_pages, sources, page_images = [], [], []
        page_texts, inks = [], []
        for page in doc:
            blocks = _read_blocks(page)
            images = page.get_image_info()
            source = _classify(page, blocks, images)
            raw_pages.append([] if source == "hidden" else blocks)
            sources.append(source)
            page_images.append(images)
            text = page.get_text()
            page_texts.append(text)
            inks.append(
                False
                if source == "hidden"
                else has_ink(page)
                if len(text.strip()) < 40
                else True
            )
        heights = [page.rect.height for page in doc]
        widths = [page.rect.width for page in doc]
        labels = [page.get_label() for page in doc]
        toc = doc.get_toc()
    candidates = [
        _candidates(blocks, h) for blocks, h in zip(raw_pages, heights, strict=True)
    ]
    readings = [
        None if source == "hidden" else load_reading(book_id, i + 1, root)
        for i, source in enumerate(sources)
    ]
    original_decisions = _printed_pages(candidates)
    candidates = [
        reading_candidates(r["text"]) if r else c
        for r, c in zip(readings, candidates, strict=True)
    ]
    has_readings = any(readings)
    decisions = _printed_pages(candidates)
    decisions = [
        decision if decision[0] else original
        for decision, original in zip(decisions, original_decisions, strict=True)
    ]
    page_failures = []
    for i, reading in enumerate(readings):
        if reading:
            sources[i] = "ocr"
            neighbours = [
                (offset, candidates[i + offset])
                for offset in (-2, -1, 1, 2)
                if 0 <= i + offset < len(candidates)
            ]
            reading["rules_failed"] = failed_rules(
                reading["text"],
                language,
                "ocr",
                reading["ink"],
                candidates[i],
                neighbours,
                reading["finish_reason"],
            )
        neighbours = [
            (offset, candidates[i + offset])
            for offset in (-2, -1, 1, 2)
            if 0 <= i + offset < len(candidates)
        ]
        page_failures.append(
            reading["rules_failed"]
            if reading
            else failed_rules(
                page_texts[i], language, sources[i], inks[i], candidates[i], neighbours
            )
        )
    decisions = [
        (
            value,
            method,
            "no-readable-header-number"
            if reason == "no-header-number" and sources[index] == "inherited-ocr"
            else reason,
        )
        for index, (value, method, reason) in enumerate(decisions)
    ]
    decisions = [
        (None, "none", "hidden") if sources[i] == "hidden" else decision
        for i, decision in enumerate(decisions)
    ]
    accounts = [
        PageAccount(i + 1, bool(blocks), *decision, label, sources[i])
        for i, (blocks, decision, label) in enumerate(
            zip(raw_pages, decisions, labels, strict=True)
        )
    ]
    margins: dict[str, set[int]] = defaultdict(set)
    fonts = Counter()
    for index, (blocks, height) in enumerate(zip(raw_pages, heights, strict=True)):
        for block in blocks:
            for line in block.lines:
                if _edge(line, height):
                    margins[_margin_key(line)].add(index)
                elif len(line.text) >= 40:
                    fonts[line.size] += len(line.text)
    repeated = {key for key, pages in margins.items() if len(pages) >= 2}
    body_size = fonts.most_common(1)[0][0] if fonts else 10.0
    with pymupdf.open(pdf_path) as doc:
        figures = [
            _figure_boxes(page, blocks, images, body_size)
            for page, blocks, images in zip(doc, raw_pages, page_images, strict=True)
        ]
    bookmark_path: list[str] = []
    heading_path: list[tuple[float, str]] = []
    entries = iter(sorted((row for row in toc if row[2] > 0), key=lambda row: row[2]))
    entry = next(entries, None)
    previous_last: _Block | None = None
    previous_section: tuple[str, ...] = ()
    for index, (blocks, height, width) in enumerate(
        zip(raw_pages, heights, widths, strict=True)
    ):
        page_number = index + 1
        while entry and entry[2] <= page_number:
            level, title, _ = entry
            bookmark_path = bookmark_path[: level - 1] + [title]
            heading_path = []
            entry = next(entries, None)
        if sources[index] == "hidden":
            previous_last = None
            continue
        if readings[index]:
            reading = readings[index]
            section = tuple(bookmark_path)
            printed, reason = _positions(page_number, page_number, accounts)
            text = "\n".join(reading_lines(reading["text"]))
            edge_numbers = reading_candidates(text)
            lines = text.splitlines()
            nonempty = [i for i, line in enumerate(lines) if line.strip()]
            for position in nonempty[:1] + nonempty[-1:]:
                number, _, remainder = reading_number(lines[position])
                if number in edge_numbers:
                    lines[position] = remainder
            text = "\n".join(lines)
            # Figure markers delimit captions even without a surrounding blank line.
            text = re.sub(r"(?m)^(\[FIGURE [^\]]+\])", r"\n\n\1", text)
            pending_caption = None
            for paragraph in re.split(r"\n\s*\n", text.strip()):
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                if pending_caption is not None:
                    pending_caption.text = paragraph
                    pending_caption = None
                    continue
                figure = re.match(r"^\[FIGURE ([^\]]+)\]\s*(.*)", paragraph, re.S)
                if figure:
                    captions.append(
                        Caption(
                            book_id,
                            language,
                            figure[1],
                            page_number,
                            figure[2].strip(),
                            {"text_source": "ocr"},
                        )
                    )
                    if not figure[2].strip():
                        pending_caption = captions[-1]
                    continue
                group = []
                for line in paragraph.splitlines():
                    if passage_kind((line,)) != "content":
                        if group:
                            _reading_passage(
                                passages,
                                book_id,
                                language,
                                page_number,
                                printed,
                                reason,
                                section,
                                group,
                                reading,
                            )
                            group = []
                        section = tuple(bookmark_path) + (line.strip(),)
                    else:
                        group.append(line)
                if group:
                    _reading_passage(
                        passages,
                        book_id,
                        language,
                        page_number,
                        printed,
                        reason,
                        section,
                        group,
                        reading,
                    )
            accounts[index].has_text = bool(reading["text"].strip())
            previous_last = None
            continue
        if not toc:
            running_titles = []
            for raw in blocks:
                for line in raw.lines:
                    if line.bbox[3] <= height * 0.075:
                        title_words = [
                            word for word in line.text.split() if not _number(word)
                        ]
                        if title_words:
                            running_titles.append(" ".join(title_words))
            if running_titles:
                title = max(running_titles, key=len)
                if bookmark_path != [title]:
                    bookmark_path = [title]
                    heading_path = []
        definitions = any(
            title.strip().casefold() in {"glossary", "glosario"}
            for title in bookmark_path
        )
        references = any(
            block.text.strip().casefold()
            in {"references", "bibliography", "bibliografía", "bibliografia"}
            for block in blocks
        )
        cleaned = _clean_blocks(
            blocks,
            height,
            repeated,
            figures[index],
            top_fraction=0.04 if definitions else 0.075,
        )
        local_size = _body_size(cleaned, body_size, width)
        ordered = _reading_order(cleaned, width)
        hanging_pairs = sum(
            local_size * 0.7 < b.bbox[0] - a.bbox[0] < local_size * 2.5
            and len(a.lines) == 1
            for a, b in zip(ordered, ordered[1:], strict=False)
        )
        ordered = (
            _definitions(ordered, local_size, width)
            if definitions and (sources[index] == "inherited-ocr" or hanging_pairs >= 5)
            else _bibliography(ordered, local_size)
            if references and sources[index] == "inherited-ocr"
            else _coalesce(
                ordered,
                local_size,
                width,
                hanging_only=(
                    sources[index] != "inherited-ocr"
                    or hanging_pairs >= 5
                    or references
                ),
            )
        )
        # Restrict prose to the book's body-font family, retaining enlarged headings.
        body = [
            b
            for b in ordered
            if definitions
            or b.size
            >= local_size * (0.85 if sources[index] == "inherited-ocr" else 0.88)
            or CAPTION.match(b.text)
            or _heading(b, local_size)
        ]
        first_body = True
        last_block = None
        for block in body:
            if (
                sources[index] == "inherited-ocr"
                and not definitions
                and not CAPTION.match(block.text)
            ):
                letters = [c for c in block.text if c.isalpha()]
                if (
                    block.size < local_size * 0.94
                    and block.bbox[2] - block.bbox[0] > width * 0.65
                ) or (
                    letters
                    and sum(c.isupper() for c in letters) > len(letters) * 0.45
                    and len(block.text) < 100
                    and not END_SENTENCE.search(block.text)
                    and not _heading(block, local_size)
                ):
                    continue
            caption = CAPTION.match(block.text)
            if caption:
                captions.append(
                    Caption(
                        book_id,
                        language,
                        caption["identifier"],
                        page_number,
                        block.text,
                        {"bbox": block.bbox, "text_source": sources[index]},
                    )
                )
                continue
            if not definitions and _heading(block, local_size):
                if block.size >= local_size * 1.35 and block.bbox[1] < height * 0.2:
                    heading_path = []
                if (
                    bookmark_path
                    and block.text.casefold() == bookmark_path[-1].casefold()
                ):
                    first_body = False
                    last_block = None
                    continue
                while heading_path and block.size >= heading_path[-1][0] * 0.97:
                    heading_path.pop()
                heading_path.append((block.size, block.text))
                first_body = False
                last_block = None
                continue
            if (
                len(block.text.split()) < 3
                or (
                    not definitions
                    and block.bbox[2] - block.bbox[0] < width * 0.28
                    and block.size <= local_size * 1.12
                    and any(b.bbox[2] - b.bbox[0] > width * 0.4 for b in body)
                )
                or block.text.isupper()
                or (
                    block.bbox[0] > width * 0.65
                    and block.bbox[2] - block.bbox[0] < width * 0.16
                    and len(block.lines) <= 4
                    and not END_SENTENCE.search(block.text)
                )
                or (
                    sum(
                        bool(
                            re.search(r"\((?:ch|chap|cap)\.\s*\d+\)$", line.text, re.I)
                        )
                        for line in block.lines
                    )
                    >= len(block.lines) * 0.5
                )
                or sum(c.isalpha() for c in block.text) < len(block.text) * 0.45
            ):
                continue
            section = tuple(bookmark_path + [title for _, title in heading_path])
            if any(
                title.strip().casefold()
                in {
                    "index",
                    "índice",
                    "content",
                    "contents",
                    "detailed contents",
                    "contenido",
                    "resumen del contenido",
                }
                for title in bookmark_path
            ):
                continue
            printed, reason = _positions(page_number, page_number, accounts)
            location = {"pdf_page": page_number, "bbox": block.bbox}
            column_left = min(
                b.bbox[0] for b in body if abs(b.bbox[0] - block.bbox[0]) < width * 0.18
            )
            starts_definition = definitions and (
                block.lines[0].bold
                or (
                    len(block.lines) > 1
                    and min(line.bbox[0] for line in block.lines[1:])
                    - block.lines[0].bbox[0]
                    > local_size * 0.7
                )
                or (
                    len(block.lines) == 1
                    and block.bbox[2] - block.bbox[0] < width * 0.35
                )
            )
            if definitions and not starts_definition:
                column_left = block.lines[0].bbox[0]
            column_continuation = (
                last_block is not None
                and block.bbox[0] - last_block.bbox[0] > width * 0.2
                and block.bbox[1] < last_block.bbox[1]
                and section == previous_section
                and _continues(last_block, block, column_left)
                and not starts_definition
            )
            if column_continuation:
                passage = passages[-1]
                passage.text = _join_lines([passage.text, block.text])
                passage.metadata["locations"].append(location)
            elif (
                first_body
                and previous_last is not None
                and passages
                and passages[-1].pdf_pages[1] == page_number - 1
                and section == previous_section
                and _continues(previous_last, block, column_left)
                and not starts_definition
                and (
                    not has_readings
                    or (
                        sources[index] != "inherited-ocr"
                        and not passages[-1].inherited_ocr
                        and not passages[-1].check_page
                    )
                )
            ):
                passage = passages[-1]
                passage.text = _join_lines([passage.text, block.text])
                passage.pdf_pages = (passage.pdf_pages[0], page_number)
                passage.printed_pages, passage.printed_page_reason = _positions(
                    *passage.pdf_pages, accounts
                )
                passage.metadata["locations"].append(location)
                passage.inherited_ocr |= sources[index] == "inherited-ocr"
                passage.metadata["page_text_sources"][page_number] = sources[index]
            else:
                passages.append(
                    Passage(
                        book_id,
                        language,
                        (page_number, page_number),
                        printed,
                        reason,
                        section,
                        len(passages) + 1,
                        block.text,
                        sources[index] == "inherited-ocr",
                        {
                            "locations": [location],
                            "page_text_sources": {page_number: sources[index]},
                        },
                    )
                )
            first_body = False
            last_block = block
            previous_section = section
        previous_last = last_block
    for passage in passages:
        if has_readings and passage.text_source != "ocr":
            passage.ocr_reasons = sorted(
                {
                    reason
                    for page in range(passage.pdf_pages[0], passage.pdf_pages[1] + 1)
                    for reason in page_failures[page - 1]
                }
            )
            passage.check_page = bool(passage.ocr_reasons)
        passage.text_source = (
            "inherited-ocr" if passage.inherited_ocr else "born-digital"
        )
        if "ocr" in passage.metadata.get("page_text_sources", {}).values():
            passage.text_source = "ocr"
    passages = join_page_passages(passages, accounts)
    return Extraction(book_id, language, passages, captions, accounts)


def _reading_passage(
    passages, book_id, language, page, printed, reason, section, lines, reading
):
    """1. Preserve a reading's paragraph as one passage with its source and failures."""
    passage = Passage(
        book_id,
        language,
        (page, page),
        printed,
        reason,
        section,
        len(passages) + 1,
        " ".join(line.strip() for line in lines),
        metadata={"page_text_sources": {page: "ocr"}},
        check_page=bool(reading["rules_failed"]),
        ocr_reasons=list(reading["rules_failed"]),
    )
    passage.text_source = "ocr"
    passages.append(passage)


def join_page_passages(
    passages: list[Passage], accounts: list[PageAccount]
) -> list[Passage]:
    """1. Compare consecutive passages across pages, skipping body-free pages.
    2. Refuse joins across hidden pages whose source content is missing.
    3. Require the same book and section, content kinds, and no unchecked OCR.
    4. Join unfinished sentences continued in lower case or with a hyphenated word.
    5. Extend PDF and verified printed ranges and preserve both source locations.
    6. Renumber the retained passages in reading order.
    """
    joined: list[Passage] = []
    for current in passages:
        previous = joined[-1] if joined else None
        hyphenated = (
            bool(re.search(r"[^\W\d_][-\u00ad]$", previous.text)) if previous else False
        )
        if (
            previous is not None
            and previous.pdf_pages[1] < current.pdf_pages[0]
            and all(
                account.text_source != "hidden"
                for account in accounts[
                    previous.pdf_pages[1] : current.pdf_pages[0] - 1
                ]
            )
            and previous.book_id == current.book_id
            and previous.section_path == current.section_path
            and previous.kind == current.kind == "content"
            and not previous.inherited_ocr
            and not current.inherited_ocr
            and not previous.check_page
            and not current.check_page
            and not END_SENTENCE.search(previous.text)
            and (current.text[:1].islower() or hyphenated)
        ):
            previous.text = (
                previous.text[:-1] + current.text
                if hyphenated
                else _join_lines([previous.text, current.text])
            )
            previous.pdf_pages = (previous.pdf_pages[0], current.pdf_pages[1])
            previous.printed_pages, previous.printed_page_reason = _positions(
                *previous.pdf_pages, accounts
            )
            if current.text_source == "ocr":
                previous.text_source = "ocr"
            previous.metadata.setdefault("locations", []).extend(
                current.metadata.get("locations", [])
            )
            previous.metadata.setdefault("page_text_sources", {}).update(
                current.metadata.get("page_text_sources", {})
            )
        else:
            joined.append(current)
    for order, passage in enumerate(joined, 1):
        passage.order = order
    return joined


def passage_label(passage: Passage) -> str:
    """1. Format the exact PDF range and the book identifier.
    2. Include a printed range only when known; otherwise include its reason.
    """
    start, end = passage.pdf_pages
    pdf = f"pdf page {start}" if start == end else f"pdf pages {start}–{end}"
    if passage.printed_pages:
        a, b = passage.printed_pages
        printed = f"print page: {a}" if start == end else f"print pages: {a}–{b}"
    else:
        printed = f"print page unknown: {passage.printed_page_reason}"
    return f"{passage.book_id}: {pdf} <{printed}>"


def sample_pages(result: Extraction, sample: int, seed: int) -> list[int]:
    """1. Select reproducible random PDF pages without replacement.
    2. Include both ends of a crossing paragraph when the sample has room.
    """
    pages = random.Random(seed).sample(
        range(1, len(result.pages) + 1), min(max(sample, 0), len(result.pages))
    )
    crossing = [p for p in result.passages if p.pdf_pages[0] != p.pdf_pages[1]]
    if crossing and len(pages) >= 2:
        start, end = random.Random(seed).choice(crossing).pdf_pages
        for page in (start, end):
            if page not in pages:
                replace = next(i for i, p in enumerate(pages) if p not in (start, end))
                pages[replace] = page
    return sorted(pages)


def report(result: Extraction, pages: list[int]) -> str:
    """1. Count text, hidden pages, paragraphs, captions, numbering, and crossings.
    2. Compare diagnostic PDF labels with confident printed numbers.
    3. List each sampled paragraph's label, section, and first six original words.
    """
    methods = Counter(p.printed_page_method for p in result.pages)
    reasons = Counter(p.reason for p in result.pages if p.reason)
    agreement = Counter()
    for page in result.pages:
        if not page.pdf_label:
            agreement["absent"] += 1
        elif page.printed_page is None:
            agreement["unverified"] += 1
        elif _number(page.pdf_label) == _number(page.printed_page):
            agreement["agree"] += 1
        else:
            agreement["disagree"] += 1
    provenance = Counter(p.text_source for p in result.pages)
    with_text = sum(p.has_text for p in result.pages)
    lines = [
        f"Book: {result.book_id} ({result.language})",
        f"Pages: {len(result.pages)}; text: {with_text}; "
        f"without text: {len(result.pages) - with_text}; "
        f"hidden: {provenance['hidden']}",
        f"Passages: {len(result.passages)}; captions: {len(result.captions)}",
        f"Text provenance: born-digital {provenance['born-digital']}; "
        f"inherited-OCR pages {provenance['inherited-ocr']}; "
        f"inherited-OCR passages {sum(p.inherited_ocr for p in result.passages)}",
        f"Printed pages: read {methods['read']}; inferred {methods['inferred']}; "
        f"none {methods['none']} ({dict(sorted(reasons.items()))})",
        f"PDF label agreement: {dict(sorted(agreement.items()))}",
        f"Passages spanning two pages: "
        f"{sum(p.pdf_pages[1] - p.pdf_pages[0] == 1 for p in result.passages)}",
        "Sample previews below are original source text.",
    ]
    for number in pages:
        account = result.pages[number - 1]
        lines.append(
            f"\nPDF page {number}: text={account.has_text}; "
            f"printed={account.printed_page}; {account.printed_page_method}; "
            f"reason={account.reason}; source={account.text_source}"
        )
        for passage in result.passages:
            if passage.pdf_pages[0] <= number <= passage.pdf_pages[1]:
                lines.append(
                    f"  #{passage.order} {passage_label(passage)} | "
                    f"inherited-OCR={passage.inherited_ocr} | "
                    f"section: {' > '.join(passage.section_path) or '(unknown)'} | "
                    f"{' '.join(passage.text.split()[:6])}"
                )
    return "\n".join(lines)


def _external_output(path: Path) -> Path:
    """1. Resolve the output path, including symlinks.
    2. Reject output inside a Git checkout so source content cannot enter a repository.
    """
    resolved = path.resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise ValueError("--output must be outside a Git checkout")
    return resolved


def main() -> int:
    """1. Parse the PDF, identity, language, and reproducible sample options.
    2. Extract in memory and print the report.
    3. Only for an explicit output folder, save the report and sampled page renders.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--book", required=True)
    parser.add_argument("--lang", required=True)
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--output", type=Path, help="External folder for report and PNGs"
    )
    args = parser.parse_args()
    if args.output:
        try:
            args.output = _external_output(args.output)
        except ValueError as error:
            parser.error(str(error))
    result = extract_book(args.pdf, args.book, args.lang)
    pages = sample_pages(result, args.sample, args.seed)
    text = report(result, pages)
    print(text)
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "report.txt").write_text(text, encoding="utf-8")
        with pymupdf.open(args.pdf) as doc:
            for number in pages:
                doc[number - 1].get_pixmap(dpi=110).save(
                    args.output / f"page-{number}.png"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
