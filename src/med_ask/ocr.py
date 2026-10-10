"""Sequential, resumable page reading; originals live only under /data/originals."""

import base64
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter

import pymupdf
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from med_ask.extract import _candidates, _classify, _number, _read_blocks
from med_ask.generation import detect_language, language_code

PROMPT = (
    "Transcribe all the text on this page exactly as printed, in reading order. "
    "Do not correct, modernise or translate anything: keep every word, spelling, "
    "accent, number, symbol and language exactly as printed. Join a word split by "
    "a hyphen at the end of a line. Keep each paragraph as one paragraph, with a "
    "blank line between paragraphs. Include headings, running headers, page "
    "numbers, table text and figure captions. Do not transcribe any text that is "
    "inside a figure, diagram, chemical structure, graph or photograph (labels, "
    "letters, axis text); instead, at the figure's place, write one line "
    "`[FIGURE n]`, with the figure's number as printed, or `[FIGURE ?]` when it "
    "has none, followed by its caption as printed. Output plain text only."
)
DPI = 300
MAX_TOKENS = 4096
METHOD_VERSION = "vision-300dpi-4096-t0-v1"
LANGUAGE_MIN = 1000
EMPTY_MIN = 40
NONWORD_LIMIT = 0.5
NONWORD_MIN = 50
INK_LIMIT = 0.001
_DARK_PIXELS = bytes(int(value < 180) for value in range(256))
ROOT = Path("/data/originals/ocr")


class VisionUnavailable(RuntimeError):
    pass


class VisionEndpoint:
    def __init__(self):
        self.client = OpenAI(
            base_url=os.environ["MODEL_BASE_URL"],
            api_key=os.environ["MODEL_API_KEY"],
            max_retries=0,
        )

    def read(self, png):
        """1. Send only an in-memory data PNG to the vision purpose, without retries.
        2. Return the unmodified text, reported identity, and completion reason.
        3. Signal unavailable service without including endpoint response text.
        """
        try:
            reply = self.client.chat.completions.create(
                model="vision",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,"
                                    + base64.b64encode(png).decode()
                                },
                            },
                        ],
                    }
                ],
                temperature=0,
                max_tokens=MAX_TOKENS,
                timeout=180,
            )
        except (APIConnectionError, APITimeoutError) as error:
            raise VisionUnavailable(
                "vision is not serving; run again to resume"
            ) from error
        except APIStatusError as error:
            if error.status_code >= 500:
                raise VisionUnavailable(
                    "vision is not serving; run again to resume"
                ) from error
            raise
        choice = reply.choices[0]
        return dict(
            text=choice.message.content or "",
            model=reply.model,
            finish_reason=choice.finish_reason,
        )


def reading_lines(text):
    """1. Drop exact preview-watermark lines from an in-memory reading copy."""
    return [
        line for line in text.splitlines() if line.strip() != "Copyrighted material"
    ]


def reading_candidates(text):
    """1. Ignore preview watermarks and inspect the first and last nonempty lines.
    2. Accept whole decimal or Roman numbers in either case for sequence checks.
    """
    lines = [line.strip() for line in reading_lines(text) if line.strip()]
    return {
        parsed: line for line in (lines[:1] + lines[-1:]) if (parsed := _number(line))
    }


def nonword_share(text):
    """1. Exclude numbers and strip ordinary punctuation from whitespace tokens.
    2. Count tokens lacking letters, a vowel, or mostly alphabetic characters.
    3. Return the share and sample size, without a dictionary.
    """
    words = [
        w.strip(".,;:!?()[]{}\"'")
        for w in text.split()
        if not any(c.isdigit() for c in w)
    ]
    bad = sum(
        not (
            sum(c.isalpha() for c in w) >= 2
            and sum(c.isalpha() for c in w) / max(1, len(w)) >= 0.7
            and any(c in "aeiouyáéíóúüAEIOUYÁÉÍÓÚÜ" for c in w)
        )
        for w in words
    )
    return bad / max(1, len(words)), len(words)


def has_ink(page):
    """1. Render the inner 96 percent at 72 DPI in grayscale, in memory.
    2. Require more than 0.1 percent of pixels below intensity 180.
    """
    box = pymupdf.Rect(page.rect)
    inset_x, inset_y = box.width * 0.02, box.height * 0.02
    box.x0 += inset_x
    box.x1 -= inset_x
    box.y0 += inset_y
    box.y1 -= inset_y
    samples = page.get_pixmap(
        dpi=72, colorspace=pymupdf.csGRAY, alpha=False, clip=box
    ).samples
    return (
        samples.translate(_DARK_PIXELS).count(b"\x01") / max(1, len(samples))
        > INK_LIMIT
    )


