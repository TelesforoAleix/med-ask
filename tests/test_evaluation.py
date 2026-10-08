import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from med_ask import ingest
from med_ask.embedding import Endpoint
from med_ask.evaluation import (
    EvalError,
    compare_runs,
    comparison_lines,
    load_eval,
    load_run,
    parse_record,
    run_eval,
    score_results,
    scoreable_evidence,
)
from med_ask.manifest import Book, load_manifest
from med_ask.retrieval import NoIndex

# Invented eval books; two editions share one work.
BOOKS = {
    "bio": Book("bio", "bio.pdf", "Bio", "English", Path("bio.pdf"), "bio-2-en"),
    "bio-old": Book(
        "bio-old", "old.pdf", "Bio old", "English", Path("old.pdf"), "bio-1-en"
    ),
    "chem": Book("chem", "chem.pdf", "Chem", "Spanish", Path("chem.pdf"), "chem-3-es"),
    "plain": Book("plain", "plain.pdf", "Plain", "English", Path("plain.pdf")),
}
EVAL_BOOKS = {b.id: b.eval_book for b in BOOKS.values() if b.eval_book}
INDEXED = set(EVAL_BOOKS.values())
WORKS = {"bio-2-en": "bio", "bio-1-en": "bio", "chem-3-es": "chem"}


@dataclass
class Hit:
    id: str
    book_id: str
    pdf_pages: tuple[int, int]
    score: float


def page(n, text="ok"):
    return {"pdf": n, "print": str(n), "text": text}


def span(start, end, text="ok"):
    return {"pdf_from": start, "pdf_to": end, "print": "x", "text": text}


def evidence(book, pages, state="confirmed"):
    return {
        "book": book,
        "work": WORKS.get(book, book),
        "state": state,
        "pages": pages,
        "confirmed_by": None,
        "confirmed_on": None,
        "note": "",
    }


def record(identity, *items, status="confirmed", **fields):
    data = {
        "id": identity,
        "schema": 1,
        "set_version": 1,
        "core": True,
        "status": status,
        "origin": "drafted",
        "logged_on": "2026-01-01",
        "question": f"Synthetic secret question {identity}?",
        "language": "en",
        "type": "definition",
        "subject": "genetics",
        "expect_not_found": False,
        "hints": {"book": "bio", "where": "nowhere"},
        "evidence": list(items),
        "notes": "",
    }
    data.update(fields)
    return data


