"""LlamaIndex ingestion and retrieval, with plain evidence at the boundary."""

import asyncio
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from llama_index.core import QueryBundle, VectorStoreIndex
from llama_index.core.schema import MetadataMode, TextNode
from llama_index.core.vector_stores import (
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)
from llama_index.vector_stores.postgres import PGVectorStore
from sqlalchemy.engine import make_url

from med_ask.embedding import PurposeEmbedding, embed_query
from med_ask.extract import Passage, extract_book, passage_label

NEIGHBOUR_WORDS = 120
INDEX_VERSION = "v2"
SEARCH_KINDS = ("content", "summary", "glossary")


class NoIndex(ValueError):
    def __init__(self, message, table):
        super().__init__(message)
        self.table = table


@dataclass(frozen=True)
class Evidence:
    """1. Carry only original text, source identity, page labels, and similarity.
    2. Attach capped neighbouring source passages without exposing library objects.
    """

    id: str
    text: str
    book_id: str
    title: str
    language: str
    pdf_pages: tuple[int, int]
    printed_pages: tuple[str, str] | None
    printed_page_reason: str | None
    inherited_ocr: bool
    section_path: tuple[str, ...]
    order: int
    label: str
    score: float | None
    neighbours: list[dict] = field(default_factory=list)
    kind: str = "content"
    text_source: str = "born-digital"
    check_page: bool = False
    ocr_reasons: list[str] = field(default_factory=list)


def model_table(model: str, roles=False, purpose="embed") -> str:
    """1. Sanitize the reported model for a short PostgreSQL identifier.
    2. Mark role indexes separately, except embed-large's unchanged passages.
    3. Add the index version and model hash to keep rebuilds in separate tables.
    """
    stem = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:32] or "model"
    digest = hashlib.sha256(model.encode()).hexdigest()[:16]
    version = os.environ.get("INDEX_VERSION", INDEX_VERSION)
    if not re.fullmatch(r"v[0-9]{1,4}", version):
        raise ValueError("INDEX_VERSION must be v followed by one to four digits")
    version += "r" if roles is True and purpose != "embed-large" else ""
    return f"e_{version}_{stem}_{digest}"


def passage_id(passage: Passage) -> str:
    """1. Hash the book id, inclusive PDF range, and unchanged passage text."""
    value = json.dumps(
        [passage.book_id, passage.pdf_pages, passage.text], ensure_ascii=False
    )
    return hashlib.sha256(value.encode()).hexdigest()