def failed_rules(
    text, language, source, ink, candidates, neighbours=(), finish_reason="stop"
):
    """1. Skip hidden placeholders, then check incomplete replies and missing text.
    2. Keep publisher text except nearly empty pages with ink; check OCR language.
    3. Measure malformed word shapes only when at least 50 tokens are available.
    4. Require a printed number for OCR; reject conflicts with nearby numbers.
    5. Return short reason codes, never source text.
    """
    reasons = []
    if source == "hidden":
        return reasons
    if source == "ocr" and finish_reason == "length":
        reasons.append("token-limit")
    if len(text.strip()) < EMPTY_MIN and ink:
        reasons.append("empty-ink")
    if source not in {"ocr", "inherited-ocr"}:
        return reasons
    if len(text) >= LANGUAGE_MIN and detect_language(text) != language_code(language):
        reasons.append("language")
    share, count = nonword_share(text)
    if count >= NONWORD_MIN and share > NONWORD_LIMIT:
        reasons.append("nonwords")
    if source in {"ocr", "inherited-ocr"} and (ink or text.strip()):
        if not candidates:
            reasons.append("number-missing")
        else:
            comparable = [
                (kind, value + offset)
                for offset, other in neighbours
                for kind, value in candidates
                if any(k == kind for k, _ in other)
            ]
            supported = any(
                (kind, value + offset) in other
                for offset, other in neighbours
                for kind, value in candidates
            )
            if comparable and not supported:
                reasons.append("number-order")
    return reasons


def load_reading(book_id, page, root=ROOT):
    """1. Load the page file if present and ignore older versions."""
    path = root / book_id / f"{page}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    return record if record["method_version"] == METHOD_VERSION else None


def save_reading(book_id, page, record, root=ROOT):
    """1. Create the book directory under the originals root.
    2. Write and sync a temporary JSON file beside the destination.
    3. Rename atomically and remove the temporary file on every failure.
    """
    directory = root / book_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{page}.json"
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".reading-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(record, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inspect_book(book, root=ROOT):
    """1. Read PDF provenance and margins, excluding hidden page placeholders.
    2. Substitute the best current-version reading and its margin candidates.
    3. Apply rules with nearby candidates and return page diagnostics without writes.
    """
    pages = []
    with pymupdf.open(book.path) as doc:
        for index, page in enumerate(doc, 1):
            blocks = _read_blocks(page)
            source = _classify(page, blocks, page.get_image_info())
            record = None if source == "hidden" else load_reading(book.id, index, root)
            text = (
                ""
                if source == "hidden"
                else record["text"]
                if record
                else page.get_text()
            )
            candidates = (
                reading_candidates(text)
                if record
                else _candidates(blocks, page.rect.height)
            )
            pages.append(
                dict(
                    pdf_page=index,
                    text=text,
                    text_source="ocr" if record else source,
                    ink=False if source == "hidden" else has_ink(page),
                    candidates=candidates,
                    record=record,
                    finish_reason=record["finish_reason"] if record else "stop",
                )
            )
    for index, page in enumerate(pages):
        neighbours = [
            (offset, pages[index + offset]["candidates"])
            for offset in (-2, -1, 1, 2)
            if 0 <= index + offset < len(pages)
        ]
        page["neighbours"] = neighbours
        page["rules_failed"] = failed_rules(
            page["text"],
            book.language,
            page["text_source"],
            page["ink"],
            page["candidates"],
            neighbours,
            page["finish_reason"],
        )
    return pages