def write_eval(path, records, extra_lines=()):
    lines = [json.dumps(r) for r in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n")
    return path


def fake_retrieve(answers, calls=None):
    """Return fixed passages per question; unknown questions get one weak result."""

    def retrieve(question):
        if calls is not None:
            calls.append(question)
        results = answers.get(question, [Hit("p-none", "plain", (1, 1), 0.1)])
        return results, "e_synthetic_table", 0.01 + 0.001 * len(calls or [])

    return retrieve


def question_of(identity):
    return f"Synthetic secret question {identity}?"


def test_hits_on_single_pages_and_ranges_without_tolerance():
    rec = parse_record(record("q001", evidence("bio-2-en", [page(11), span(20, 22)])))
    scoreable = scoreable_evidence(rec, INDEXED, {"confirmed"})
    assert scoreable == {"bio-2-en": {"work": "bio", "pages": {11, 20, 21, 22}}}
    results = [
        Hit("a", "bio", (12, 13), 0.9),  # next page: no ±1 tolerance
        Hit("b", "bio", (19, 19), 0.8),  # page before the range
        Hit("c", "chem", (11, 11), 0.7),  # right page, wrong book
        Hit("d", "bio", (10, 11), 0.6),  # range overlapping a single page
        Hit("e", "bio", (22, 23), 0.5),  # passage overlapping the range end
    ]
    score = score_results(results, scoreable, EVAL_BOOKS)
    assert score["hits"] == [False, False, False, True, True]
    assert score["hit5"] and score["hit10"] and score["first_hit_rank"] == 4
    late = [Hit(str(i), "bio", (1, 1), 0.5) for i in range(5)]
    late += [Hit("x", "bio", (21, 21), 0.4)]
    score = score_results(late, scoreable, EVAL_BOOKS)
    assert not score["hit5"] and score["hit10"] and score["first_hit_rank"] == 6


def test_only_confirmed_indexed_pages_with_text_are_scoreable():
    rec = parse_record(
        record(
            "q001",
            evidence("bio-2-en", [page(5)], state="open"),
            evidence("bio-2-en", [page(6)], state="ocr_missing"),
            evidence("bio-2-en", [page(7, text="missing"), span(8, 9, "missing")]),
            evidence("unmapped-1-en", [page(10)]),
            evidence("chem-3-es", [page(11)], state="not_indexed"),
            evidence("chem-3-es", [page(12)], state="absent"),
        )
    )
    assert scoreable_evidence(rec, INDEXED, {"confirmed"}) == {}
    draft = parse_record(
        record("q002", evidence("bio-2-en", [page(5)]), status="pages_open")
    )
    assert scoreable_evidence(draft, INDEXED, {"confirmed"}) == {}
    assert scoreable_evidence(draft, INDEXED, {"confirmed", "pages_open"})
    not_found = parse_record(
        record("q003", evidence("bio-2-en", [page(5)]), expect_not_found=True)
    )
    assert scoreable_evidence(not_found, INDEXED, {"confirmed"}) == {}


def test_book_coverage_counts_works_not_editions():
    rec = parse_record(
        record(
            "q001",
            evidence("bio-2-en", [page(5)]),
            evidence("bio-1-en", [page(50)]),
            evidence("chem-3-es", [page(7)]),
        )
    )
    scoreable = scoreable_evidence(rec, INDEXED, {"confirmed"})
    both_editions = [Hit("a", "bio", (5, 5), 0.9), Hit("b", "bio-old", (50, 50), 0.8)]
    score = score_results(both_editions, scoreable, EVAL_BOOKS)
    assert score["works"] == 2 and score["works_covered10"] == 1
    assert score["books_hit5"] == {
        "bio-1-en": True,
        "bio-2-en": True,
        "chem-3-es": False,
    }
    score = score_results(
        both_editions + [Hit("c", "chem", (7, 7), 0.7)], scoreable, EVAL_BOOKS
    )
    assert score["works_covered10"] == 2


def test_malformed_records_are_reported_by_id(tmp_path):
    bad_page = record("q003", evidence("bio-2-en", [{"print": "4", "text": "ok"}]))
    both = record("q004", evidence("bio-2-en", [{**span(1, 2), "pdf": 1}]))
    backwards = record("q005", evidence("bio-2-en", [span(9, 2)]))
    path = write_eval(
        tmp_path / "eval.jsonl",
        [
            record("q001", schema=2),
            record("q002", evidence("bio-2-en", [page(1)], state="guessed")),
            bad_page,
            both,
            backwards,
            record("q006", status="unknown"),
            record("q007", evidence("bio-2-en", [page(1)])),
            record("q007"),
            {"id": "Synthetic secret question with spaces?", "schema": 1},
        ],
        extra_lines=["{not json"],
    )
    records, malformed = load_eval(path)
    assert [r.id for r in records] == ["q007"]
    assert malformed == [
        {"id": "q001", "reason": "wrong schema"},
        {"id": "q002", "reason": "unknown evidence state"},
        {
            "id": "q003",
            "reason": "page item is neither a pdf page nor a pdf_from/pdf_to range",
        },
        {
            "id": "q004",
            "reason": "page item is neither a pdf page nor a pdf_from/pdf_to range",
        },
        {
            "id": "q005",
            "reason": "page item is neither a pdf page nor a pdf_from/pdf_to range",
        },
        {"id": "q006", "reason": "unknown status"},
        {"id": "q007", "reason": "duplicate id"},
        {"line": 9, "reason": "missing or unusable id"},
        {"line": 10, "reason": "not valid JSON"},
    ]


def synthetic_set(tmp_path):
    records = [
        # Scored, hit at rank 1.
        record("q001", evidence("bio-2-en", [page(10)])),
        # Scored, hit only at rank 7; Spanish, another type and subject.
        record(
            "q002",
            evidence("chem-3-es", [span(30, 31)]),
            language="es",
            type="interaction",
            subject="biochemistry",
        ),
        # Scored, two works, one covered.
        record(
            "q003",
            evidence("bio-2-en", [page(40)]),
            evidence("chem-3-es", [page(41)]),
        ),
        # Skipped: status not selected.
        record("q004", evidence("bio-2-en", [page(10)]), status="pages_open"),
        # Skipped: nothing scoreable.
        record("q005", evidence("bio-2-en", [page(10)], state="ocr_missing")),
        # Not found.
        record("q006", expect_not_found=True, checked_books=["bio-2-en"]),
        # Retired: neither retrieved nor counted as skipped.
        record("q007", evidence("bio-2-en", [page(10)]), status="retired"),
    ]
    filler = [Hit(f"f{i}", "plain", (1, 1), 0.5 - i / 100) for i in range(6)]
    answers = {
        question_of("q001"): [Hit("a", "bio", (10, 10), 0.91)] + filler,
        question_of("q002"): filler + [Hit("b", "chem", (31, 32), 0.4)],
        question_of("q003"): [Hit("c", "bio", (40, 40), 0.8)] + filler,
        question_of("q006"): [Hit("d", "plain", (3, 3), 0.33)],
    }
    path = write_eval(tmp_path / "med-ask-eval.v1.jsonl", records)
    return path, answers


@pytest.mark.parametrize(
    "purpose,roles",
    [("embed", False), ("embed", True), ("embed-large", False), ("embed-large", True)],
)
def test_run_scores_skips_and_breaks_down(tmp_path, purpose, roles):
    path, answers = synthetic_set(tmp_path)
    calls, printed = [], []
    result = run_eval(
        path,
        tmp_path / "runs",
        BOOKS,
        fake_retrieve(answers, calls),
        purpose,
        ["confirmed"],
        output=lambda line, **_: printed.append(line),
        roles=roles,
    )
    assert calls[0] == "med-ask eval warm-up" and len(calls) == 7
    run = json.loads(result.read_text())
    assert result.parent == tmp_path / "runs"
    assert run["purpose"] == purpose and run["roles"] is roles
    role_mark = "roles-on" if roles else "roles-off"
    assert f"-{purpose}-{role_mark}-" in result.name
    assert run["set_version"] == 1 and run["table"] == "e_synthetic_table"
    assert run["counts"] == {
        "read": 7,
        "malformed": 0,
        "retired": 1,
        "retrieved": 6,
        "answerable": 5,
        "not_found": 1,
        "scored": 3,
        "skipped": 2,
    }
    assert run["skipped"] == [
        {"id": "q004", "reason": "status not selected"},
        {"id": "q005", "reason": "no scoreable evidence"},
    ]
    s = run["summary"]
    assert (s["scored"], s["hit5"], s["hit10"]) == (3, 2, 3)
    assert s["coverage10"] == {"questions": 1, "mean": 0.5}
    assert s["hit5_by"]["language"] == {
        "en": {"hit5": 2, "n": 2},
        "es": {"hit5": 0, "n": 1},
    }
    assert s["hit5_by"]["type"]["interaction"] == {"hit5": 0, "n": 1}
    assert s["hit5_by"]["subject"]["genetics"] == {"hit5": 2, "n": 2}
    assert s["hit5_by"]["book"] == {
        "bio-2-en": {"hit5": 2, "n": 2},
        "chem-3-es": {"hit5": 0, "n": 2},
    }
    assert s["embedding_seconds"]["sample"] == 6
    assert s["top_score"]["not_found"] == {
        "n": 1,
        "min": 0.33,
        "median": 0.33,
        "max": 0.33,
    }
    assert s["top_score"]["answerable"]["n"] == 5
    assert s["top_score"]["answerable"]["max"] == 0.91
    assert "Hit@5 (gate): 2/3 (67%)" in printed
    assert printed[-1] == f"result: {result}"


def test_run_with_nothing_confirmed_says_so(tmp_path):
    path = write_eval(
        tmp_path / "eval.jsonl",
        [
            record(f"q{n:03}", evidence("bio-2-en", [page(n)]), status="draft")
            for n in (1, 2)
        ]
        + [record("q003", expect_not_found=True)],
    )
    printed = []
    run_eval(
        path,
        tmp_path / "runs",
        BOOKS,
        fake_retrieve({}),
        "embed",
        ["confirmed"],
        output=lambda line, **_: printed.append(line),
    )
    assert "Nothing scored: no answerable record has scoreable evidence." in printed
    assert "skipped (status not selected): 2: q001 q002" in printed
    assert not any(line.startswith("Hit@5") for line in printed)


def test_embedding_timing_uses_twenty_questions(tmp_path):
    path = write_eval(
        tmp_path / "eval.jsonl", [record(f"q{n:03}") for n in range(1, 26)]
    )
    calls = []
    result = run_eval(
        path,
        tmp_path / "runs",
        BOOKS,
        fake_retrieve({}, calls),
        "embed",
        ["confirmed"],
        output=lambda *a, **k: None,
    )
    timing = json.loads(result.read_text())["summary"]["embedding_seconds"]
    # Call 1 is the warm-up; questions 1-20 are calls 2-21.
    assert timing == {"sample": 20, "median": 0.0215, "slowest": 0.031}


def run_file(tmp_path, name, hits, scored=None, set_version=1):
    scored = hits if scored is None else scored
    questions = [
        {"id": q, "scored": True, "hit5": q in hits} for q in sorted(set(scored))
    ]
    data = {
        "kind": "med-ask-eval-run",
        "format": 1,
        "set_version": set_version,
        "purpose": name,
        "table": f"e_{name}",
        "questions": questions,
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data))
    return load_run(path)


