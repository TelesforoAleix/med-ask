"""Owner resolutions stored beside originals, never in the index or database."""

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pymupdf
from flask import Blueprint, abort, current_app, jsonify, request

from med_ask.extract import _number
from med_ask.manifest import load_manifest
from med_ask.ocr import (
    DPI,
    METHOD_VERSION,
    VisionEndpoint,
    VisionPageTimeout,
    VisionUnavailable,
    failed_rules,
    load_reading,
    reading_candidates,
    save_reading,
)
from med_ask.ocr import (
    ROOT as OCR_ROOT,
)
from med_ask.resolve_cache import (
    ROOT as CACHE_ROOT,
)
from med_ask.resolve_cache import (
    DiagnosticsUnavailable,
    keep_hint_cache_current,
    load_cache,
)

ROOT = Path("/data/originals/resolutions")
REASONS = {
    "number-missing": "The printed page number could not be read.",
    "number-order": "The printed page number does not follow nearby pages.",
    "language": "The reading appears to be in the wrong language.",
    "nonwords": "Most words in the reading do not look like words.",
    "token-limit": "The reading reached its token limit and may be incomplete.",
    "empty-ink": "The reading is nearly empty although the page has ink.",
    "page-timeout": "The page reading timed out.",
}


def load_resolution(book_id, page, root=None):
    """1. Load the latest owner resolution from its per-page source file."""
    path = (ROOT if root is None else root) / book_id / f"{page}.json"
    return json.loads(path.read_text())["history"][-1] if path.exists() else None


def apply_resolution(reading, resolution):
    """1. Substitute resolved text in memory while preserving OCR provenance.
    2. Clear reading failures without altering the kept reading file.
    """
    return {
        **(reading or {}),
        "text": resolution["text"],
        "ink": (reading or {}).get("ink", True),
        "finish_reason": "stop",
        "rules_failed": [],
    }


@contextmanager
def book_lock(root, book_id):
    directory = root / book_id
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def save_resolution(book_id, page, resolution, root=ROOT):
    """1. Read earlier resolutions under the caller's book lock.
    2. Append the new resolution and atomically sync and replace the page file.
    """
    path = root / book_id / f"{page}.json"
    record = json.loads(path.read_text()) if path.exists() else {"history": []}
    record["history"].append({**resolution, "time": datetime.now(UTC).isoformat()})
    save_reading(book_id, page, record, root)


