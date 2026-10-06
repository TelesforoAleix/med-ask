import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_no_data import (
    DATA_EXTENSIONS,
    JSON_CONFIG_PATHS,
    MAX_BYTES,
    SIGNATURES,
    check_paths,
)


@pytest.mark.parametrize("extension", sorted(DATA_EXTENSIONS))
def test_rejects_every_data_extension(tmp_path, extension):
    name = f"fixture{extension}"
    (tmp_path / name).write_bytes(b"synthetic content")
    assert name in check_paths(tmp_path, [name])[0]


@pytest.mark.parametrize("signature", SIGNATURES)
def test_rejects_renamed_pdf_and_images(tmp_path, signature):
    (tmp_path / "disguised.txt").write_bytes(signature + b"synthetic")
    assert "signature" in check_paths(tmp_path, ["disguised.txt"])[0]


@pytest.mark.parametrize("name", ["evaluation.json", "frontend/evaluation.json"])
def test_rejects_unlisted_json(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert name in check_paths(tmp_path, [name])[0]


def test_rejects_large_file(tmp_path):
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_BYTES + 1))
    assert "larger than 1 MB" in check_paths(tmp_path, ["large.txt"])[0]


def test_accepts_clean_tree_and_exact_size_limit(tmp_path):
    names = ["app.py", "README.md", "exact.txt", *sorted(JSON_CONFIG_PATHS)]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if name.endswith(".json") else "code")
    (tmp_path / "exact.txt").write_bytes(b"x" * MAX_BYTES)
    assert check_paths(tmp_path, names) == []


def test_rejects_uppercase_data_extension(tmp_path):
    (tmp_path / "BOOK.PDF").write_text("synthetic")
    assert check_paths(tmp_path, ["BOOK.PDF"])


def test_checks_tracked_files_and_returns_failure(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("# code")
    (tmp_path / "untracked.pdf").write_text("synthetic")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    script = Path(__file__).parents[1] / "scripts/check_no_data.py"
    command = [sys.executable, str(script), "--root", str(tmp_path)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    (tmp_path / "bad.jsonl").write_text("{}")
    subprocess.run(["git", "add", "bad.jsonl"], cwd=tmp_path, check=True)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "bad.jsonl" in result.stdout
