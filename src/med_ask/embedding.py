"""Optional query and passage roles for the compatible embedding endpoint."""

import os
from dataclasses import dataclass
from time import perf_counter

from llama_index.core.base.embeddings.base import BaseEmbedding
from openai import OpenAI
from pydantic import PrivateAttr


@dataclass(frozen=True)
class Embedded:
    model: str
    vectors: list[list[float]]
    seconds: float


class Endpoint:
    def __init__(self, purpose=None, roles=None):
        self.purpose = purpose or os.environ.get("EMBEDDING_PURPOSE", "embed")
        self.roles = (
            os.environ.get("EMBEDDING_ROLES", "false") == "true"
            if roles is None
            else roles
        )
        self.client = OpenAI(
            base_url=os.environ["MODEL_BASE_URL"],
            api_key=os.environ["MODEL_API_KEY"],
            timeout=float(os.environ.get("EMBEDDING_TIMEOUT", "20")),
            max_retries=0,
        )

    def request(self, texts: list[str], input_type=None) -> Embedded:
        """1. Send unchanged text, adding the input role only when enabled.
        2. Keep the reported model, ordered vectors, and elapsed time.
        3. Reject incomplete or inconsistent responses.
        """
        started = perf_counter()
        response = self.client.embeddings.create(
            model=self.purpose,
            input=texts,
            encoding_format="float",
            **({"extra_body": {"input_type": input_type}} if self.roles else {}),
        )
        vectors = [
            item.embedding for item in sorted(response.data, key=lambda x: x.index)
        ]
        if (
            not response.model
            or len(vectors) != len(texts)
            or not vectors
            or not vectors[0]
            or any(len(v) != len(vectors[0]) for v in vectors)
        ):
            raise ValueError("Invalid embedding response")
        return Embedded(response.model, vectors, perf_counter() - started)


def embed_query(endpoint, question: str) -> Embedded:
    """1. Embed unchanged question text with the query role when enabled."""
    if getattr(endpoint, "roles", False) is True:
        return endpoint.request([question], input_type="query")
    return endpoint.request([question])


def embed_passages(endpoint, texts: list[str]) -> Embedded:
    """1. Embed unchanged passage text with the passage role when enabled."""
    if getattr(endpoint, "roles", False) is True:
        return endpoint.request(texts, input_type="passage")
    return endpoint.request(texts)


class PurposeEmbedding(BaseEmbedding):
    """LlamaIndex adapter that pins every passage call to the probed model."""

    _endpoint: object = PrivateAttr()
    _reported: str = PrivateAttr()
    _dimensions: int = PrivateAttr()

    def __init__(self, endpoint, reported: str, dimensions: int):
        super().__init__(model_name=endpoint.purpose, embed_batch_size=1)
        self._endpoint, self._reported, self._dimensions = (
            endpoint,
            reported,
            dimensions,
        )

    def _check(self, result: Embedded) -> list[float]:
        """1. Refuse a changed model or dimension before returning a vector."""
        if result.model != self._reported or len(result.vectors[0]) != self._dimensions:
            raise ValueError("Embedding model changed; start a new index")
        return result.vectors[0]

    def _get_text_embedding(self, text: str) -> list[float]:
        """1. Embed a passage and enforce the ingest's model identity."""
        return self._check(embed_passages(self._endpoint, [text]))

    def _get_query_embedding(self, query: str) -> list[float]:
        """1. Embed a query and enforce the selected table's model identity."""
        return self._check(embed_query(self._endpoint, query))

    async def _aget_query_embedding(self, query: str) -> list[float]:
        """1. Use the same configured query seam for asynchronous callers."""
        return self._get_query_embedding(query)