@pytest.mark.parametrize(
    ("before", "after", "passed", "lost", "gained"),
    [
        (["q1", "q2"], ["q1", "q2"], True, [], []),
        (["q1"], ["q1", "q2"], True, [], ["q2"]),
        (["q1", "q2"], ["q1"], False, ["q2"], []),
        # A lost hit balanced by a gain keeps the total and passes.
        (["q1", "q2"], ["q1", "q3"], True, ["q2"], ["q3"]),
        # A gain that does not balance two losses fails on the total.
        (["q1", "q2", "q3"], ["q1", "q4"], False, ["q2", "q3"], ["q4"]),
    ],
)
def test_gate(tmp_path, before, after, passed, lost, gained):
    everything = ["q1", "q2", "q3", "q4"]
    base = run_file(tmp_path, "base", before, everything)
    cand = run_file(tmp_path, "cand", after, everything)
    result = compare_runs(base, cand)
    assert (result["passed"], result["lost"], result["gained"]) == (
        passed,
        lost,
        gained,
    )


def test_compare_refuses_different_set_versions(tmp_path):
    base = run_file(tmp_path, "base", ["q1"])
    cand = run_file(tmp_path, "cand", ["q1"], set_version=2)
    with pytest.raises(EvalError, match="different set versions"):
        compare_runs(base, cand)