def read_book(book, endpoint, root=ROOT, limit=None, output=print):
    """1. Skip hidden placeholders, check existing text, and resume current readings.
    2. Read failing pages at 300 DPI, one at a time; persist every completed reply.
    3. Retry only empty-with-ink or malformed upright replies once at 180 degrees.
    4. Stop cleanly when vision is unavailable, keeping durable progress.
    5. Report only counts and diagnostics, never book text or reported model names.
    """
    pages = recheck_book(book, root)
    read = 0
    with pymupdf.open(book.path) as doc:
        for page in pages:
            if page["text_source"] == "hidden":
                continue
            record = page["record"]
            if record and record["complete"]:
                continue
            if (
                not record
                and not page["rules_failed"]
                and not (root / book.id / f"{page['pdf_page']}.json").exists()
            ):
                continue
            if limit is not None and read >= limit:
                break
            index = page["pdf_page"]
            previous = root / book.id / f"{index}.json"
            history = (
                json.loads(previous.read_text())
                if previous.exists() and not record
                else None
            )
            record = record or dict(
                method_version=METHOD_VERSION,
                readings=[],
                complete=False,
                previous_versions=([history] if history else []),
                ink=page["ink"],
            )
            for rotated in (False, True):
                if any(r["rotated"] == rotated for r in record["readings"]):
                    continue
                matrix = pymupdf.Matrix(DPI / 72, DPI / 72).prerotate(
                    180 if rotated else 0
                )
                pixmap = doc[index - 1].get_pixmap(matrix=matrix, alpha=False)
                pixmap.set_dpi(DPI, DPI)
                png = pixmap.tobytes("png")
                started = perf_counter()
                try:
                    reading = endpoint.read(png)
                except VisionUnavailable:
                    output(
                        f"{book.id}: vision is not serving; "
                        f"stopped at PDF page {index}; "
                        "resume by running again",
                        flush=True,
                    )
                    return False
                reading.update(
                    rung="vision",
                    method_version=METHOD_VERSION,
                    rotated=rotated,
                    seconds=perf_counter() - started,
                    time=datetime.now(UTC).isoformat(),
                )
                reading["rules_failed"] = failed_rules(
                    reading["text"],
                    book.language,
                    "ocr",
                    page["ink"],
                    reading_candidates(reading["text"]),
                    page["neighbours"],
                    reading["finish_reason"],
                )
                record["readings"].append(reading)
                best = min(
                    record["readings"],
                    key=lambda r: (len(r["rules_failed"]), r["rotated"]),
                )
                record.update(best)
                record["complete"] = rotated or not needs_rotation(
                    reading["rules_failed"]
                )
                save_reading(book.id, index, record, root)
                if record["complete"]:
                    break
            page["candidates"] = reading_candidates(record["text"])
            for other in pages:
                offset = index - other["pdf_page"]
                if offset in (-2, -1, 1, 2):
                    other["neighbours"] = [
                        (n, page["candidates"] if n == offset else c)
                        for n, c in other["neighbours"]
                    ]
            read += 1
            output(
                f"{book.id}: PDF page {index}; "
                f"failures={','.join(record['rules_failed']) or 'none'}",
                flush=True,
            )
    return True


def needs_rotation(reasons):
    """1. Retry only nearly empty inked readings or excessive malformed words."""
    return bool({"empty-ink", "nonwords"}.intersection(reasons))


def recheck_book(book, root=ROOT):
    """1. Recompute current rules for every kept reading without model calls.
    2. Finalize partial readings that no longer need a rotation retry.
    3. Atomically save changed status, preserving text and historical attempts.
    4. Return the rechecked diagnostics for reading or queue reporting.
    """
    pages = inspect_book(book, root)
    for page in pages:
        record = page["record"]
        if not record:
            continue
        complete = record["complete"] or not needs_rotation(page["rules_failed"])
        if (
            record["rules_failed"] != page["rules_failed"]
            or complete != record["complete"]
        ):
            record.update(rules_failed=page["rules_failed"], complete=complete)
            save_reading(book.id, page["pdf_page"], record, root)
    return pages


def queue_summary(book, root=ROOT):
    """1. Apply the rules to the best available text for every page.
    2. List failures and totals for hidden, kept, read and passing, or queued pages.
    3. Summarize reading times and rotation rescues without exposing book text.
    """
    pages = inspect_book(book, root)
    queue = [
        {"pdf_page": p["pdf_page"], "reasons": p["rules_failed"]}
        for p in pages
        if p["rules_failed"]
    ]
    records = [p["record"] for p in pages if p["record"]]
    readings = [r for record in records for r in record["readings"]]
    shares = sorted(
        nonword_share(p["text"])[0] for p in pages if p["text_source"] != "hidden"
    )
    return dict(
        book_id=book.id,
        pages=len(pages),
        hidden=sum(p["text_source"] == "hidden" for p in pages),
        kept=sum(
            p["text_source"] != "hidden" and not p["record"] and not p["rules_failed"]
            for p in pages
        ),
        read_passing=sum(bool(p["record"]) and not p["rules_failed"] for p in pages),
        queued=len(queue),
        queue=queue,
        reasons=dict(Counter(r for p in queue for r in p["reasons"])),
        seconds_per_reading=[r["seconds"] for r in readings],
        rotations=sum(r["rotated"] for r in readings),
        rotation_rescues=sum(
            record["rotated"] and not record["rules_failed"] for record in records
        ),
        nonword_quantiles={
            str(q): shares[int((len(shares) - 1) * q)] if shares else 0
            for q in (0, 0.5, 0.9, 0.95, 0.99, 1)
        },
    )
