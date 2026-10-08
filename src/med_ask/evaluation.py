"""Retrieval evaluation against the private eval set, reporting ids and numbers only.

Question text is read only to embed it. It is never printed, logged or stored.
"""

import json
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from med_ask.retrieval import NoIndex

SCHEMA = 1
RUN_FORMAT = 1
STATUSES = frozenset({"draft", "pages_open", "confirmed", "retired"})
SELECTABLE_STATUSES = STATUSES - {"retired"}
STATES = frozenset({"open", "confirmed", "absent", "ocr_missing", "not_indexed"})
LANGUAGES = frozenset({"en", "es", "ca"})
TYPES = frozenset({"definition", "consequence", "interaction"})
SUBJECTS = frozenset(
    {"cell_biology", "genetics", "biochemistry", "homeostasis", "outside"}
)
PAGE_TEXT = frozenset({"ok", "missing"})
TIMING_SAMPLE = 20
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,32}")


class EvalError(RuntimeError):
    """A refusal whose message holds no question text."""


class Malformed(ValueError):
    """A record that breaks schema 1; its message is a fixed, text-free reason."""


@dataclass(frozen=True)
class Record:
    id: str
    set_version: int
    status: str
    question: str
    language: str
    type: str
    subject: str
    expect_not_found: bool
    evidence: tuple[dict, ...]


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def page_numbers(item) -> list[int]:
    """1. Accept a single PDF page written as `pdf`.
    2. Or accept an inclusive range written as `pdf_from` and `pdf_to`.
    3. Refuse anything else, because a guessed page could invent a hit.
    """
    if not isinstance(item, dict):
        raise Malformed("page item is not an object")
    if item.get("text") not in PAGE_TEXT:
        raise Malformed("page item text is neither ok nor missing")
    single = "pdf" in item
    ranged = "pdf_from" in item or "pdf_to" in item
    if single and not ranged and _integer(item["pdf"]) and item["pdf"] >= 1:
        return [item["pdf"]]
    if ranged and not single:
        start, end = item.get("pdf_from"), item.get("pdf_to")
        if _integer(start) and _integer(end) and 1 <= start <= end:
            return list(range(start, end + 1))
    raise Malformed("page item is neither a pdf page nor a pdf_from/pdf_to range")


def parse_record(data) -> Record:
    """1. Check the fields scoring relies on against schema 1.
    2. Check every evidence entry's book, work, state and page items.
    3. Raise a fixed reason, never a field's value, when anything does not fit.
    """
    if not isinstance(data, dict):
        raise Malformed("record is not an object")
    if not _integer(data.get("schema")) or data["schema"] != SCHEMA:
        raise Malformed("wrong schema")
    if not _integer(data.get("set_version")) or data["set_version"] < 1:
        raise Malformed("set_version is not a positive integer")
    if data.get("status") not in STATUSES:
        raise Malformed("unknown status")
    if not isinstance(data.get("question"), str) or not data["question"].strip():
        raise Malformed("question is empty")
    for name, allowed in (
        ("language", LANGUAGES),
        ("type", TYPES),
        ("subject", SUBJECTS),
    ):
        if data.get(name) not in allowed:
            raise Malformed(f"unknown {name}")
    if not isinstance(data.get("expect_not_found"), bool):
        raise Malformed("expect_not_found is not a boolean")
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list):
        raise Malformed("evidence is not a list")
    for entry in evidence:
        if not isinstance(entry, dict):
            raise Malformed("evidence entry is not an object")
        for name in ("book", "work"):
            if not isinstance(entry.get(name), str) or not entry[name].strip():
                raise Malformed(f"evidence {name} is missing")
        if entry.get("state") not in STATES:
            raise Malformed("unknown evidence state")
        pages = entry.get("pages", [])
        if not isinstance(pages, list):
            raise Malformed("evidence pages is not a list")
        for item in pages:
            page_numbers(item)
    return Record(
        data["id"],
        data["set_version"],
        data["status"],
        data["question"],
        data["language"],
        data["type"],
        data["subject"],
        data["expect_not_found"],
        tuple(evidence),
    )


def load_eval(path: Path) -> tuple[list[Record], list[dict]]:
    """1. Read the JSON Lines file one non-blank line at a time.
    2. Name each malformed record by its id, or by line number when the id is unusable.
    3. Treat a repeated id as malformed rather than choosing between the copies.
    4. Return well-formed records in file order, and the malformed reports.
    """
    records, malformed, seen = [], [], set()
    with path.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                malformed.append({"line": number, "reason": "not valid JSON"})
                continue
            identity = data.get("id") if isinstance(data, dict) else None
            where = (
                {"id": identity}
                if isinstance(identity, str) and SAFE_ID.fullmatch(identity)
                else {"line": number}
            )
            if "id" not in where:
                malformed.append({**where, "reason": "missing or unusable id"})
                continue
            if identity in seen:
                malformed.append({**where, "reason": "duplicate id"})
                continue
            seen.add(identity)
            try:
                records.append(parse_record(data))
            except Malformed as error:
                malformed.append({**where, "reason": str(error)})
    return records, malformed


