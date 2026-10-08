"""Synthetic purpose endpoint and response flow checks; no book fixtures."""

import json
import threading
import time
from dataclasses import replace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import OpenAI

from med_ask.generation import (
    GenerationEndpoint,
    build_answer_prompt,
    detect_language,
    generate_answer,
    grade_candidates,
    group_evidence,
    parse_grade,
    translate_passage,
)
from med_ask.retrieval import Evidence


def candidate(identity="one", book="a", score=0.9, language="English"):
    return Evidence(
        identity,
        f"Invented source passage {identity}.",
        book,
        f"Invented book {book}",
        language,
        (1, 1),
        None,
        "unknown",
        False,
        (),
        0,
        "PDF page 1",
        score,
    )


def fake_endpoint(monkeypatch, respond):
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    endpoint = GenerationEndpoint()
    endpoint.client.close()
    endpoint.client = OpenAI(
        base_url="http://synthetic.invalid/v1",
        api_key="unused",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    return endpoint


def reply(text, finish="stop"):
    return httpx.Response(
        200,
        json={
            "id": "synthetic",
            "object": "chat.completion",
            "created": 0,
            "model": "synthetic-reported",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": text},
                }
            ],
        },
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", True),
        (" YES\n", True),
        ("No", False),
        ("no.", None),
        ("yes because", None),
        ('{"yes":true}', None),
        ("", None),
        (None, None),
    ],
)
def test_strict_grade(text, expected):
    assert parse_grade(text) is expected


def test_parallel_grading_cap_errors_and_one_candidate_per_call(monkeypatch):
    active = peak = calls = 0
    lock = threading.Lock()
    cap = 3
    monkeypatch.setenv("GRADE_CONCURRENCY", str(cap))

    def respond(request):
        nonlocal active, peak, calls
        data = json.loads(request.content)
        assert data["model"] == "grade"
        content = json.loads(data["messages"][1]["content"])
        assert set(content) == {"question", "passage"}
        assert content["question"] == "Invented question?"
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
        time.sleep(0.015)
        with lock:
            active -= 1
        passage = content["passage"]
        if "error" in passage:
            return httpx.Response(400, json={"error": {"message": "synthetic"}})
        if "timeout" in passage:
            raise httpx.ReadTimeout("synthetic")
        if "invalid" in passage:
            return reply("yes, it is relevant")
        if "truncated" in passage:
            return reply("yes", "length")
        return reply("no" if "negative" in passage else "yes")

    items = [candidate(str(n)) for n in range(25)] + [
        candidate(name)
        for name in ("error", "timeout", "invalid", "truncated", "negative")
    ]
    endpoint = fake_endpoint(monkeypatch, respond)
    flags, seconds = grade_candidates("Invented question?", items, endpoint)
    assert flags == [True] * 25 + [None] * 4 + [False]
    assert calls == 30 and peak == cap and seconds > 0
    groups = group_evidence(items, flags, "en")
    assert len(groups[0]["evidence"]) == 25
    with pytest.raises(ValueError):
        grade_candidates("question", items, endpoint, concurrency=0)


def test_grouping_numbering_and_translation_visibility():
    items = [
        candidate("a1", "a", 0.6),
        candidate("b1", "b", 0.9, "Spanish"),
        candidate("a2", "a", 0.8),
        candidate("bad", "c", 1.0),
    ]
    groups = group_evidence(items, [True, True, True, None], "en")
    assert [g["book_id"] for g in groups] == ["b", "a"]
    evidence = [e for g in groups for e in g["evidence"]]
    assert [e["id"] for e in evidence] == ["b1", "a2", "a1"]
    assert [e["number"] for e in evidence] == [1, 2, 3]
    assert [e["translation_available"] for e in evidence] == [True, False, False]
    assert group_evidence(items, [False] * 4, "en") == []
    assert all(
        e["translation_available"]
        for g in group_evidence(items, [True] * 4, "ca")
        for e in g["evidence"]
    )


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How do cells repair damage to their DNA?", "en"),
        ("¿Cómo se regula la expresión de los genes en las células?", "es"),
        ("Com es regula l'expressió dels gens a les cèl·lules?", "ca"),
        ("Quina és la funció de les proteïnes de la membrana?", "ca"),
        ("What is the function of membrane proteins?", "en"),
        ("¿Cuál es la función de las proteínas de la membrana?", "es"),
        ("123", "en"),
    ],
)
def test_local_languages(question, expected):
    assert detect_language(question) == expected


def passing():
    original = replace(candidate(), neighbours=[{"text": "Excluded neighbour"}])
    return group_evidence([original, candidate("rejected")], [True, False], "en")[0][
        "evidence"
    ]


def test_answer_endpoint_has_only_passing_numbered_originals(monkeypatch):
    evidence = passing()
    calls = []

    def respond(request):
        data = json.loads(request.content)
        calls.append(data)
        assert data["model"] == "chat"
        content = json.loads(data["messages"][1]["content"])
        assert content == {
            "question": "Invented question?",
            "passages": [{"number": 1, "text": evidence[0]["text"]}],
        }
        assert "Excluded neighbour" not in request.content.decode()
        assert "rejected" not in request.content.decode()
        return reply(
            json.dumps(
                {
                    "sentences": [
                        {"text": "Invented supported statement.", "citations": [1]},
                        {"text": "The evidence is limited.", "citations": [1]},
                    ]
                }
            )
        )

    endpoint = fake_endpoint(monkeypatch, respond)
    assert generate_answer("Invented question?", "en", evidence, endpoint) == (
        "Invented supported statement. [1]\nThe evidence is limited. [1]"
    )
    assert len(calls) == 1
    with pytest.raises(ValueError, match="No passing"):
        build_answer_prompt("question", "en", [])
    assert len(calls) == 1


@pytest.mark.parametrize(
    "sentence",
    [
        {"text": "Invented.", "citations": []},
        {"text": "Invented.", "citations": [2]},
        {"text": "Invented.", "citations": [True]},
        {"text": "First. Second.", "citations": [1]},
        {"text": "Invented [999].", "citations": [1]},
        {"text": "word " * 151, "citations": [1]},
    ],
)
def test_answer_rejects_invalid_output(sentence):
    endpoint = MagicMock()
    endpoint.complete.return_value = json.dumps({"sentences": [sentence]})
    with pytest.raises(ValueError):
        generate_answer("question", "en", passing(), endpoint)


def test_translation_cache_and_target_language(monkeypatch):
    cache = {}
    db = MagicMock()
    db.translation.side_effect = lambda passage, language: cache.get(
        (passage, language)
    )
    db.save_translation.side_effect = lambda passage, language, text: cache.update(
        {(passage, language): text}
    )
    passage = group_evidence([candidate(language="Spanish")], [True], "en")[0][
        "evidence"
    ][0]
    calls = []

    def respond(request):
        data = json.loads(request.content)
        calls.append(data)
        assert data["model"] == "translate"
        assert "English" in data["messages"][0]["content"]
        assert data["messages"][1]["content"] == passage["text"]
        return reply("Invented translation.")

    endpoint = fake_endpoint(monkeypatch, respond)
    assert translate_passage(passage, "en", db, endpoint) == (
        "Invented translation.",
        False,
    )
    assert translate_passage(passage, "en", db, endpoint) == (
        "Invented translation.",
        True,
    )
    assert len(calls) == 1
    with pytest.raises(ValueError):
        translate_passage(passage, "es", db, endpoint)
