"""Keep the document map, relative links, anchors and claim citations valid."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = sorted(
    {
        ROOT / "AGENTS.md",
        ROOT / "ARCHITECTURE.md",
        ROOT / "DECISIONS.md",
        ROOT / "README.md",
        *(ROOT / "docs").glob("*.md"),
    }
)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def prose_lines(path):
    """Yield (number, line) outside fenced code blocks."""
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def anchors(path):
    """Return the GitHub-style anchors of a Markdown file's headings."""
    seen, result = {}, set()
    for _, line in prose_lines(path):
        match = HEADING.match(line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match[1].strip().lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def map_targets():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## Document map", 1)[1].split("\n## ", 1)[0]
    return [
        LINK.search(line)[1]
        for line in section.splitlines()
        if line.startswith("- ") and LINK.search(line)
    ]


def test_document_map_names_existing_files():
    targets = map_targets()
    assert len(targets) >= 10
    for target in targets:
        assert (ROOT / target.split("#")[0]).is_file(), target


@pytest.mark.parametrize("path", CORE, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(path):
    for number, line in prose_lines(path):
        for target in LINK.findall(line):
            if re.match(r"[a-z]+:", target):
                continue
            file, _, anchor = target.partition("#")
            resolved = (path.parent / file).resolve() if file else path
            where = f"{path.relative_to(ROOT)}:{number}: {target}"
            assert resolved.exists(), where
            if anchor:
                assert resolved.suffix == ".md", where
                assert anchor in anchors(resolved), where


def cited(claims):
    for part in claims["parts"]:
        yield part["doc"], part["doc_lines"]
    for claim in claims["claims"]:
        yield claim["doc"], claim["doc_lines"]
        for source in claim["sources"]:
            yield source["path"], source["lines"]
    for relation in claims["relations"]:
        for source in relation["provider_sources"] + relation["user_sources"]:
            yield source["path"], source["lines"]
    for contract in claims["contracts"]:
        yield contract["doc"], contract["doc_lines"]


def test_claims_cite_existing_paths_and_lines():
    claims = json.loads((ROOT / "docs/claims.json").read_text(encoding="utf-8"))
    assert claims["version"] == 1
    lengths = {}
    for path, (start, end) in cited(claims):
        file = ROOT / path
        assert file.is_file(), path
        if path not in lengths:
            lengths[path] = len(file.read_text(encoding="utf-8").splitlines())
        assert 1 <= start <= end <= lengths[path], f"{path}: {start}-{end}"


def test_claim_ids_are_unique_per_kind():
    claims = json.loads((ROOT / "docs/claims.json").read_text(encoding="utf-8"))
    for kind, key in (
        ("parts", "name"),
        ("claims", "id"),
        ("relations", "id"),
        ("contracts", "name"),
    ):
        names = [item[key] for item in claims[kind]]
        assert len(names) == len(set(names)), kind
