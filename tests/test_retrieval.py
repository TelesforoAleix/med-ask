import hashlib
import json
import os
from dataclasses import asdict
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pymupdf
import pytest
from openai import OpenAI

from med_ask.database import Database
from med_ask.embedding import Embedded, Endpoint, PurposeEmbedding, embed_query
from med_ask.extract import Passage
from med_ask.manifest import Book, load_manifest
from med_ask.retrieval import (
    NoIndex,
    evidence_from_node,
    ingest_book,
    ingest_status,
    model_table,
    neighbouring_passages,
    passage_id,
    passage_nodes,
    search,
)


class FakeEndpoint:
    purpose = "embed"

    def __init__(self, dimensions=3, model="synthetic-v1"):
        self.dimensions = dimensions
        self.model = model
        self.calls = []

    def request(self, texts):
        self.calls.extend(texts)
        return Embedded(self.model, [[1.0] * self.dimensions for _ in texts], 0.001)


def make_book(tmp_path, identity="synthetic"):
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        for n in range(3):
            page = pdf.new_page()
            page.insert_text(
                (72, 130), f"Synthetic paragraph {n}. A test of source evidence."
            )
            page.insert_text((290, 810), str(n + 1))
        pdf.set_toc([[1, "Test section", 1]])
        pdf.save(path)
    return Book(identity, path.name, "Synthetic book", "English", path)


def make_passage(order, section=("one",), text=None):
    return Passage(
        "synthetic",
        "English",
        (1, 2),
        ("7", "8"),
        None,
        section,
        order,
        text or f"Synthetic test paragraph {order}.",
        inherited_ocr=True,
    )


def test_evidence_and_plain_embedding_text():
    passage = make_passage(0)
    node = passage_nodes([passage], "Invented title")[0]
    evidence = evidence_from_node(node, 0.73)
    assert asdict(evidence) == dict(
        id=node.node_id,
        text=passage.text,
        book_id="synthetic",
        title="Invented title",
        language="English",
        pdf_pages=(1, 2),
        printed_pages=("7", "8"),
        printed_page_reason=None,
        inherited_ocr=True,
        section_path=("one",),
        order=0,
        label="synthetic: pdf pages 1–2 <print pages: 7–8>",
        score=0.73,
        neighbours=[],
    )
    from llama_index.core.schema import MetadataMode

    assert node.get_content(metadata_mode=MetadataMode.EMBED) == passage.text
    assert node.metadata["passage_hash"] == node.node_id
    unknown = make_passage(1)
    unknown.printed_pages = None
    unknown.printed_page_reason = "synthetic reason"
    assert (
        "synthetic reason"
        in evidence_from_node(passage_nodes([unknown], "Title")[0]).label
    )


def test_neighbours_cannot_cross_sections_and_are_capped():
    nodes = passage_nodes(
        [
            make_passage(0, text="word " * 150),
            make_passage(1),
            make_passage(2, section=("two",)),
        ],
        "Title",
    )
    assert nodes[1].metadata["before"] == nodes[0].node_id
    assert "after" not in nodes[1].metadata
    store = MagicMock()
    store.get_nodes.return_value = [nodes[0]]
    neighbours = neighbouring_passages(nodes[1], store)
    assert len(neighbours) == 1
    assert len(neighbours[0]["text"].split()) == 120
    assert neighbours[0]["truncated"]
    # Even a corrupt neighbour link cannot introduce another section.
    nodes[1].metadata["after"] = nodes[2].node_id
    store.get_nodes.return_value = [nodes[0], nodes[2]]
    assert len(neighbouring_passages(nodes[1], store)) == 1


def test_passage_nodes_remove_nul_from_text_and_metadata(monkeypatch):
    passage = make_passage(0, section=("one\x00",), text="Source\x00 text")
    passage.printed_pages = ("7\x00", "8\x00")
    passage.printed_page_reason = "Reason\x00"
    # Exercise nested containers beyond the current source metadata schema.
    monkeypatch.setattr(
        "med_ask.retrieval.asdict",
        lambda p: {
            **asdict(p),
            "nested": [{"key\x00": ("value\x00", ["deep\x00", 1, None])}],
        },
    )
    node = passage_nodes([passage], "Title\x00")[0]
    assert node.text == "Source text"
    assert node.metadata["section_path"] == ("one",)
    assert node.metadata["title"] == "Title"
    assert node.metadata["printed_pages"] == ("7", "8")
    assert node.metadata["printed_page_reason"] == "Reason"
    assert node.metadata["label"] == "synthetic: pdf pages 1–2 <print pages: 7–8>"
    assert node.metadata["nested"] == [{"key": ("value", ["deep", 1, None])}]
    assert "\\u0000" not in json.dumps(node.metadata)
    assert node.node_id == passage_id(passage)
    assert passage_nodes([passage], "Title\x00")[0].node_id == node.node_id
    assert passage.text == "Source\x00 text"
    assert passage.section_path == ("one\x00",)


