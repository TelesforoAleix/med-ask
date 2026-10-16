"""Private, human labels for logged questions; source files stay outside git."""

import fcntl
import json
import os
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

import pymupdf
from flask import Blueprint, abort, current_app, jsonify, request
from psycopg.rows import dict_row

from med_ask.database import Database
from med_ask.embedding import Endpoint
from med_ask.evaluation import parse_record
from med_ask.generation import detect_language
from med_ask.manifest import load_manifest
from med_ask.retrieval import managed_store, search, vector_store

REAL_FILE = Path("/data/originals/eval/med-ask-eval-real.v1.jsonl")


def logged_questions(database):
    """1. Read logged questions newest first, without reading askers or feedback.
    2. Keep their original candidate order, grades and index table for recovery.
    """
    with (
        database.connect() as connection,
        connection.cursor(row_factory=dict_row) as cursor,
    ):
        cursor.execute(
            "SELECT id, question, language, asked_at, answer, evidence, "
            "grades, vector_table FROM medask_questions ORDER BY asked_at DESC, id"
        )
        return [
            {**row, "id": str(row["id"]), "asked_at": row["asked_at"].isoformat()}
            for row in cursor
        ]


def recover_candidates(row, database):
    """1. Read candidate nodes from the question's recorded index with library lookup.
    2. Try the matching figure table, if present, for ids absent from that index.
    3. Read book and inclusive PDF range from metadata, preserving logged rank.
    4. Keep missing or unusable metadata unresolved; never infer a page from a label.
    """
    grades = row.get("grades") or []
    table = row.get("vector_table")
    found = {}
    if table and grades:
        recorded = database.model(table)
        if recorded and recorded[2]:
            tables = [table]
            figure = "f_" + table[2:] if table.startswith("e_") else None
            if figure:
                with database.connect() as connection:
                    exists = connection.execute(
                        "SELECT to_regclass(%s) IS NOT NULL", ("public.data_" + figure,)
                    ).fetchone()[0]
                if exists:
                    tables.append(figure)
            for source in tables:
                missing = [g["id"] for g in grades if g["id"] not in found]
                if not missing:
                    break
                with managed_store(
                    vector_store, database.url, source, recorded[1]
                ) as store:
                    for node in store.get_nodes(node_ids=missing):
                        metadata = node.metadata
                        pages = metadata.get("pdf_pages")
                        if (
                            isinstance(metadata.get("book_id"), str)
                            and isinstance(pages, (list, tuple))
                            and len(pages) == 2
                            and all(type(p) is int for p in pages)
                            and 1 <= pages[0] <= pages[1]
                        ):
                            found[node.node_id] = dict(
                                book_id=metadata["book_id"], pdf_pages=list(pages)
                            )
    return [
        dict(id=g["id"], rank=rank, grade=g.get("grade"), **found.get(g["id"], {}))
        for rank, g in enumerate(grades, 1)
    ]


def logged_suggestions(row, database):
    """1. Recover candidates when grades preserve their original order.
    2. Otherwise keep legacy logged evidence pages without inventing grades.
    """
    if row.get("grades"):
        return recover_candidates(row, database)
    return [
        dict(book_id=e["book_id"], pdf_pages=e["pdf_pages"])
        for e in row.get("evidence", [])
    ]


def read_labels(path):
    """1. Read every complete record, or return an empty list before the first save.
    2. Refuse malformed or duplicate records rather than lose them on a later save.
    """
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    seen = set()
    for record in records:
        parse_record(record)
        if record["id"] in seen:
            raise ValueError("Duplicate label id")
        seen.add(record["id"])
    return records


def expected_pages(record, books):
    """1. Map saved eval book ids back to manifest books and individual PDF pages."""
    from med_ask.evaluation import page_numbers

    mapped = {b.eval_book: b.id for b in books.values() if b.eval_book}
    return [
        dict(book_id=mapped[e["book"]], pdf_page=p)
        for e in (record or {}).get("evidence", [])
        for item in e["pages"]
        for p in page_numbers(item)
        if e["book"] in mapped
    ]


def label_state(record):
    """1. Distinguish missing labels, out-of-sources labels and expected-page labels."""
    return (
        "unlabelled"
        if record is None
        else "out of the sources"
        if record["expect_not_found"]
        else "in scope"
    )