def passage_nodes(passages: list[Passage], title: str) -> list[TextNode]:
    """1. Compute stable ids from the unchanged extracted passages.
    2. Link the immediately adjacent passages only when their sections match.
    3. Remove only NUL characters from text and every nested metadata string.
    4. Exclude all metadata from embedding text so only source text is sent.
    """

    def remove_nul(value):
        """1. Remove only NUL from strings, including nested keys and values.
        2. Preserve lists, tuples, dictionaries, and all other values.
        """
        if isinstance(value, str):
            return value.replace("\x00", "")
        if isinstance(value, dict):
            return {remove_nul(k): remove_nul(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remove_nul(v) for v in value]
        if isinstance(value, tuple):
            return tuple(remove_nul(v) for v in value)
        return value

    nodes = []
    identities = [passage_id(p) for p in passages]
    for position, passage in enumerate(passages):
        metadata = asdict(passage)
        metadata.pop("text")
        metadata.pop("metadata")
        metadata.update(
            title=title, passage_hash=identities[position], label=passage_label(passage)
        )
        for name, offset in (("before", -1), ("after", 1)):
            neighbour = position + offset
            if (
                0 <= neighbour < len(passages)
                and passages[neighbour].book_id == passage.book_id
                and passages[neighbour].section_path == passage.section_path
            ):
                metadata[name] = identities[neighbour]
        metadata = remove_nul(metadata)
        nodes.append(
            TextNode(
                id_=identities[position],
                text=remove_nul(passage.text),
                metadata=metadata,
                excluded_embed_metadata_keys=list(metadata),
            )
        )
    return nodes


def vector_store(url: str, table: str, dimensions: int, setup=False):
    """1. Pass database credentials and probed dimensions to LlamaIndex's store.
    2. Leave approximate indexes and hybrid text search disabled.
    """
    parsed = make_url(url)
    return PGVectorStore.from_params(
        connection_string=parsed.set(drivername="postgresql+psycopg2").render_as_string(
            hide_password=False
        ),
        async_connection_string=parsed.set(
            drivername="postgresql+asyncpg"
        ).render_as_string(hide_password=False),
        table_name=table,
        embed_dim=dimensions,
        perform_setup=setup,
        use_jsonb=True,
        hnsw_kwargs=None,
    )


@contextmanager
def managed_store(factory, *args, **kwargs):
    """1. Open the library store for one operation.
    2. Dispose both database engines even when ingestion or search fails.
    """
    store = factory(*args, **kwargs)
    try:
        yield store
    finally:
        asyncio.run(store.close())


def evidence_from_node(node, score=None, neighbours=None) -> Evidence:
    """1. Read the stored source metadata and unchanged node text.
    2. Convert ranges and sections into ordinary Python values.
    3. Return evidence independent of LlamaIndex's node types.
    """
    m = node.metadata
    return Evidence(
        id=node.node_id,
        text=node.get_content(metadata_mode=MetadataMode.NONE),
        book_id=m["book_id"],
        title=m["title"],
        language=m["language"],
        pdf_pages=tuple(m["pdf_pages"]),
        printed_pages=tuple(m["printed_pages"]) if m["printed_pages"] else None,
        printed_page_reason=m["printed_page_reason"],
        inherited_ocr=m["inherited_ocr"],
        section_path=tuple(m["section_path"]),
        order=m["order"],
        label=m["label"],
        score=score,
        neighbours=neighbours or [],
        kind=m.get("kind", "content"),
        text_source=m.get(
            "text_source", "inherited-ocr" if m["inherited_ocr"] else "born-digital"
        ),
        check_page=m.get("check_page", False),
        ocr_reasons=m.get("ocr_reasons", []),
    )


def neighbouring_passages(node, store) -> list[dict]:
    """1. Load at most the two neighbours by their stored node ids.
    2. Check their book and section against the matched passage.
    3. Cap each neighbour at 120 words and keep it labelled as source context.
    """
    positions = {
        node.metadata[k]: k for k in ("before", "after") if node.metadata.get(k)
    }
    if not positions:
        return []
    neighbours = []
    for other in store.get_nodes(node_ids=list(positions)):
        if (
            other.metadata["book_id"] != node.metadata["book_id"]
            or other.metadata["section_path"] != node.metadata["section_path"]
        ):
            continue
        evidence = asdict(evidence_from_node(other))
        words = evidence["text"].split()
        evidence["text"] = " ".join(words[:NEIGHBOUR_WORDS])
        evidence["truncated"] = len(words) > NEIGHBOUR_WORDS
        evidence["position"] = positions[other.node_id]
        neighbours.append(evidence)
    return sorted(neighbours, key=lambda p: p["order"])


def ingest_book(book, endpoint, database, store_factory=vector_store, output=print):
    """1. Extract the requested book and probe the endpoint's model and dimensions.
    2. Select its role-aware table and record the extracted count outside git.
    3. Ask LlamaIndex which stable passage ids are already stored.
    4. Insert only missing passages through LlamaIndex, committing each passage.
    5. Print durable progress and the final stored/extracted summary.
    """
    extraction = extract_book(book.path, book.id, book.language)
    probe = embed_query(endpoint, "index dimension probe")
    dimensions = len(probe.vectors[0])
    table = model_table(
        probe.model,
        getattr(endpoint, "roles", False),
        getattr(endpoint, "purpose", "embed"),
    )
    database.register(table, probe.model, dimensions)
    database.book_count(table, book.id, len(extraction.passages))
    with managed_store(
        store_factory, database.url, table, dimensions, setup=True
    ) as store:
        adapter = PurposeEmbedding(endpoint, probe.model, dimensions)
        index = VectorStoreIndex.from_vector_store(store, embed_model=adapter)
        nodes = passage_nodes(extraction.passages, book.title)
        existing = set()
        for start in range(0, len(nodes), 250):
            existing.update(
                n.node_id
                for n in store.get_nodes(
                    node_ids=[n.node_id for n in nodes[start : start + 250]]
                )
            )
        skipped = len(existing)
        output(
            f"{book.id}: model={probe.model} table={table} dimensions={dimensions} "
            f"stored={skipped}/{len(nodes)}",
            flush=True,
        )
        for node in nodes:
            if node.node_id in existing:
                continue
            index.insert_nodes([node])
            existing.add(node.node_id)
            output(f"{book.id}: stored={len(existing)}/{len(nodes)}", flush=True)
        output(
            f"{book.id}: complete stored={len(existing)}/{len(nodes)} "
            f"skipped={skipped}",
            flush=True,
        )
        return {
            "book_id": book.id,
            "model": probe.model,
            "table": table,
            "stored": len(existing),
            "extracted": len(nodes),
            "skipped": skipped,
        }


def search(question, endpoint, database, store_factory=vector_store, k=10):
    """1. Embed the question once and select the model's role-aware vector table.
    2. Refuse absent tables or a mismatched recorded model or dimension.
    3. Filter to content, summary, and glossary inside LlamaIndex's vector search.
    4. Convert passages and same-section neighbours into plain evidence records.
    """
    embedded = embed_query(endpoint, question)
    table = model_table(
        embedded.model,
        getattr(endpoint, "roles", False),
        getattr(endpoint, "purpose", "embed"),
    )
    recorded = database.model(table)
    if not recorded or not recorded[2]:
        raise NoIndex(
            "No index yet for the current search model. Ingest a book first.", table
        )
    dimensions = len(embedded.vectors[0])
    if recorded[:2] != (embedded.model, dimensions):
        raise NoIndex(
            "The index's recorded model differs. A new index is required.", table
        )
    with managed_store(store_factory, database.url, table, dimensions) as store:
        index = VectorStoreIndex.from_vector_store(
            store, embed_model=PurposeEmbedding(endpoint, embedded.model, dimensions)
        )
        matches = index.as_retriever(
            similarity_top_k=k,
            filters=MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="kind", value=list(SEARCH_KINDS), operator=FilterOperator.IN
                    )
                ]
            ),
        ).retrieve(QueryBundle(query_str=question, embedding=embedded.vectors[0]))
        evidence = [
            evidence_from_node(m.node, m.score, neighbouring_passages(m.node, store))
            for m in matches
        ]
        return evidence, table, embedded.seconds