def private_api():
    api = Blueprint("resolutions", __name__, url_prefix="/api/private/resolve")
    diagnostics = {}

    def cached_pages(book):
        return load_cache(book, roots()[0], cache_root(), diagnostics)

    def cache_root():
        return current_app.config.get("RESOLVE_CACHE_ROOT", CACHE_ROOT)

    @api.errorhandler(DiagnosticsUnavailable)
    def diagnostics_unavailable(error):
        return jsonify(error=str(error)), 503

    @api.errorhandler(OSError)
    @api.errorhandler(ValueError)
    def source_unavailable(error):
        return jsonify(error="Source files are temporarily unavailable."), 503

    @api.errorhandler(404)
    def missing_page(error):
        return jsonify(error="Unknown book or page"), 404

    def roots():
        return (
            current_app.config.get("OCR_ROOT", OCR_ROOT),
            current_app.config.get("RESOLUTIONS_ROOT", ROOT),
        )

    def books():
        return load_manifest(current_app.config["SOURCES_DIR"])

    def page_detail(book_id, number):
        book = books().get(book_id)
        if book is None:
            abort(404)
        pages = cached_pages(book)
        if (
            not 1 <= number <= len(pages)
            or pages[number - 1]["text_source"] == "hidden"
        ):
            abort(404)
        page = pages[number - 1]
        resolution = load_resolution(book_id, number, roots()[1])
        page = {
            **page,
            "record": load_reading(book_id, number, roots()[0]),
            "resolution": resolution,
            "rules_failed": [] if resolution else page["rules_failed"],
        }
        printed = page["printed_page"]
        return book, page, printed

    def body():
        # The tailnet is the lock; reject cross-origin browser writes to it.
        if request.headers.get("Origin") and request.headers[
            "Origin"
        ] != request.host_url.rstrip("/"):
            abort(400, description="Use the resolve screen on this service")
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400, description="Supply a JSON object")
        return value

    @api.before_request
    def private_body_limit():
        request.max_content_length = 420_000

    @api.get("/queue")
    def queue():
        rows = []
        catalogue = books()
        selected = request.args.get("book")
        if selected and selected not in catalogue:
            abort(404)
        reason = request.args.get("reason")
        if reason and reason not in REASONS:
            abort(400, description="Unknown queue reason")
        for book in catalogue.values():
            if selected and selected != book.id:
                continue
            for page in cached_pages(book):
                reasons = page["rules_failed"]
                if not reasons or load_resolution(
                    book.id, page["pdf_page"], roots()[1]
                ):
                    continue
                if reasons and (not reason or reason in reasons):
                    rows.append(
                        dict(
                            book_id=book.id,
                            title=book.title,
                            pdf_page=page["pdf_page"],
                            reasons=reasons,
                        )
                    )
        return jsonify(
            queue=rows,
            books=[dict(id=b.id, title=b.title) for b in catalogue.values()],
            reasons=REASONS,
        )

    @api.get("/page/<book_id>/<int:number>")
    def detail(book_id, number):
        book, page, printed = page_detail(book_id, number)
        return jsonify(
            book_id=book.id,
            title=book.title,
            pdf_page=number,
            text=page["text"],
            generated=bool(page["record"]),
            printed_page=printed,
            reasons=page["rules_failed"],
            reason_messages=[REASONS[r] for r in page["rules_failed"]],
            resolution=page["resolution"],
        )

    @api.post("/page/<book_id>/<int:number>/resolution")
    def resolve(book_id, number):
        data = body()
        _, resolution_root = roots()
        # Validate the id through the catalogue before using it as a path.
        if book_id not in books():
            abort(404)
        with book_lock(resolution_root, book_id):
            _, page, printed = page_detail(book_id, number)
            previous = page["resolution"]
            text = previous["text"] if previous else page["text"]
            printed = previous["printed_page"] if previous else printed
            action = data.get("action")
            if action == "number":
                printed = data.get("printed_page")
                if not isinstance(printed, str) or not _number(printed):
                    abort(400, description="Enter a whole printed page number")
                printed = printed.strip()
            elif action == "text":
                text = data.get("text")
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or len(text) > 100_000
                    or "\x00" in text
                ):
                    abort(
                        400,
                        description="Enter 1–100000 text characters without NUL",
                    )
            elif action == "hinted":
                attempts = (page["record"] or {}).get("readings", [])
                candidate = next(
                    (
                        r
                        for r in attempts
                        if r.get("attempt_id") == data.get("attempt_id")
                        and r.get("hinted")
                    ),
                    None,
                )
                if candidate is None:
                    abort(400, description="Unknown hinted attempt")
                text = candidate["text"]
                printed = next(iter(reading_candidates(text).values()), None)
            elif action != "confirm":
                abort(400, description="Unknown resolution action")
            if action == "text":
                # Typed text can include its printed number at either edge.
                printed = next(iter(reading_candidates(text).values()), printed)
            save_resolution(
                book_id,
                number,
                dict(
                    action=action,
                    text=text,
                    printed_page=printed,
                    attempt_id=data.get("attempt_id") if action == "hinted" else None,
                ),
                resolution_root,
            )
        return jsonify(saved=True, applies="next rebuild")

    @api.post("/page/<book_id>/<int:number>/hint")
    def hint(book_id, number):
        data = body()
        hint = data.get("hint")
        if (
            not isinstance(hint, str)
            or not 1 <= len(hint.strip()) <= 4000
            or "\x00" in hint
        ):
            abort(400, description="Enter a hint of 1–4000 characters without NUL")
        book, page, _ = page_detail(book_id, number)
        ocr_root, resolution_root = roots()
        with book_lock(resolution_root, book_id):
            with pymupdf.open(book.path) as pdf:
                png = pdf[number - 1].get_pixmap(dpi=DPI, alpha=False).tobytes("png")
            started = perf_counter()
            try:
                endpoint = current_app.config.get("VISION_ENDPOINT") or VisionEndpoint()
                attempt = endpoint.read(png, hint=hint.strip())
            except (VisionUnavailable, KeyError):
                return jsonify(
                    error="Vision is not serving. Ask for it to be started."
                ), 503
            except VisionPageTimeout:
                return jsonify(
                    error="The hinted reading timed out. Try again when ready."
                ), 503
            except Exception:
                return jsonify(
                    error="The hinted reading is unavailable. Try again."
                ), 503
            attempt.update(
                hinted=True,
                hint=hint.strip(),
                attempt_id=uuid4().hex,
                rung="vision",
                rotated=False,
                method_version=METHOD_VERSION,
                seconds=perf_counter() - started,
                time=datetime.now(UTC).isoformat(),
            )
            attempt["rules_failed"] = failed_rules(
                attempt["text"],
                book.language,
                "ocr",
                page["ink"],
                reading_candidates(attempt["text"]),
                page["neighbours"],
                attempt["finish_reason"],
            )
            # Reload under the lock; append only, never promote the candidate.
            record = load_reading(book_id, number, ocr_root)
            if record is None:
                record = dict(
                    method_version=METHOD_VERSION,
                    readings=[],
                    complete=True,
                    rotated=False,
                    seconds=0,
                    model="",
                    text=page["text"],
                    finish_reason="stop",
                    ink=page["ink"],
                    rules_failed=page["rules_failed"],
                    previous_versions=[],
                )
                path = ocr_root / book_id / f"{number}.json"
                if path.exists():
                    record["previous_versions"].append(json.loads(path.read_text()))
            record["readings"].append(attempt)
            save_reading(book_id, number, record, ocr_root)
            keep_hint_cache_current(book_id, number, ocr_root, cache_root())
        return jsonify(
            attempt_id=attempt["attempt_id"],
            text=attempt["text"],
            generated=True,
            reason_messages=[REASONS[r] for r in attempt["rules_failed"]],
        )

    @api.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    return api
