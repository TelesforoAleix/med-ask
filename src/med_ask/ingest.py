"""Hand-run, resumable ingestion, application-log export and retrieval eval."""

import argparse
import json
import os
from pathlib import Path

from med_ask.database import Database
from med_ask.embedding import Endpoint
from med_ask.evaluation import (
    SELECTABLE_STATUSES,
    EvalError,
    compare_runs,
    comparison_lines,
    figure_gate,
    load_run,
    run_eval,
    run_real_eval,
)
from med_ask.extract import _external_output, extract_book
from med_ask.figures import describe_figures, figure_entries
from med_ask.manifest import load_manifest
from med_ask.navigation import build_navigation
from med_ask.ocr import ROOT as OCR_ROOT
from med_ask.ocr import (
    VisionEndpoint,
    VisionUnavailable,
    queue_summary,
    read_book,
    recheck_book,
)
from med_ask.resolve_cache import build_cache
from med_ask.retrieval import ingest_book, ingest_status, search

DEFAULT_EVAL_FILE = "/data/originals/eval/med-ask-eval.v2.jsonl"


def passage_candidates():
    return int(os.environ.get("PASSAGE_CANDIDATES", "26"))


def evaluate(args, parser):
    """1. Apply the requested comparison or figure gate to existing result files.
    2. Score served logs or repeat retrieval and grading for a real-case mode.
    3. Otherwise run every eval record through the app's search for one purpose.
    4. Report refusals by message only; question text never reaches the output.
    """
    eval_file = Path(os.environ.get("EVAL_FILE", DEFAULT_EVAL_FILE))
    runs = Path(os.environ.get("EVAL_RUNS_DIR", eval_file.parent / "runs"))
    try:
        if args.figure_gate:
            if not args.cohort:
                parser.error("--figure-gate requires --cohort")
            baseline, candidate = (load_run(Path(p)) for p in args.figure_gate)
            cohort = json.loads(args.cohort.read_text())["cohort_ids"]
            result = figure_gate(baseline, candidate, cohort)
            print(json.dumps(result, indent=2), flush=True)
            return 0 if result["passed"] else 1
        if args.compare:
            baseline, candidate = (
                load_run(Path(p) if "/" in p else runs / p) for p in args.compare
            )
            result = compare_runs(baseline, candidate)
            for line in comparison_lines(result, baseline, candidate):
                print(line, flush=True)
            return 0 if result["passed"] else 1
        database = Database(os.environ["DATABASE_URL"])
        if not (args.served or args.end_to_end):
            database.ensure()
        books = load_manifest(Path(os.environ.get("SOURCES_DIR", "/data/sources")))
        if args.served or args.end_to_end:
            from med_ask.generation import GenerationEndpoint, grade_candidates
            from med_ask.labels import REAL_FILE, logged_questions, recover_candidates

            real_file = Path(os.environ.get("REAL_EVAL_FILE", REAL_FILE))
            if not real_file.exists() or not real_file.read_text().strip():
                print("Nothing labelled yet.", flush=True)
                return 0
            endpoint = Endpoint(args.purpose, args.roles) if args.end_to_end else None
            generation = GenerationEndpoint() if args.end_to_end else None
            run_real_eval(
                real_file,
                books,
                logged_questions(database),
                lambda row: recover_candidates(row, database),
                retrieve=(
                    lambda question: search(
                        question, endpoint, database, k=passage_candidates()
                    )
                )
                if args.end_to_end
                else None,
                grade=(
                    lambda question, hits: grade_candidates(question, hits, generation)
                )
                if args.end_to_end
                else None,
                cut=passage_candidates(),
            )
            return 0
        endpoint = Endpoint(args.purpose, args.roles)
        run_eval(
            eval_file,
            runs,
            books,
            lambda question: search(
                question, endpoint, database, k=passage_candidates()
            ),
            endpoint.purpose,
            args.status or ["confirmed"],
            roles=endpoint.roles,
            cut=passage_candidates(),
        )
        return 0
    except EvalError as error:
        parser.exit(2, f"eval: {error}\n")


