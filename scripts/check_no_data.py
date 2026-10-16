"""Reject tracked content that does not belong in a code-only repository."""

import argparse
import re
import subprocess
from pathlib import Path

MAX_BYTES = 1_000_000
DATA_EXTENSIONS = frozenset(
    """.pdf .png .jpg .jpeg .gif .webp .tif .tiff .bmp .svg .ico .heic
    .csv .tsv .jsonl .ndjson .parquet .xlsx .xls .pkl .pickle .npy .npz
    .h5 .db .sqlite .sqlite3 .dump .faiss .index .bin .pt .safetensors
    .onnx .gguf .arrow .feather .hdf5 .hdf .sqlite-journal .wal
    .pem .key .p12 .pfx .jks .crt .cer .der""".split()
)
FORBIDDEN_DIRECTORIES = frozenset(
    """sources originals data indexes embeddings navigation eval evals feedback logs
    node_modules dist build .venv __pycache__ .pytest_cache .ruff_cache""".split()
)
PRIVATE_PATTERNS = (
    (re.compile(rb"(?i)\b[a-z0-9][a-z0-9-]{0,62}\.ts\.net\b"), "tailnet hostname"),
    (re.compile(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/"), "home-directory path"),
)
EMAIL_PATTERN = re.compile(
    rb"(?<![A-Za-z0-9_.+-])[A-Za-z0-9_.+-]{1,64}@"
    rb"([A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63})"
)
EXAMPLE_DOMAINS = frozenset({b"example.com", b"example.net", b"example.org"})
KEY_NAMES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
# Exact configuration paths only; never allow entire directories or glob patterns.
JSON_CONFIG_PATHS = frozenset(
    {
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/tsconfig.json",
        "frontend/tsconfig.app.json",
        "frontend/tsconfig.node.json",
    }
)
# Exact diagram paths only; never allow entire directories or glob patterns.
DIAGRAM_PATHS = frozenset(
    {
        "docs/diagrams/architecture-light.svg",
        "docs/diagrams/architecture-dark.svg",
    }
)
SIGNATURES = (b"%PDF", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def check_paths(root: Path, paths: list[str]) -> list[str]:
    violations = []
    for name in paths:
        path = root / name
        reasons = []
        suffix = path.suffix.lower()
        if path.name.lower() in KEY_NAMES:
            reasons.append("SSH key filename")
        if any(part.lower() in FORBIDDEN_DIRECTORIES for part in Path(name).parts[:-1]):
            reasons.append("data or build directory")
        if path.name.lower() == ".env" or (
            path.name.lower().startswith(".env.") and name != ".env.example"
        ):
            reasons.append("environment file is not the root example")
        if suffix in DATA_EXTENSIONS and name not in DIAGRAM_PATHS:
            reasons.append("data extension")
        if suffix == ".json" and name not in JSON_CONFIG_PATHS:
            reasons.append("JSON path is not an allowed configuration")
        if path.is_symlink():
            reasons.append("symlinks cannot be checked as code-only files")
        elif not path.is_file():
            reasons.append("tracked path is not a regular file")
        else:
            if path.stat().st_size > MAX_BYTES:
                reasons.append("larger than 1 MB")
            with path.open("rb") as file:
                content = file.read(MAX_BYTES + 1)
                if content.startswith(SIGNATURES):
                    reasons.append("PDF or image signature")
                for pattern, reason in PRIVATE_PATTERNS:
                    if pattern.search(content):
                        reasons.append(reason)
                if any(
                    match[1].lower() not in EXAMPLE_DOMAINS
                    and not match[1].lower().endswith(b".invalid")
                    for match in EMAIL_PATTERN.finditer(content)
                ):
                    reasons.append("email address outside example domains")
        if reasons:
            violations.append(f"{name}: {', '.join(reasons)}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=args.root)
    paths = [name.decode() for name in tracked.split(b"\0") if name]
    violations = check_paths(args.root, paths)
    for violation in violations:
        print(violation)
    if not violations:
        print(f"Code-only guard passed: {len(paths)} tracked files")
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