def scoreable_evidence(record: Record, indexed: set[str], statuses) -> dict:
    """1. Score only answerable records whose status the run selected.
    2. Keep only confirmed evidence for books the manifest maps to an index.
    3. Leave out pages without usable OCR text.
    4. Return each remaining eval book with its work and its set of PDF pages.
    """
    if record.expect_not_found or record.status not in statuses:
        return {}
    books = {}
    for entry in record.evidence:
        if entry["state"] != "confirmed" or entry["book"] not in indexed:
            continue
        pages = {
            page
            for item in entry.get("pages", [])
            if item["text"] != "missing"
            for page in page_numbers(item)
        }
        if pages:
            book = books.setdefault(
                entry["book"], {"work": entry["work"], "pages": set()}
            )
            book["pages"] |= pages
    return books


def score_results(results, evidence: dict, eval_books: dict[str, str]) -> dict:
    """1. Map each retrieved passage's manifest book to its eval book.
    2. Mark a passage as a hit when any page in its PDF range is a confirmed page.
    3. Derive Hit@5, Hit@10, the first hit's rank, and per-book Hit@5.
    4. Count each evidence work once when a top-10 hit comes from any of its editions.
    """
    ranked = []
    for rank, item in enumerate(results, 1):
        book = eval_books.get(item.book_id)
        start, end = item.pdf_pages
        hit = book in evidence and any(
            p in evidence[book]["pages"] for p in range(start, end + 1)
        )
        ranked.append({"rank": rank, "book": book, "hit": hit})
    hits = [r for r in ranked if r["hit"]]
    works = {b["work"] for b in evidence.values()}
    covered = {evidence[r["book"]]["work"] for r in hits if r["rank"] <= 10}
    return {
        "hit5": any(r["rank"] <= 5 for r in hits),
        "hit10": any(r["rank"] <= 10 for r in hits),
        "first_hit_rank": hits[0]["rank"] if hits else None,
        "books_hit5": {
            book: any(r["rank"] <= 5 and r["book"] == book for r in hits)
            for book in sorted(evidence)
        },
        "works": len(works),
        "works_covered10": len(covered),
        "hits": [r["hit"] for r in ranked],
    }


def _spread(values) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "median": round(statistics.median(values), 4),
        "max": round(max(values), 4),
    }


def summarise(questions: list[dict]) -> dict:
    """1. Gate on Hit@5 over scored answerable questions; report Hit@10 beside it.
    2. Average work coverage at 10 over questions with evidence in two or more works.
    3. Break Hit@5 down by language, type, subject, and evidence book.
    4. Take embedding time from the first twenty retrieved questions in file order.
    5. Set the top similarity of answerable questions beside not-found questions.
    """
    scored = [q for q in questions if q["scored"]]
    multi = [q for q in scored if q["works"] >= 2]
    groups = {}
    for name in ("language", "type", "subject"):
        groups[name] = {}
        for q in scored:
            cell = groups[name].setdefault(q[name], {"n": 0, "hit5": 0})
            cell["n"] += 1
            cell["hit5"] += q["hit5"]
    groups["book"] = {}
    for q in scored:
        for book, hit in q["books_hit5"].items():
            cell = groups["book"].setdefault(book, {"n": 0, "hit5": 0})
            cell["n"] += 1
            cell["hit5"] += hit
    groups = {name: dict(sorted(cells.items())) for name, cells in groups.items()}
    timed = [q["embed_seconds"] for q in questions][:TIMING_SAMPLE]
    top = [q for q in questions if q["top_score"] is not None]
    return {
        "scored": len(scored),
        "hit5": sum(q["hit5"] for q in scored),
        "hit10": sum(q["hit10"] for q in scored),
        "coverage10": {
            "questions": len(multi),
            "mean": round(
                statistics.mean(q["works_covered10"] / q["works"] for q in multi), 4
            )
            if multi
            else None,
        },
        "hit5_by": groups,
        "embedding_seconds": {
            "sample": len(timed),
            "median": round(statistics.median(timed), 4) if timed else None,
            "slowest": round(max(timed), 4) if timed else None,
        },
        "top_score": {
            "answerable": _spread(
                [q["top_score"] for q in top if not q["expect_not_found"]]
            ),
            "not_found": _spread(
                [q["top_score"] for q in top if q["expect_not_found"]]
            ),
        },
    }


