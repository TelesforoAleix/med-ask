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
    load_run,
    run_eval,
)
from med_ask.manifest import load_manifest
from med_ask.retrieval import ingest_book, ingest_status, search

DEFAULT_EVAL_FILE = "/data/originals/eval/med-ask-eval.v1.jsonl"


def evaluate(args, parser):
    """1. Compare two result files when asked, applying the Hit@5 gate.
    2. Otherwise run every eval record through the app's search for one purpose.
    3. Report refusals by message only; question text never reaches the output.
    """
    eval_file = Path(os.environ.get("EVAL_FILE", DEFAULT_EVAL_FILE))
    runs = Path(os.environ.get("EVAL_RUNS_DIR", eval_file.parent / "runs"))
    try:
        if args.compare:
            baseline, candidate = (
                load_run(Path(p) if "/" in p else runs / p) for p in args.compare
            )
            result = compare_runs(baseline, candidate)
            for line in comparison_lines(result, baseline, candidate):
                print(line, flush=True)
            return 0 if result["passed"] else 1
        database = Database(os.environ["DATABASE_URL"])
        database.ensure()
        books = load_manifest(Path(os.environ.get("SOURCES_DIR", "/data/sources")))
        endpoint = Endpoint(args.purpose)
        run_eval(
            eval_file,
            runs,
            books,
            lambda question: search(question, endpoint, database),
            endpoint.purpose,
            args.status or ["confirmed"],
        )
        return 0
    except EvalError as error:
        parser.exit(2, f"eval: {error}\n")


def main():
    """1. Parse the ingest, status, export, or eval command and its arguments.
    2. Initialize application bookkeeping in Postgres.
    3. Run the selected operation and print progress and a final summary.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ingest").add_argument("books", nargs="+")
    commands.add_parser("status").add_argument("books", nargs="*")
    commands.add_parser("export-questions")
    evaluation = commands.add_parser("eval")
    evaluation.add_argument(
        "--purpose", help="Embedding purpose to evaluate (default EMBEDDING_PURPOSE)"
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
    args = parser.parse_args()
    if args.command == "eval":
        raise SystemExit(evaluate(args, parser))
    database = Database(os.environ["DATABASE_URL"])
    database.ensure()
    if args.command == "export-questions":
        path = database.export(Path("/data/originals"))
        print(f"Exported {path} ({path.stat().st_size} bytes)", flush=True)
        return
    books = load_manifest(Path(os.environ.get("SOURCES_DIR", "/data/sources")))
    unknown = set(args.books) - books.keys()
    if unknown:
        parser.error("Unknown book ids: " + ", ".join(sorted(unknown)))
    selected = [books[b] for b in args.books] if args.books else list(books.values())
    if args.command == "status":
        rows = ingest_status(selected, database)
    else:
        endpoint = Endpoint()
        rows = [ingest_book(book, endpoint, database) for book in selected]
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
