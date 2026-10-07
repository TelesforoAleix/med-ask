"""Load the external, operator-maintained book catalogue."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Book:
    id: str
    filename: str
    title: str
    language: str
    path: Path


def load_manifest(sources: Path) -> dict[str, Book]:
    """1. Read the book catalogue from the sources directory.
    2. Validate unique URL-safe ids, descriptive fields, and contained PDF paths.
    3. Return book records without reading any source text.
    """
    sources = sources.resolve()
    with (sources / "books.toml").open("rb") as file:
        entries = tomllib.load(file)["books"]
    books = {}
    for entry in entries:
        identity = entry["id"]
        filename = entry["filename"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identity):
            raise ValueError("Book ids must be URL-safe lowercase identifiers")
        if identity in books:
            raise ValueError("Duplicate book id")
        if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
            raise ValueError("Book filenames must be PDF basenames")
        path = (sources / filename).resolve()
        if path.parent != sources:
            raise ValueError("Book path escapes the sources directory")
        if not all(
            isinstance(entry[k], str) and entry[k].strip()
            for k in ("title", "language")
        ):
            raise ValueError("Books require a title and language")
        books[identity] = Book(
            identity, filename, entry["title"], entry["language"], path
        )
    return books
