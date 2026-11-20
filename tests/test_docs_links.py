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
# Inline links and images, with an optional title: [text](target "t"), ![alt](target).
INLINE = re.compile(
    r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
# Reference-style: a definition "[label]: target" and a use "[text][label]".
DEFINITION = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?", re.MULTILINE)
REFERENCE = re.compile(r"!?\[[^\]]+\]\[([^\]]+)\]")
# HTML attributes that name a file; srcset holds comma-separated candidates.
ATTRIBUTE = re.compile(r"""\b(src|srcset|href)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
EXTERNAL = re.compile(r"(?:[a-z][a-z0-9+.-]*:|//)", re.I)


def blank_code(text):
    """Blank fenced blocks and inline code with spaces, keeping line numbers."""
    fenced, lines = False, []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            lines.append("")
        elif fenced:
            lines.append("")
        else:
            lines.append(re.sub(r"`[^`\n]*`", lambda m: " " * len(m[0]), line))
    return "\n".join(lines)


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


def link_targets(text):
    """Yield (line, target) for every link form: inline, image, reference, HTML."""
    body = blank_code(text)

    def line(position):
        return body.count("\n", 0, position) + 1

    definitions = {}
    for match in DEFINITION.finditer(body):
        definitions[match[1].strip().lower()] = match[2]
        yield line(match.start()), match[2]
    for match in INLINE.finditer(body):
        yield line(match.start()), match[1]
    for match in REFERENCE.finditer(body):
        if match[1].strip().lower() not in definitions:
            yield line(match.start()), f"undefined-reference:{match[1]}"
    for match in ATTRIBUTE.finditer(body):
        value = match[2] if match[2] is not None else match[3]
        if match[1].lower() == "srcset":
            for candidate in value.split(","):
                if candidate.strip():
                    yield line(match.start()), candidate.split()[0]
        elif value.strip():
            yield line(match.start()), value.strip()


def broken_links(path, root=ROOT):
    """Return a message for each relative link in a document that does not resolve."""
    problems = []
    for number, target in link_targets(path.read_text(encoding="utf-8")):
        where = f"{path.name}:{number}: {target}"
        if target.startswith("undefined-reference:"):
            problems.append(where)
            continue
        if EXTERNAL.match(target):
            continue
        file, _, anchor = target.partition("#")
        resolved = (path.parent / file.split("?")[0]).resolve() if file else path
        if not resolved.exists():
            problems.append(where)
        elif anchor and (resolved.suffix != ".md" or anchor not in anchors(resolved)):
            problems.append(where)
    return problems


def map_targets(text=None):
    text = text or (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## Document map", 1)[1].split("\n## ", 1)[0]
    return [
        target
        for line in section.splitlines()
        if line.startswith("- ")
        for _, target in link_targets(line)
    ]


def test_document_map_names_existing_files():
    targets = map_targets()
    assert len(targets) >= 10
    for target in targets:
        file = target.split("#")[0]
        assert not EXTERNAL.match(target) and (ROOT / file).is_file(), target


@pytest.mark.parametrize("path", CORE, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(path):
    assert broken_links(path) == []


def test_readme_diagram_references_are_checked():
    targets = [t for _, t in link_targets((ROOT / "README.md").read_text("utf-8"))]
    assert any(t.endswith(".svg") for t in targets)


@pytest.mark.parametrize(
    "source",
    [
        "[inline](missing.md)",
        "![image](missing.png)",
        '[titled](missing.md "title")',
        "[text][ref]\n\n[ref]: missing.md",
        "[undefined][nowhere]",
        '<img alt="x" src="missing.svg">',
        '<source srcset="missing-dark.svg">',
        '<source srcset="present.md 1x, missing-2x.svg 2x">',
        '<a href="missing.md">x</a>',
        "[bad anchor](present.md#nowhere)",
        "[row](present.md) and [second](missing.md)",
    ],
)
def test_missing_targets_are_rejected_in_every_link_form(tmp_path, source):
    (tmp_path / "present.md").write_text("# Present\n", encoding="utf-8")
    document = tmp_path / "doc.md"
    document.write_text(source + "\n", encoding="utf-8")
    assert broken_links(document, tmp_path), source


def test_present_and_external_targets_are_accepted(tmp_path):
    (tmp_path / "present.md").write_text("# Present\n", encoding="utf-8")
    (tmp_path / "present.svg").write_text("<svg/>", encoding="utf-8")
    document = tmp_path / "doc.md"
    document.write_text(
        "[a](present.md#present) ![b](present.svg)\n"
        "[c][ref]\n\n[ref]: present.md\n"
        '<img src="present.svg">\n<source srcset="present.svg">\n'
        "[external](https://example.invalid/missing.md) ![e](http://example.invalid/x.png)\n"
        '<img src="https://example.invalid/missing.svg">\n'
        "[mail](mailto:someone@example.com)\n"
        "```\n[code](missing.md)\n```\n`[span](missing.md)`\n",
        encoding="utf-8",
    )
    assert broken_links(document, tmp_path) == []


def test_every_document_map_link_on_a_row_is_checked():
    text = "## Document map\n\n- Change one → [a](AGENTS.md) and [b](missing.md)\n"
    assert map_targets(text) == ["AGENTS.md", "missing.md"]
    assert not (ROOT / "missing.md").is_file()


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