def save_label(path, row, action, books, page=None):
    """1. Lock the eval directory across workers before reading the whole label file.
    2. Apply one page addition, removal, out-of-sources label or clear operation.
    3. Map books to confirmed evidence and detect language only for null log values.
    4. Sync and atomically replace the file, preserving all other records.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    temporary = None
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        records = read_labels(path)
        identity = "r" + UUID(row["id"]).hex[:12]
        previous = next((r for r in records if r["id"] == identity), None)
        if previous and previous.get("log_id") != row["id"]:
            raise ValueError("Label id collision")
        pages = expected_pages(previous, books)
        if action == "add":
            if page not in pages:
                pages.append(page)
        elif action == "remove":
            pages = [p for p in pages if p != page]
        elif action in {"clear", "outside"}:
            pages = []
        else:
            raise ValueError("Unknown label action")
        record = None
        if pages or action == "outside":
            evidence = []
            for book_id in sorted({p["book_id"] for p in pages}):
                book = books[book_id]
                if not book.eval_book:
                    raise ValueError("Book has no eval id")
                evidence.append(
                    dict(
                        book=book.eval_book,
                        work=book.id,
                        state="confirmed",
                        pages=[
                            dict(pdf=p["pdf_page"], text="ok")
                            for p in sorted(pages, key=lambda p: p["pdf_page"])
                            if p["book_id"] == book_id
                        ],
                    )
                )
            record = dict(
                schema=1,
                id=identity,
                log_id=row["id"],
                set_version=1,
                status="confirmed",
                question=row["question"],
                language=row["language"]
                if row["language"] is not None
                else detect_language(row["question"]),
                language_source="log" if row["language"] is not None else "detected",
                type="unclassified",
                subject="unclassified",
                expect_not_found=action == "outside",
                evidence=evidence,
            )
            parse_record(record)
        records = [r for r in records if r["id"] != identity]
        if record:
            records.append(record)
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".real-eval-",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            for item in records:
                file.write(json.dumps(item, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
        os.fsync(directory)
        return record
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        os.close(directory)


def suggested_pages(candidates, wider, books):
    """1. Put logged passing candidates before other logged candidates and wider hits.
    2. Expand inclusive PDF ranges and keep each book/page pair only once.
    """
    rows, seen = [], set()
    ordered = sorted(candidates, key=lambda c: c.get("grade") is not True)
    ordered += [dict(book_id=e.book_id, pdf_pages=e.pdf_pages) for e in wider]
    for candidate in ordered:
        book = books.get(candidate.get("book_id"))
        if not book or "pdf_pages" not in candidate:
            continue
        start, end = candidate["pdf_pages"]
        for page in range(start, end + 1):
            key = (book.id, page)
            if key not in seen:
                rows.append(
                    dict(
                        book_id=book.id,
                        title=book.title,
                        pdf_page=page,
                        passed=candidate.get("grade") is True,
                    )
                )
                seen.add(key)
    return rows


def private_api():
    api = Blueprint("labels", __name__, url_prefix="/api/private/label")

    def database():
        return current_app.config.get("QUESTION_DATABASE") or Database(
            current_app.config["DATABASE_URL"]
        )

    def books():
        return load_manifest(current_app.config["SOURCES_DIR"])

    def path():
        return current_app.config.get("REAL_EVAL_FILE", REAL_FILE)

    def row(identity):
        try:
            identity = str(UUID(identity))
        except ValueError:
            abort(404)
        value = next(
            (r for r in logged_questions(database()) if r["id"] == identity), None
        )
        if value is None:
            abort(404)
        return value

    def record(identity):
        return next(
            (r for r in read_labels(path()) if r.get("log_id") == identity), None
        )

    @api.before_request
    def guard():
        request.max_content_length = 32_000
        if (
            request.method == "POST"
            and request.headers.get("Origin")
            and request.headers["Origin"] != request.host_url.rstrip("/")
        ):
            abort(400, description="Use the label screen on this service")

    @api.errorhandler(Exception)
    def unavailable(error):
        from werkzeug.exceptions import HTTPException

        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        return jsonify(error="Question labels are temporarily unavailable."), 503

    @api.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @api.get("/questions")
    def listing():
        """1. Filter logs by inclusive dates and attach saved label states."""
        dates = {}
        for name in ("from", "to"):
            value = request.args.get(name)
            if value:
                try:
                    dates[name] = date.fromisoformat(value).isoformat()
                except ValueError:
                    abort(400, description="Use ISO dates")
        labels = {r["log_id"]: r for r in read_labels(path())}
        rows = [
            dict(
                id=r["id"],
                question=r["question"],
                language=r["language"],
                asked_at=r["asked_at"],
                label=label_state(labels.get(r["id"])),
            )
            for r in logged_questions(database())
            if ("from" not in dates or r["asked_at"][:10] >= dates["from"])
            and ("to" not in dates or r["asked_at"][:10] <= dates["to"])
        ]
        return jsonify(questions=rows)

    @api.get("/question/<identity>")
    def detail(identity):
        """1. Show the question and label beside recovered or legacy pages."""
        value = row(identity)
        saved = record(value["id"])
        candidates = logged_suggestions(value, database())
        return jsonify(
            id=value["id"],
            question=value["question"],
            asked_at=value["asked_at"],
            language=value["language"],
            answer=value["answer"],
            label=label_state(saved),
            expected=expected_pages(saved, books()),
            suggestions=suggested_pages(candidates, [], books()),
            unresolved=sum("pdf_pages" not in c for c in candidates),
        )

    @api.get("/question/<identity>/suggestions")
    def wider(identity):
        """1. Append wider search pages after logged pages without grading."""
        value = row(identity)
        candidates = logged_suggestions(value, database())
        hits, _, _ = search(
            value["question"],
            current_app.config.get("EMBEDDING_ENDPOINT") or Endpoint(),
            database(),
            k=100,
        )
        return jsonify(suggestions=suggested_pages(candidates, hits, books()))

    @api.post("/question/<identity>/label")
    def save(identity):
        """1. Validate the action and source page, then save the label atomically."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get("action") not in {
            "add",
            "remove",
            "outside",
            "clear",
        }:
            abort(400, description="Supply a label action")
        value = row(identity)
        catalogue = books()
        page = None
        if data["action"] in {"add", "remove"}:
            book = catalogue.get(data.get("book_id"))
            number = data.get("pdf_page")
            if book is None or not book.eval_book or type(number) is not int:
                abort(400, description="Supply an indexed book and PDF page")
            with pymupdf.open(book.path) as pdf:
                if not 1 <= number <= len(pdf):
                    abort(400, description="PDF page is out of range")
            page = dict(book_id=book.id, pdf_page=number)
        saved = save_label(path(), value, data["action"], catalogue, page)
        return jsonify(
            label=label_state(saved), expected=expected_pages(saved, catalogue)
        )

    return api
