"""Question-export checks use only synthetic rows created at runtime."""

from unittest.mock import MagicMock

import pytest

from med_ask.database import Database


def exporter(rows):
    database = Database("unused")
    database.connect = MagicMock()
    cursor = database.connect.return_value.__enter__.return_value.cursor.return_value
    cursor.__enter__.return_value.__iter__.side_effect = lambda: iter(rows)
    return database


@pytest.mark.parametrize("count", [0, 2])
def test_first_export(tmp_path, count):
    database = exporter([({"synthetic": i},) for i in range(count)])
    path = database.export(tmp_path)
    assert path == tmp_path / "questions.jsonl"
    assert len(path.read_bytes().splitlines()) == count
    assert list(tmp_path.iterdir()) == [path]
    cursor = database.connect.return_value.__enter__.return_value.cursor
    cursor.assert_called_once_with(name="question_export")
    cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
        "SELECT row_to_json(q) FROM medask_questions q ORDER BY asked_at"
    )


@pytest.mark.parametrize("count", [2, 3])
def test_equal_or_larger_export_replaces(tmp_path, count):
    path = exporter([({"synthetic": "old"},)] * 2).export(tmp_path)
    old_inode = path.stat().st_ino
    before = path.read_bytes()
    assert exporter([({"synthetic": "new"},)] * count).export(tmp_path) == path
    assert path.stat().st_ino != old_inode
    assert path.read_bytes() != before
    assert len(path.read_bytes().splitlines()) == count
    assert list(tmp_path.iterdir()) == [path]


def test_smaller_export_refused(tmp_path):
    path = exporter([({"synthetic": "old"},)] * 2).export(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="1 rows is fewer than the existing 2"):
        exporter([({"synthetic": "new"},)]).export(tmp_path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_interrupted_export_preserves_previous(tmp_path):
    path = exporter([({"synthetic": "old"},)]).export(tmp_path)
    before = path.read_bytes()

    def interrupted():
        yield ({"synthetic": "new", "passage": "x" * 100_000},)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        exporter(interrupted()).export(tmp_path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_cli_refusal_is_nonzero(monkeypatch, capsys):
    from med_ask import ingest

    monkeypatch.setenv("DATABASE_URL", "unused")
    monkeypatch.setattr("sys.argv", ["ingest", "export-questions"])
    database = MagicMock()
    database.export.side_effect = ValueError("Refusing question export: synthetic")
    monkeypatch.setattr(ingest, "Database", lambda url: database)
    with pytest.raises(SystemExit) as error:
        ingest.main()
    assert error.value.code == 1
    assert "Refusing question export" in capsys.readouterr().err
