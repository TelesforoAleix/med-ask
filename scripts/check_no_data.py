"""Reject tracked content that does not belong in a code-only repository."""

import argparse
import subprocess
from pathlib import Path

MAX_BYTES = 1_000_000
DATA_EXTENSIONS = frozenset(
    """.pdf .png .jpg .jpeg .gif .webp .tif .tiff .bmp .svg .ico .heic
    .csv .tsv .jsonl .ndjson .parquet .xlsx .xls .pkl .pickle .npy .npz
    .h5 .db .sqlite .sqlite3 .dump .faiss .index .bin .pt .safetensors
    .onnx .gguf""".split()
)
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
SIGNATURES = (b"%PDF", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def check_paths(root: Path, paths: list[str]) -> list[str]:
    violations = []
    for name in paths:
        path = root / name
        reasons = []
        suffix = path.suffix.lower()
        if suffix in DATA_EXTENSIONS:
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
                if file.read(8).startswith(SIGNATURES):
                    reasons.append("PDF or image signature")
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
