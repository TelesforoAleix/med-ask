"""Explicitly build source-side diagnostics; HTTP requests never inspect a book."""

import json
from datetime import UTC, datetime
from pathlib import Path

from med_ask.extract import _printed_pages
from med_ask.ocr import METHOD_VERSION, inspect_book, save_reading

ROOT = Path("/data/originals/resolve-cache")
VERSION = 1


class DiagnosticsUnavailable(RuntimeError):
    pass


def fingerprint(book, ocr_root):
    """1. Stat the PDF and kept-reading files without opening the PDF.
    2. Include language and diagnostic versions so changed inputs require a rebuild.
    """
    stat = book.path.stat()
    return dict(
        version=VERSION,
        method=METHOD_VERSION,
        language=book.language,
        pdf=[str(book.path), stat.st_mtime_ns, stat.st_size],
        ocr={
            p.name: [s.st_mtime_ns, s.st_size]
            for p in sorted((ocr_root / book.id).glob("*.json"))
            for s in [p.stat()]
        },
    )


def build_cache(book, ocr_root, root=ROOT):
    """1. Snapshot input file metadata and inspect the book without model calls.
    2. Precompute printed numbers and serialize diagnostics without attempt history.
    3. Refuse changed inputs, then publish the complete cache atomically.
    """
    signature = fingerprint(book, ocr_root)
    pages = inspect_book(book, ocr_root, apply_resolutions=False)
    decisions = _printed_pages([p["candidates"] for p in pages])
    rows = []
    for page, decision in zip(pages, decisions, strict=True):
        row = {k: v for k, v in page.items() if k not in {"record", "resolution"}}
        row["has_reading"] = bool(page["record"])
        row["printed_page"] = decision[0]
        if row["printed_page"] is None and len(page["candidates"]) == 1:
            row["printed_page"] = next(iter(page["candidates"].values()))
        row["candidates"] = [
            [kind, value, text] for (kind, value), text in page["candidates"].items()
        ]
        row["neighbours"] = [
            [
                offset,
                [[kind, value, text] for (kind, value), text in candidates.items()],
            ]
            for offset, candidates in page["neighbours"]
        ]
        rows.append(row)
    if fingerprint(book, ocr_root) != signature:
        raise DiagnosticsUnavailable("Inputs changed during diagnostics; run again")
    save_reading(
        book.id,
        "diagnostics",
        dict(fingerprint=signature, pages=rows, built_at=datetime.now(UTC).isoformat()),
        root,
    )
    return dict(
        book_id=book.id,
        pages=len(rows),
        queued=sum(bool(p["rules_failed"]) for p in rows),
    )


def load_cache(book, ocr_root, root=ROOT, memory=None):
    """1. Require a complete cache whose input file metadata is still current.
    2. Reuse parsed diagnostics in memory until the atomic cache file changes.
    3. Tell the operator to rebuild missing or stale diagnostics, never scan a PDF.
    """
    path = root / book.id / "diagnostics.json"
    message = (
        f"Resolve diagnostics missing or stale. Run ingest resolve-cache {book.id}."
    )
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = memory.get(book.id) if memory is not None else None
        if cached is None or cached[0] != key:
            data = json.loads(path.read_text())
            for page in data["pages"]:
                page["candidates"] = {
                    (kind, value): text for kind, value, text in page["candidates"]
                }
                page["neighbours"] = [
                    (offset, {(kind, value): text for kind, value, text in candidates})
                    for offset, candidates in page["neighbours"]
                ]
            if memory is not None:
                memory[book.id] = (key, data)
        else:
            data = cached[1]
        if data["fingerprint"] != fingerprint(book, ocr_root):
            raise DiagnosticsUnavailable(message)
        return data["pages"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DiagnosticsUnavailable(message) from error


def keep_hint_cache_current(book_id, page, ocr_root, root=ROOT):
    """1. Read the existing cache after appending only a hinted attempt.
    2. Advance only that file's metadata; preserve diagnostics and all other inputs.
    3. Atomically publish so hints do not force a book scan or hide unrelated changes.
    """
    path = root / book_id / "diagnostics.json"
    data = json.loads(path.read_text())
    stat = (ocr_root / book_id / f"{page}.json").stat()
    data["fingerprint"]["ocr"][f"{page}.json"] = [stat.st_mtime_ns, stat.st_size]
    # If no reading existed, the new record preserves the cached page text.
    data["pages"][page - 1]["has_reading"] = True
    save_reading(book_id, "diagnostics", data, root)