def main():
    """1. Parse ingest, status, export, eval, reading or figure job arguments.
    2. Initialize Postgres only for commands that need application bookkeeping.
    3. Run the selected operation and print progress and a final summary.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ingest").add_argument("books", nargs="+")
    commands.add_parser("status").add_argument("books", nargs="*")
    commands.add_parser("export-questions")
    for name in ("figures-count", "figures-describe"):
        command = commands.add_parser(name)
        command.add_argument("books", nargs="+")
        command.add_argument("--output", type=Path, help="External JSON summary path")
        if name == "figures-describe":
            command.add_argument("--limit", type=int, help="Sample figures per book")
    reading = commands.add_parser(
        "ocr", help="Read failing pages, resuming kept readings"
    )
    reading.add_argument("books", nargs="+")
    reading.add_argument("--limit", type=int, help="Maximum new pages per book")
    commands.add_parser(
        "ocr-queue", help="List failing pages without model calls"
    ).add_argument("books", nargs="*")
    commands.add_parser(
        "ocr-recheck", help="Recheck saved readings without model calls"
    ).add_argument("books", nargs="*")
    commands.add_parser(
        "resolve-cache",
        help="Build resolve diagnostics without models or database writes",
    ).add_argument("books", nargs="*")
    navigate = commands.add_parser(
        "navigate", help="Build navigation without model calls"
    )
    navigate.add_argument("books", nargs="+")
    navigate.add_argument(
        "--output", type=Path, help="External counts-only report path"
    )
    evaluation = commands.add_parser("eval")
    evaluation.add_argument(
        "--purpose", help="Embedding purpose to evaluate (default EMBEDDING_PURPOSE)"
    )
    evaluation.add_argument(
        "--roles",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Query roles on/off (default EMBEDDING_ROLES)",
    )
    evaluation.add_argument(
        "--status",
        action="append",
        choices=sorted(SELECTABLE_STATUSES),
        help="Record status to score; repeatable (default confirmed)",
    )
    evaluation.add_argument(
        "--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"), help="Result files"
    )
    modes = evaluation.add_mutually_exclusive_group()
    modes.add_argument("--served", action="store_true")
    modes.add_argument("--end-to-end", action="store_true")
    evaluation.add_argument("--figure-gate", nargs=2, metavar=("BASELINE", "CANDIDATE"))
    evaluation.add_argument(
        "--cohort", type=Path, help="Private file containing cohort_ids"
    )
    args = parser.parse_args()
    if args.command == "eval":
        raise SystemExit(evaluate(args, parser))
    if (
        args.command in {"ocr", "figures-describe"}
        and args.limit is not None
        and args.limit < 1
    ):
        parser.error("--limit must be positive")
    database = None
    if args.command not in {
        "ocr",
        "ocr-queue",
        "ocr-recheck",
        "resolve-cache",
        "navigate",
        "figures-count",
        "figures-describe",
    }:
        database = Database(os.environ["DATABASE_URL"])
        database.ensure()
    if args.command == "export-questions":
        try:
            path = database.export(Path("/data/originals"))
        except ValueError as error:
            parser.exit(1, f"export-questions: {error}\n")
        print(f"Exported {path} ({path.stat().st_size} bytes)", flush=True)
        return
    books = load_manifest(Path(os.environ.get("SOURCES_DIR", "/data/sources")))
    unknown = set(args.books) - books.keys()
    if unknown:
        parser.error("Unknown book ids: " + ", ".join(sorted(unknown)))
    selected = [books[b] for b in args.books] if args.books else list(books.values())
    if args.command in {"figures-count", "figures-describe"}:
        rows = []
        endpoint = VisionEndpoint() if args.command == "figures-describe" else None
        for book in selected:
            figures = figure_entries(
                extract_book(book.path, book.id, book.language), pdf_path=book.path
            )
            if endpoint:
                try:
                    rows.append(
                        describe_figures(book, figures, endpoint, limit=args.limit)
                    )
                except VisionUnavailable:
                    parser.exit(
                        3, "figures-describe: vision unavailable; resume later\n"
                    )
            else:
                rows.append(
                    dict(
                        book_id=book.id,
                        figures=len(figures),
                        captionless=sum(not f.caption.strip() for f in figures),
                        unnumbered=sum(f.identifier == "?" for f in figures),
                    )
                )
        if args.output:
            path = _external_output(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    elif args.command == "ocr":
        endpoint = VisionEndpoint()
        for book in selected:
            if not read_book(book, endpoint, limit=args.limit):
                raise SystemExit(3)
        rows = [queue_summary(book) for book in selected]
    elif args.command == "navigate":
        rows = [build_navigation(book, ocr_root=OCR_ROOT) for book in selected]
        if args.output:
            path = _external_output(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    elif args.command == "resolve-cache":
        rows = [build_cache(book, OCR_ROOT) for book in selected]
    elif args.command == "ocr-queue":
        rows = [queue_summary(book) for book in selected]
    elif args.command == "ocr-recheck":
        for book in selected:
            recheck_book(book)
        rows = [queue_summary(book) for book in selected]
    elif args.command == "status":
        rows = ingest_status(selected, database)
    else:
        endpoint = Endpoint()
        rows = [ingest_book(book, endpoint, database) for book in selected]
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
