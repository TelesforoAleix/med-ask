"""Explicit, model-free book navigation; requests only read published files."""

import hashlib
import json
import os
import random
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import pymupdf

from med_ask.extract import excluded_section, extract_book
from med_ask.figures import figure_entries
from med_ask.ocr import METHOD_VERSION, load_reading
from med_ask.ocr import ROOT as OCR_ROOT
from med_ask.resolutions import ROOT as RESOLUTIONS_ROOT
from med_ask.resolutions import load_resolution
from med_ask.retrieval import passage_id

ROOT = Path("/data/navigation")
VERSION = 2
KINDS = ("figure", "table", "section", "chapter", "page")
IDENTIFIER = r"\d+(?:[.\-–—]\d+)*(?:[A-Za-z])?"
PREFIXES = {
    "figure": r"fig(?:ure|ura)?s?\.?",
    "table": r"tabl(?:e|a)s?",
    "section": r"(?:sections?|secci[oó]n(?:es)?)",
    "chapter": r"(?:chapters?|cap[ií]tulos?)",
    "page": r"(?:pp?\.|p[aá]gs?\.|p[aá]ginas?|pages?)",
}
PATTERNS = {
    kind: re.compile(r"\b(?:" + prefix + r")\s*(?P<id>" + IDENTIFIER + r")\b", re.I)
    for kind, prefix in PREFIXES.items()
}
CONTINUATION = re.compile(
    r"\s*(?:,\s*(?:(?:and|y|e)\s+)?|(?:and|y|e)\s+)(" + IDENTIFIER + r")\b", re.I
)
TABLE_CAPTION = re.compile(
    r"^\s*(?:Table|Tabla)\s+(" + IDENTIFIER + r")\b", re.I | re.M
)
HEADING = re.compile(r"^\s*(?:(chapter|cap[ií]tulo)\s+)?(\d+(?:[.\-–—]\d+)*)\b", re.I)


NUMBERED_CHAPTER = re.compile(r"^\s*(?:chapter|cap[ií]tulo)\s+(\d{1,3})\b", re.I)
NUMBERED_SECTION = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})+)\s+[^\W\d_]", re.I)


def content_page_numbers(extraction):
    """1. Count each PDF page covered by content passages once."""
    return {
        page
        for p in extraction.passages
        if p.kind == "content"
        for page in range(p.pdf_pages[0], p.pdf_pages[1] + 1)
    }


def scanned_book(extraction):
    """1. Select books where kept readings supply more than half the content pages."""
    pages = content_page_numbers(extraction)
    return (
        bool(pages)
        and sum(extraction.pages[n - 1].text_source == "ocr" for n in pages)
        > len(pages) / 2
    )


def numbered_paths(first):
    """1. Keep explicit numbered chapter headings as roots, deduplicated by number.
    2. Keep numbered section titles under the observed chapter with that number.
    3. Attach deeper sections to available numbered ancestors; never invent headings.
    4. Omit orphan sections and sections preceding their observed chapter heading.
    """
    chapters, sections = {}, {}
    for path, page in sorted(first.items(), key=lambda item: item[1]):
        title = path[-1].strip()
        if match := NUMBERED_CHAPTER.match(title):
            chapters.setdefault(int(match[1]), (title, page))
        elif match := NUMBERED_SECTION.match(title):
            number = tuple(int(n) for n in match[1].split("."))
            sections.setdefault(number, (title, page))
    result = {(title,): page for title, page in chapters.values()}
    section_paths = {}
    for number, (title, page) in sorted(
        sections.items(), key=lambda item: len(item[0])
    ):
        chapter = chapters.get(number[0])
        if chapter is None or page < chapter[1]:
            continue
        parent = (chapter[0],)
        for depth in range(2, len(number)):
            candidate = section_paths.get(number[:depth])
            if candidate and result[candidate] <= page:
                parent = candidate
        path = parent + (title,)
        section_paths[number] = path
        result[path] = page
    return result


def identifier_key(value):
    """1. Compare case and numeric separators without changing source offsets."""
    return re.sub(r"[.\-–—]", "-", value.casefold())