def run_eval(eval_path, runs_dir, books, retrieve, purpose, statuses, output=print):
    """1. Load the eval file, setting malformed records aside by id.
    2. Map manifest books to eval books; unmapped eval books count as not indexed.
    3. Warm the endpoint once with fixed text so a cold start is not timed.
    4. Retrieve each non-retired record with the app's search, keeping ids,
       ranks, scores and timings only.
    5. Score selected answerable records; report the rest as skipped with a reason.
    6. Write the result file under the runs directory and print a short summary.
    """
    records, malformed = load_eval(Path(eval_path))
    statuses = frozenset(statuses)
    eval_books = {b.id: b.eval_book for b in books.values() if b.eval_book}
    indexed = set(eval_books.values())
    active = [r for r in records if r.status != "retired"]
    if not active:
        raise EvalError("No well-formed, non-retired record to run")

    def retrieved(identity, text):
        try:
            return retrieve(text)
        except NoIndex as error:
            raise EvalError(str(error)) from None
        except Exception as error:
            # Library and endpoint errors may echo the question; give only its type.
            raise EvalError(
                f"Retrieval failed for {identity} ({type(error).__name__})"
            ) from None

    _, table, _ = retrieved("warm-up", "med-ask eval warm-up")
    questions, skipped = [], []
    for record in active:
        results, table, seconds = retrieved(record.id, record.question)
        evidence = scoreable_evidence(record, indexed, statuses)
        score = score_results(results, evidence, eval_books)
        flags = score.pop("hits")
        question = {
            "id": record.id,
            "status": record.status,
            "expect_not_found": record.expect_not_found,
            "language": record.language,
            "type": record.type,
            "subject": record.subject,
            "scored": bool(evidence),
            "embed_seconds": round(seconds, 4),
            "top_score": round(results[0].score, 4) if results else None,
            "results": [
                {
                    "rank": rank,
                    "passage": item.id,
                    "book_id": item.book_id,
                    "pdf_pages": list(item.pdf_pages),
                    "score": round(item.score, 4),
                    "hit": hit,
                }
                for rank, (item, hit) in enumerate(zip(results, flags, strict=True), 1)
            ],
            **score,
        }
        questions.append(question)
        if not record.expect_not_found and not evidence:
            reason = (
                "status not selected"
                if record.status not in statuses
                else "no scoreable evidence"
            )
            skipped.append({"id": record.id, "reason": reason})
    summary = summarise(questions)
    created = datetime.now(UTC)
    run = {
        "kind": "med-ask-eval-run",
        "format": RUN_FORMAT,
        "created_at": created.isoformat(timespec="seconds"),
        "eval_file": Path(eval_path).name,
        "set_version": max(r.set_version for r in records),
        "purpose": purpose,
        "table": table,
        "statuses": sorted(statuses),
        "eval_books": dict(sorted(eval_books.items())),
        "counts": {
            "read": len(records) + len(malformed),
            "malformed": len(malformed),
            "retired": len(records) - len(active),
            "retrieved": len(questions),
            "answerable": sum(not q["expect_not_found"] for q in questions),
            "not_found": sum(q["expect_not_found"] for q in questions),
            "scored": summary["scored"],
            "skipped": len(skipped),
        },
        "malformed": malformed,
        "skipped": skipped,
        "summary": summary,
        "questions": sorted(questions, key=lambda q: q["id"]),
    }
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(exist_ok=True)
    stem = re.sub(r"[^a-z0-9_-]+", "-", purpose.lower()).strip("-") or "purpose"
    path = runs_dir / (f"run-{created:%Y%m%dT%H%M%SZ}-{stem}-{uuid4().hex[:6]}.json")
    with path.open("x", encoding="utf-8") as file:
        json.dump(run, file, indent=2)
        file.write("\n")
    for line in summary_lines(run):
        output(line, flush=True)
    output(f"result: {path}", flush=True)
    return path


def _rate(hits, n) -> str:
    return f"{hits}/{n} ({hits / n:.0%})" if n else "0/0"