def ingest_status(books, database, store_factory=vector_store):
    """1. Extract each selected book to count current passages and hidden pages.
    2. Read each recorded model through LlamaIndex's metadata filters.
    3. Report how many current stable ids are stored for each book and model.
    """
    models = database.models()
    rows = []
    for book in books:
        extraction = extract_book(book.path, book.id, book.language)
        passages = extraction.passages
        hidden = sum(page.text_source == "hidden" for page in extraction.pages)
        identities = {passage_id(p) for p in passages}
        for table, model, dimensions in models:
            record = database.model(table)
            nodes = []
            if record and record[2]:
                with managed_store(
                    store_factory, database.url, table, dimensions
                ) as store:
                    nodes = store.get_nodes(
                        filters=MetadataFilters(
                            filters=[MetadataFilter(key="book_id", value=book.id)]
                        )
                    )
            stored = len(identities.intersection(n.node_id for n in nodes))
            rows.append(
                dict(
                    book_id=book.id,
                    model=model,
                    table=table,
                    stored=stored,
                    extracted=len(passages),
                    hidden=hidden,
                )
            )
        if not models:
            rows.append(
                dict(
                    book_id=book.id,
                    model=None,
                    table=None,
                    stored=0,
                    extracted=len(passages),
                    hidden=hidden,
                )
            )
    return rows
