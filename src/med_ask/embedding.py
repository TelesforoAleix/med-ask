"""Plain-text embedding seams for the compatible endpoint."""

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
    def __init__(self):
        self.purpose = os.environ.get("EMBEDDING_PURPOSE", "embed")
        self.client = OpenAI(
            base_url=os.environ["MODEL_BASE_URL"],
            api_key=os.environ["MODEL_API_KEY"],
            timeout=float(os.environ.get("EMBEDDING_TIMEOUT", "20")),
            max_retries=0,
        )

    def request(self, texts: list[str]) -> Embedded:
        """1. Send unchanged text using the configured embedding purpose.
        2. Keep the reported model, ordered vectors, and elapsed time.
        3. Reject incomplete or inconsistent responses.
        """
        started = perf_counter()
        response = self.client.embeddings.create(
            model=self.purpose, input=texts, encoding_format="float"
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
    """1. Embed the question as plain text at the query-role seam."""
    return endpoint.request([question])


def embed_passages(endpoint, texts: list[str]) -> Embedded:
    """1. Embed passages as plain text at the passage-role seam."""
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
        """1. Use the same plain-text query seam for asynchronous callers."""
        return self._get_query_embedding(query)
