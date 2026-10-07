"""Hand-run, resumable ingestion and application-log export."""

import argparse
import json
import os
from pathlib import Path

from med_ask.database import Database
from med_ask.embedding import Endpoint
from med_ask.manifest import load_manifest
from med_ask.retrieval import ingest_book, ingest_status


def main():
    """1. Parse the ingest, status, or export command and selected book ids.
    2. Initialize application bookkeeping in Postgres.
    3. Run the selected operation and print progress and a final summary.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ingest").add_argument("books", nargs="+")
    commands.add_parser("status").add_argument("books", nargs="*")
    commands.add_parser("export-questions")
    args = parser.parse_args()
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