def summary_lines(run: dict) -> list[str]:
    """1. Describe the run's set, purpose and table.
    2. Count records read, malformed, retired, scored and skipped, naming ids.
    3. Give Hit@5 and its diagnostics, or say plainly that nothing was scored.
    4. Give embedding timings and the top-score comparison.
    """
    c, s = run["counts"], run["summary"]
    lines = [
        f"eval: set_version={run['set_version']} purpose={run['purpose']} "
        f"table={run['table']} statuses={','.join(run['statuses'])}",
        f"records: read={c['read']} malformed={c['malformed']} "
        f"retired={c['retired']} retrieved={c['retrieved']} "
        f"answerable={c['answerable']} not_found={c['not_found']}",
    ]
    for item in run["malformed"]:
        where = item.get("id") or f"line {item['line']}"
        lines.append(f"malformed: {where}: {item['reason']}")
    reasons = {}
    for item in run["skipped"]:
        reasons.setdefault(item["reason"], []).append(item["id"])
    lines.append(f"scored={c['scored']} skipped={c['skipped']}")
    for reason, ids in sorted(reasons.items()):
        lines.append(f"skipped ({reason}): {len(ids)}: {' '.join(ids)}")
    if not s["scored"]:
        lines.append("Nothing scored: no answerable record has scoreable evidence.")
    else:
        lines.append(f"Hit@5 (gate): {_rate(s['hit5'], s['scored'])}")
        lines.append(f"Hit@10: {_rate(s['hit10'], s['scored'])}")
        cov = s["coverage10"]
        lines.append(
            f"work coverage@10: mean {cov['mean']} over {cov['questions']} "
            "multi-work questions"
            if cov["questions"]
            else "work coverage@10: no multi-work questions"
        )
        for group, cells in s["hit5_by"].items():
            parts = [f"{k} {_rate(v['hit5'], v['n'])}" for k, v in cells.items()]
            lines.append(f"Hit@5 by {group}: " + "; ".join(parts))
    e = s["embedding_seconds"]
    lines.append(
        f"embedding: median {e['median']}s, slowest {e['slowest']}s "
        f"over {e['sample']} questions"
    )
    for name in ("answerable", "not_found"):
        t = s["top_score"][name]
        lines.append(
            f"top score, {name.replace('_', '-')}: n={t['n']}"
            + (f" min={t['min']} median={t['median']} max={t['max']}" if t["n"] else "")
        )
    return lines


def load_run(path: Path) -> dict:
    """1. Read a result file and refuse anything that is not an eval run."""
    with Path(path).open(encoding="utf-8") as file:
        run = json.load(file)
    if (
        not isinstance(run, dict)
        or run.get("kind") != "med-ask-eval-run"
        or run.get("format") != RUN_FORMAT
    ):
        raise EvalError(f"{Path(path).name} is not an eval result file")
    return run


def compare_runs(baseline: dict, candidate: dict) -> dict:
    """1. Refuse runs made against different set versions.
    2. Collect the scored questions that hit at 5 in each run.
    3. Pass only when total Hit@5 does not drop and every lost hit is balanced
       by at least one gained hit.
    """
    if baseline["set_version"] != candidate["set_version"]:
        raise EvalError(
            "Refusing to compare runs on different set versions "
            f"({baseline['set_version']} and {candidate['set_version']})"
        )

    def hits(run):
        return {q["id"] for q in run["questions"] if q["scored"] and q["hit5"]}

    def scored(run):
        return {q["id"] for q in run["questions"] if q["scored"]}

    before, after = hits(baseline), hits(candidate)
    lost, gained = sorted(before - after), sorted(after - before)
    return {
        "passed": len(after) >= len(before) and (not lost or bool(gained)),
        "baseline": {"hit5": len(before), "scored": len(scored(baseline))},
        "candidate": {"hit5": len(after), "scored": len(scored(candidate))},
        "lost": lost,
        "gained": gained,
        "scored_only_in_baseline": sorted(scored(baseline) - scored(candidate)),
        "scored_only_in_candidate": sorted(scored(candidate) - scored(baseline)),
    }


def comparison_lines(result: dict, baseline: dict, candidate: dict) -> list[str]:
    """1. State pass or fail, the two Hit@5 totals, and the ids lost or gained."""
    b, c = result["baseline"], result["candidate"]
    lines = [
        f"gate: {'PASS' if result['passed'] else 'FAIL'}",
        f"baseline: purpose={baseline['purpose']} table={baseline['table']} "
        f"Hit@5 {_rate(b['hit5'], b['scored'])}",
        f"candidate: purpose={candidate['purpose']} table={candidate['table']} "
        f"Hit@5 {_rate(c['hit5'], c['scored'])}",
        "lost: " + (" ".join(result["lost"]) or "none"),
        "gained: " + (" ".join(result["gained"]) or "none"),
    ]
    for name in ("scored_only_in_baseline", "scored_only_in_candidate"):
        if result[name]:
            lines.append(f"{name.replace('_', ' ')}: {' '.join(result[name])}")
    if not b["scored"] and not c["scored"]:
        lines.append("Nothing scored in either run.")
    return lines