def test_nul_free_passage_keeps_original_id_and_other_controls():
    controls = "\x01\x08\t\n\r\x1f\x7f"
    passage = make_passage(0, section=("one" + controls,), text="Source" + controls)
    passage.printed_page_reason = "Reason" + controls
    expected = hashlib.sha256(
        json.dumps(
            [passage.book_id, passage.pdf_pages, passage.text], ensure_ascii=False
        ).encode()
    ).hexdigest()
    node = passage_nodes([passage], "Title" + controls)[0]
    assert node.node_id == expected == passage_id(passage)
    assert node.text == passage.text
    assert node.metadata["section_path"] == passage.section_path
    assert node.metadata["title"] == "Title" + controls
    assert node.metadata["printed_page_reason"] == passage.printed_page_reason


def test_neighbours_still_link_after_nul_removal():
    nodes = passage_nodes(
        [
            make_passage(0, section=("one\x00",)),
            make_passage(1, section=("one\x00",)),
            make_passage(2, section=("two\x00",)),
        ],
        "Title",
    )
    assert nodes[0].metadata["after"] == nodes[1].node_id
    assert nodes[1].metadata["before"] == nodes[0].node_id
    assert "after" not in nodes[1].metadata
    store = MagicMock()
    store.get_nodes.return_value = [nodes[0]]
    neighbours = neighbouring_passages(nodes[1], store)
    assert len(neighbours) == 1
    assert neighbours[0]["section_path"] == ("one",)


@pytest.mark.parametrize(
    "record",
    [
        None,
        ("synthetic-v1", 3, False),
        ("wrong-model", 3, True),
        ("synthetic-v1", 4, True),
    ],
)
def test_missing_or_mismatched_model_refuses_search(record):
    database = MagicMock()
    database.model.return_value = record
    factory = MagicMock()
    endpoint = FakeEndpoint()
    with pytest.raises(NoIndex, match="No index yet|recorded model differs"):
        search("Synthetic question?", endpoint, database, factory)
    factory.assert_not_called()
    assert endpoint.calls == ["Synthetic question?"]


def test_changed_endpoint_model_refuses_passage():
    endpoint = FakeEndpoint()
    adapter = PurposeEmbedding(endpoint, endpoint.model, endpoint.dimensions)
    endpoint.model = "synthetic-v2"
    with pytest.raises(ValueError, match="model changed"):
        adapter.get_text_embedding("Synthetic passage.")


