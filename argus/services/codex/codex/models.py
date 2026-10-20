"""Local embedding and reranking models (ONNX via fastembed).

No API key and no network at query time: the weights are downloaded once when
the image is built. Embeddings are deliberately independent of the chat
provider -- the Analyst can switch LLM backend without re-embedding anything.
"""

from __future__ import annotations

from functools import cached_property


class LocalModels:
    def __init__(self, embedding_model: str, rerank_model: str | None) -> None:
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model

    @cached_property
    def _embedder(self):
        from fastembed import TextEmbedding

        return TextEmbedding(model_name=self.embedding_model)

    @cached_property
    def _reranker(self):
        if not self.rerank_model:
            return None
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        return TextCrossEncoder(model_name=self.rerank_model)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._embedder.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._embedder.query_embed([text]))).tolist()

    def dimension(self) -> int:
        return len(self.embed_query("dimension probe"))

    def rerank(self, query: str, passages: list[str]) -> list[float] | None:
        if self._reranker is None or not passages:
            return None
        return [float(s) for s in self._reranker.rerank(query, passages)]

    def warm(self) -> None:
        self.embed_query("warm up")
        self.rerank("warm up", ["warm up"])