def references(text):
    """1. Find English and Spanish reference prefixes in unchanged stored text.
    2. Expand comma/conjunction lists; retain exact character spans and labels.
    3. Treat a page range as one link to its first printed page.
    """
    found = []
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            end = match.end()
            value = match["id"]
            if kind == "page":
                # A dash in a page reference expresses a range, not a section.
                value = re.split(r"[-–—]", value)[0]
            found.append(
                dict(
                    start=match.start(),
                    end=end,
                    kind=kind,
                    label=text[match.start() : end],
                    identifier=value,
                )
            )
            while continuation := CONTINUATION.match(text, end):
                start, end = continuation.span(1)
                found.append(
                    dict(
                        start=start,
                        end=end,
                        kind=kind,
                        label=text[start:end],
                        identifier=continuation[1],
                    )
                )
    return sorted(found, key=lambda ref: (ref["start"], ref["end"]))


def contents_and_pages(extraction, bookmarks):
    """1. Collect bookmark paths and extracted section paths at their first pages.
    2. Merge paths and omit contents/index branches; filter scanned books by number.
    3. For scanned books, derive breadcrumbs only from that filtered tree.
    4. Describe every PDF page, carrying the preceding path across passage gaps.
    """
    first = {}
    bookmark_path = []

    def add(path, page):
        if not 1 <= page <= len(extraction.pages) or excluded_section(path):
            return
        for depth in range(1, len(path) + 1):
            prefix = tuple(path[:depth])
            first[prefix] = min(first.get(prefix, page), page)

    for level, title, page in bookmarks:
        if page < 1:
            continue
        bookmark_path = bookmark_path[: level - 1] + [title.strip()]
        add(bookmark_path, page)
    paths = {}
    for passage in extraction.passages:
        path = tuple(passage.section_path)
        add(path, passage.pdf_pages[0])
        for page in range(passage.pdf_pages[0], passage.pdf_pages[1] + 1):
            paths.setdefault(page, path)
    if scanned_book(extraction):
        first = numbered_paths(first)
        # Only retained tree entries can change a scanned page's breadcrumb.
        paths = {
            page: path
            for path, page in sorted(
                first.items(), key=lambda item: (item[1], len(item[0]), item[0])
            )
        }
    tree, nodes = [], {}
    for path, page in sorted(
        first.items(), key=lambda item: (item[1], len(item[0]), item[0])
    ):
        node = dict(
            title=path[-1],
            pdf_page=page,
            printed_page=extraction.pages[page - 1].printed_page,
            children=[],
        )
        nodes[path] = node
        (nodes[path[:-1]]["children"] if path[:-1] else tree).append(node)
    pages, previous = [], ()
    for account in extraction.pages:
        number = account.pdf_page
        if number in paths:
            previous = paths[number]
        path = () if excluded_section(previous) else previous
        pages.append(
            dict(
                pdf_page=number,
                printed_page=account.printed_page,
                section_path=[
                    dict(title=t, pdf_page=first[path[: i + 1]])
                    for i, t in enumerate(path)
                ],
            )
        )
    return tree, pages, first


def resolve_references(extraction, figures, first, page_texts):
    """1. Build same-book target sets from figures, captions, headings and pages.
    2. Find references in the exact NUL-stripped text served by the stored index.
    3. Keep only unique targets and count every resolved and unresolved mention.
    """
    targets = {kind: defaultdict(set) for kind in KINDS}
    for figure in figures:
        if figure.book_id != extraction.book_id:
            continue
        targets["figure"][identifier_key(figure.identifier)].add(
            (figure.pdf_page, figure.id)
        )
    for number, text in enumerate(page_texts, 1):
        for match in TABLE_CAPTION.finditer(text):
            targets["table"][identifier_key(match[1])].add((number, None))
    for path, page in first.items():
        if match := HEADING.match(path[-1]):
            kind = (
                "chapter"
                if match[1] or (len(path) == 1 and "." not in match[2])
                else "section"
            )
            targets[kind][identifier_key(match[2])].add((page, None))
    for page in extraction.pages:
        if page.printed_page:
            targets["page"][identifier_key(page.printed_page)].add(
                (page.pdf_page, None)
            )
    counts = {kind: dict(resolved=0, unresolved=0) for kind in KINDS}
    result, checks = {}, []
    for passage in extraction.passages:
        links = []
        for reference in references(passage.text.replace("\x00", "")):
            identifier = reference.pop("identifier")
            kind = reference["kind"]
            choices = targets[kind].get(identifier_key(identifier), set())
            if len(choices) != 1:
                counts[kind]["unresolved"] += 1
                continue
            page, figure_id = next(iter(choices))
            link = dict(**reference, target_pdf_page=page)
            if figure_id:
                link["figure_id"] = figure_id
            links.append(link)
            counts[kind]["resolved"] += 1
            checks.append((link, identifier))
        if links:
            result[passage_id(passage)] = links
    return result, counts, checks