def test_compatible_client_preserves_reported_identity(monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "http://synthetic.invalid/v1")
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    monkeypatch.setenv("EMBEDDING_PURPOSE", "embed")

    def respond(request):
        assert request.url.path == "/v1/embeddings"
        assert json.loads(request.content) == {
            "input": ["Synthetic question?"],
            "model": "embed",
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "model": "synthetic-reported",
                "data": [
                    {"index": 0, "embedding": [1.0, 2.0, 3.0], "object": "embedding"}
                ],
                "object": "list",
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    endpoint = Endpoint()
    endpoint.client = OpenAI(
        base_url="http://synthetic.invalid/v1",
        api_key="unused",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = embed_query(endpoint, "Synthetic question?")
    assert result.model == "synthetic-reported"
    assert result.vectors == [[1.0, 2.0, 3.0]]


def test_sanitized_table_ids_do_not_collide():
    assert model_table("Synthetic/a") != model_table("Synthetic-a")
    assert len("data_" + model_table("x" * 300)) <= 63


@pytest.mark.parametrize(
    "filename", ["../outside.pdf", "/outside.pdf", "not-a-pdf.txt"]
)
def test_manifest_rejects_path_tricks(tmp_path, filename):
    (tmp_path / "books.toml").write_text(
        f'[[books]]\nid="synthetic"\nfilename="{filename}"\ntitle="Test"\nlanguage="English"'
    )
    with pytest.raises(ValueError):
        load_manifest(tmp_path)


def test_manifest_rejects_symlink_escape(tmp_path):
    (tmp_path / "escape.pdf").symlink_to(tmp_path.parent / "outside.pdf")
    (tmp_path / "books.toml").write_text(
        '[[books]]\nid="synthetic"\nfilename="escape.pdf"\ntitle="Test"\nlanguage="English"'
    )
    with pytest.raises(ValueError):
        load_manifest(tmp_path)


@pytest.fixture
def real_database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Real pgvector integration runs in CI with TEST_DATABASE_URL")
    database = Database(url)
    with database.connect() as db:
        db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    database.ensure()
    return database


def test_real_pgvector_resume_search_status_and_export(tmp_path, real_database):
    # Deliberately exceed the ANN vector limit, without using an ANN index.
    endpoint = FakeEndpoint(dimensions=2000 + 3, model="synthetic-" + uuid4().hex)
    book = make_book(tmp_path)

    def output(*args, **kwargs):
        pass

    first = ingest_book(book, endpoint, real_database, output=output)
    assert first["stored"] == first["extracted"] > 0
    endpoint.calls.clear()
    again = ingest_book(book, endpoint, real_database, output=output)
    assert again["skipped"] == first["stored"]
    assert endpoint.calls == ["index dimension probe"]
    evidence, table, seconds = search("Synthetic question?", endpoint, real_database)
    assert evidence and evidence[0].book_id == book.id
    assert evidence[0].title == book.title
    assert seconds == 0.001
    rows = ingest_status([book], real_database)
    assert next(r for r in rows if r["table"] == table)["stored"] == first["stored"]
    with real_database.connect() as db:
        indexes = db.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename=%s", ("data_" + table,)
        ).fetchall()
        assert not any(
            "hnsw" in r[0].lower() or "ivfflat" in r[0].lower() for r in indexes
        )
    identity = real_database.begin_question("Synthetic question?", "tailnet")
    real_database.finish_question(
        identity, table, [dict(id=e.id, score=e.score, label=e.label) for e in evidence]
    )
    assert not real_database.feedback(
        identity, "different asker", "down", "Synthetic comment"
    )
    assert real_database.feedback(identity, "tailnet", "down", "Synthetic comment")
    assert not real_database.feedback(str(uuid4()), "tailnet", "up", None)
    exported = real_database.export(tmp_path)
    row = next(
        json.loads(line)
        for line in exported.read_text().splitlines()
        if json.loads(line)["id"] == identity
    )
    assert row["asker"] == "tailnet" and row["thumbs"] == "down"
    assert (
        row["feedback_at"] and row["evidence"] and row["comment"] == "Synthetic comment"
    )


def test_ingest_resume_after_interruption(tmp_path, real_database):
    book = make_book(tmp_path)
    endpoint = FakeEndpoint(model="synthetic-" + uuid4().hex)
    original_request = endpoint.request

    def interrupted(texts):
        if len(endpoint.calls) >= 2:
            raise RuntimeError("Synthetic interruption")
        return original_request(texts)

    endpoint.request = interrupted
    with pytest.raises(RuntimeError, match="interruption"):
        ingest_book(book, endpoint, real_database, output=lambda *a, **k: None)
    endpoint.request = original_request
    result = ingest_book(book, endpoint, real_database, output=lambda *a, **k: None)
    assert result["skipped"] == 1
    assert result["stored"] == result["extracted"]


def test_synthetic_ingest_rerun_skips_stored_passages(tmp_path, monkeypatch):
    book = make_book(tmp_path)
    endpoint = FakeEndpoint()
    database = MagicMock()
    database.url = "postgresql://unused"
    stored = {}
    store = MagicMock()

    async def close():
        pass

    store.close = close
    store.get_nodes.side_effect = lambda node_ids: [
        stored[n] for n in node_ids if n in stored
    ]
    index = MagicMock()
    index.insert_nodes.side_effect = lambda nodes: stored.update(
        {n.node_id: n for n in nodes}
    )
    monkeypatch.setattr(
        "med_ask.retrieval.VectorStoreIndex.from_vector_store", lambda *a, **k: index
    )
    first = ingest_book(
        book, endpoint, database, lambda *a, **k: store, output=lambda *a, **k: None
    )
    again = ingest_book(
        book, endpoint, database, lambda *a, **k: store, output=lambda *a, **k: None
    )
    assert first["stored"] == first["extracted"] > 0
    assert again["skipped"] == first["stored"]
    assert index.insert_nodes.call_count == first["stored"]