def test_compare_refuses_non_run_files(tmp_path):
    (tmp_path / "other.json").write_text(json.dumps({"kind": "other"}))
    with pytest.raises(EvalError, match="not an eval result"):
        load_run(tmp_path / "other.json")


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["med_ask.ingest", *argv])
    with pytest.raises(SystemExit) as stopped:
        ingest.main()
    out, err = capsys.readouterr()
    return stopped.value.code, out, err


@pytest.fixture
def cli(monkeypatch, tmp_path):
    path, answers = synthetic_set(tmp_path)
    monkeypatch.setenv("EVAL_FILE", str(path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    monkeypatch.setattr(ingest.Database, "ensure", lambda self: None)
    monkeypatch.setattr(ingest, "load_manifest", lambda sources: BOOKS)
    purposes = []

    def search(question, endpoint, database):
        purposes.append(endpoint.purpose)
        return fake_retrieve(answers)(question)

    monkeypatch.setattr(ingest, "search", search)
    return path, purposes


def test_no_question_text_in_any_output(cli, monkeypatch, capsys, tmp_path):
    path, purposes = cli
    code, out, err = run_cli(monkeypatch, capsys, "eval", "--purpose", "candidate")
    assert code == 0 and set(purposes) == {"candidate"}
    code, out2, err2 = run_cli(monkeypatch, capsys, "eval")
    assert code == 0 and purposes[-1] == "embed"
    files = sorted((tmp_path / "runs").iterdir())
    assert len(files) == 2
    code, out3, err3 = run_cli(
        monkeypatch, capsys, "eval", "--compare", files[0].name, str(files[1])
    )
    assert code == 0 and "gate: PASS" in out3
    outputs = [out, err, out2, err2, out3, err3] + [f.read_text() for f in files]
    questions = [json.loads(line)["question"] for line in path.read_text().splitlines()]
    for text in outputs:
        assert "Synthetic secret" not in text
        assert not any(q in text for q in questions)


def test_retrieval_failure_hides_the_question(cli, monkeypatch, capsys, tmp_path):
    def echoing(question, endpoint, database):
        if question.startswith("Synthetic"):
            raise RuntimeError(f"endpoint rejected input: {question}")
        return [], "e_synthetic_table", 0.01

    monkeypatch.setattr(ingest, "search", echoing)
    code, out, err = run_cli(monkeypatch, capsys, "eval")
    assert code == 2
    assert "Retrieval failed for q001 (RuntimeError)" in err
    assert "Synthetic secret" not in out + err
    assert not (tmp_path / "runs").exists()


def test_missing_index_is_refused(cli, monkeypatch, capsys):
    def missing(question, endpoint, database):
        raise NoIndex("No index yet for the current search model.", "e_table")

    monkeypatch.setattr(ingest, "search", missing)
    code, out, err = run_cli(monkeypatch, capsys, "eval")
    assert code == 2 and "No index yet" in err


def test_failed_gate_exits_non_zero(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("EVAL_FILE", str(tmp_path / "eval.jsonl"))
    run_file(tmp_path, "base", ["q1"])
    run_file(tmp_path, "cand", [], ["q1"])
    code, out, err = run_cli(
        monkeypatch,
        capsys,
        "eval",
        "--compare",
        str(tmp_path / "base.json"),
        str(tmp_path / "cand.json"),
    )
    assert code == 1 and "gate: FAIL" in out and "lost: q1" in out
    run_file(tmp_path, "other", ["q1"], set_version=2)
    code, out, err = run_cli(
        monkeypatch,
        capsys,
        "eval",
        "--compare",
        str(tmp_path / "base.json"),
        str(tmp_path / "other.json"),
    )
    assert code == 2 and "different set versions" in err


def test_manifest_maps_optional_eval_books(tmp_path):
    entry = '[[books]]\nid="{}"\nfilename="{}.pdf"\ntitle="T"\nlanguage="English"\n'
    (tmp_path / "books.toml").write_text(
        entry.format("one", "one")
        + 'eval_book="one-1-en"\n'
        + entry.format("two", "two")
    )
    books = load_manifest(tmp_path)
    assert books["one"].eval_book == "one-1-en"
    assert books["two"].eval_book is None
    (tmp_path / "books.toml").write_text(
        entry.format("one", "one")
        + 'eval_book="same"\n'
        + entry.format("two", "two")
        + 'eval_book="same"\n'
    )
    with pytest.raises(ValueError, match="Duplicate eval book"):
        load_manifest(tmp_path)


def test_endpoint_purpose_can_be_named(monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    monkeypatch.setenv("EMBEDDING_PURPOSE", "embed")
    assert Endpoint().purpose == "embed"
    assert Endpoint("candidate").purpose == "candidate"


@pytest.mark.parametrize(
    "setting,flag,expected",
    [
        ("true", [], True),
        ("false", [], False),
        ("true", ["--no-roles"], False),
        ("false", ["--roles"], True),
    ],
)
def test_cli_roles_recorded(
    cli, monkeypatch, capsys, tmp_path, setting, flag, expected
):
    monkeypatch.setenv("EMBEDDING_ROLES", setting)
    code, out, err = run_cli(monkeypatch, capsys, "eval", *flag)
    assert code == 0
    run = load_run(next((tmp_path / "runs").iterdir()))
    assert run["roles"] is expected
    assert f"roles={'on' if expected else 'off'}" in out


def test_comparison_names_roles_and_reads_older_plain_runs(tmp_path):
    baseline = run_file(tmp_path, "embed", ["q1"])
    candidate = {**baseline, "roles": True}
    lines = comparison_lines(compare_runs(baseline, candidate), baseline, candidate)
    assert "purpose=embed" in lines[1] and "roles=off" in lines[1]
    assert "purpose=embed" in lines[2] and "roles=on" in lines[2]


@pytest.mark.parametrize(
    "setting,expected",
    [(None, False), ("false", False), ("true", True), ("TRUE", False), ("1", False)],
)
def test_endpoint_roles_environment(monkeypatch, setting, expected):
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    monkeypatch.delenv("EMBEDDING_ROLES", raising=False)
    if setting is not None:
        monkeypatch.setenv("EMBEDDING_ROLES", setting)
    assert Endpoint().roles is expected
    assert Endpoint(roles=not expected).roles is not expected