def navigation_report(extraction, data, figures, counts, checks, page_texts):
    """1. Draw a seeded sample of up to 100 links, including every present kind.
    2. Check identifiers on destination text, or figure entries on that page.
    3. Report precision and contents coverage as counts only, never book text.
    """
    rng = random.Random(20261015)
    grouped = {
        kind: [i for i, c in enumerate(checks) if c[0]["kind"] == kind]
        for kind in KINDS
    }
    chosen = {rng.choice(rows) for rows in grouped.values() if rows}
    remaining = [i for i in range(len(checks)) if i not in chosen]
    chosen.update(rng.sample(remaining, min(100 - len(chosen), len(remaining))))
    sample = [checks[i] for i in sorted(chosen)]
    scored = {kind: dict(sampled=0, passed=0, precision=None) for kind in KINDS}
    for link, identifier in sample:
        kind, page = link["kind"], link["target_pdf_page"]
        target_text = page_texts[page - 1]
        key = identifier_key(identifier)
        if kind == "figure":
            passed = any(
                f.id == link.get("figure_id") and f.pdf_page == page for f in figures
            )
        elif kind == "table":
            passed = any(
                identifier_key(m[1]) == key for m in TABLE_CAPTION.finditer(target_text)
            )
        elif kind in {"section", "chapter"}:
            passed = any(
                (m := HEADING.match(line)) and identifier_key(m[2]) == key
                for line in target_text.splitlines()
            )
        else:
            passed = bool(
                re.search(
                    r"(?<!\w)" + re.escape(identifier) + r"(?!\w)", target_text, re.I
                )
            )
        scored[kind]["sampled"] += 1
        scored[kind]["passed"] += int(passed)
    for row in scored.values():
        if row["sampled"]:
            row["precision"] = row["passed"] / row["sampled"]
    levels = Counter()

    def walk(nodes, depth=1):
        for node in nodes:
            levels[depth] += 1
            walk(node["children"], depth + 1)

    walk(data["contents"])
    content_pages = content_page_numbers(extraction)
    covered = sum(bool(data["pages"][n - 1]["section_path"]) for n in content_pages)
    coverage = covered / len(content_pages) if content_pages else 0
    precision = (
        sum(r["passed"] for r in scored.values()) / len(sample) if sample else None
    )
    observed_chapters = set()
    for passage in extraction.passages:
        for title in passage.section_path:
            if match := NUMBERED_CHAPTER.match(title):
                observed_chapters.add(int(match[1]))
            elif match := NUMBERED_SECTION.match(title):
                observed_chapters.add(int(match[1].split(".")[0]))
    chapter_entries = {
        int(match[1])
        for node in data["contents"]
        if (match := NUMBERED_CHAPTER.match(node["title"]))
    }
    observed_chapters.update(chapter_entries)
    return dict(
        book_id=extraction.book_id,
        counts=counts,
        checks=scored,
        precision=precision,
        precision_passed=precision is not None and precision >= 0.9,
        contents_levels=dict(levels),
        numbered_contents_only=scanned_book(extraction),
        chapter_numbers_observed=sorted(observed_chapters),
        chapter_range_basis="1 through highest numbered heading in extracted paths",
        chapter_entry_coverage={
            str(n): n in chapter_entries
            for n in range(1, max(observed_chapters, default=0) + 1)
        },
        content_pages=len(content_pages),
        content_pages_with_path=covered,
        contents_coverage=coverage,
        thin_contents=max(levels, default=0) < 2 or coverage < 0.5,
    )


def apply_precision_gate(data, report):
    """1. Exclude every kind whose seeded sample precision is below 90 percent.
    2. Remove its links from every passage while preserving text, ids and offsets.
    3. Record raw checks, published counts and retained-sample precision explicitly.
    """
    excluded = [
        kind
        for kind in KINDS
        if report["checks"][kind]["precision"] is not None
        and report["checks"][kind]["precision"] < 0.9
    ]
    data["excluded_kinds"] = excluded
    data["references"] = {
        identity: retained
        for identity, links in data["references"].items()
        if (retained := [link for link in links if link["kind"] not in excluded])
    }
    report["excluded_kinds"] = excluded
    report["pre_filter_precision"] = report["precision"]
    report["published_checks"] = {
        kind: (
            dict(sampled=0, passed=0, precision=None)
            if kind in excluded
            else row.copy()
        )
        for kind, row in report["checks"].items()
    }
    for kind, row in report["counts"].items():
        row["excluded"] = row["resolved"] if kind in excluded else 0
        row["published"] = row["resolved"] - row["excluded"]
    sampled = sum(row["sampled"] for row in report["published_checks"].values())
    passed = sum(row["passed"] for row in report["published_checks"].values())
    report["precision"] = passed / sampled if sampled else None
    report["precision_passed"] = bool(sampled) and report["precision"] >= 0.9


def pdf_signature(book):
    """1. Stat the mounted PDF without opening it or depending on its host path."""
    stat = book.path.stat()
    return [stat.st_mtime_ns, stat.st_size]


def fingerprint(book, ocr_root=OCR_ROOT, resolutions_root=RESOLUTIONS_ROOT):
    """1. Record the PDF, language, method and complete reading/resolution metadata.
    2. Include builder sources so changed extraction or navigation requires rebuilding.
    """
    return dict(
        version=VERSION,
        pdf=pdf_signature(book),
        language=book.language,
        reading_method=METHOD_VERSION,
        code={
            name: hashlib.sha256(
                Path(__file__).with_name(name + ".py").read_bytes()
            ).hexdigest()
            for name in ("navigation", "extract", "figures", "ocr", "resolutions")
        },
        inputs={
            name: {
                p.name: [s.st_mtime_ns, s.st_size]
                for p in sorted((root / book.id).glob("*.json"))
                for s in [p.stat()]
            }
            for name, root in [
                ("readings", ocr_root),
                ("resolutions", resolutions_root),
            ]
        },
    )


def publish(path, data):
    """1. Write and sync a complete temporary file beside its destination.
    2. Replace atomically and clean up on success or failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".navigation-",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(data, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def build_navigation(
    book, root=ROOT, ocr_root=OCR_ROOT, resolutions_root=RESOLUTIONS_ROOT
):
    """1. Snapshot inputs and extract the book and its existing figure entries.
    2. Read bookmarks and page text, preferring owner resolutions and kept readings.
    3. Build contents, page info and uniquely resolved passage references.
    4. Score a seeded sample and suppress every kind below 90 percent precision.
    5. Refuse changed inputs before atomically publishing the filtered navigation.
    """
    signature = fingerprint(book, ocr_root, resolutions_root)
    extraction = extract_book(
        book.path,
        book.id,
        book.language,
        ocr_root=ocr_root,
        resolutions_root=resolutions_root,
    )
    figures = figure_entries(extraction, pdf_path=book.path)
    texts = []
    with pymupdf.open(book.path) as pdf:
        bookmarks = pdf.get_toc()
        for page in pdf:
            reading = load_resolution(book.id, page.number + 1, resolutions_root)
            reading = reading or load_reading(book.id, page.number + 1, ocr_root)
            texts.append(reading["text"] if reading else page.get_text())
    contents, pages, first = contents_and_pages(extraction, bookmarks)
    links, counts, checks = resolve_references(extraction, figures, first, texts)
    data = dict(
        version=VERSION,
        book_id=book.id,
        fingerprint=signature,
        built_at=datetime.now(UTC).isoformat(),
        contents=contents,
        pages=pages,
        references=links,
    )
    report = navigation_report(extraction, data, figures, counts, checks, texts)
    apply_precision_gate(data, report)
    if fingerprint(book, ocr_root, resolutions_root) != signature:
        raise ValueError("Navigation inputs changed during build; run navigate again")
    publish(root / f"{book.id}.json", data)
    return report


def load_navigation(book, root=ROOT, memory=None):
    """1. Read a published file once per file/PDF fingerprint into worker memory.
    2. Validate its version and mounted PDF stat without reading originals or a PDF.
    3. Return no navigation for missing, malformed or stale files; never extract.
    """
    try:
        path = root / f"{book.id}.json"
        stat = path.stat()
        key = (
            str(path),
            stat.st_mtime_ns,
            stat.st_size,
            tuple(pdf_signature(book)),
            VERSION,
        )
        cached = memory.get(book.id) if memory is not None else None
        if cached and cached[0] == key:
            return cached[1]
        data = json.loads(path.read_text())
        if (
            data["version"] != VERSION
            or data["book_id"] != book.id
            or data["fingerprint"]["pdf"] != list(key[3])
        ):
            return None
        if (
            not isinstance(data["contents"], list)
            or not isinstance(data["pages"], list)
            or not isinstance(data["references"], dict)
        ):
            return None
        if memory is not None:
            memory[book.id] = (key, data)
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None
