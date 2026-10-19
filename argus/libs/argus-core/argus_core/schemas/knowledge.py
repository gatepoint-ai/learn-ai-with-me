"""Knowledge collection specification -- `config/knowledge/collections/`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .query import IDENTIFIER_PATTERN


class CollectionSpec(BaseModel):
    """One retrievable corpus.

    `chunk_size` is in characters rather than tokens on purpose: the ingester
    must not depend on a tokenizer that the embedding backend might change out
    from under it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str
    sources: list[str] = Field(default_factory=lambda: ["**/*.md"])
    chunk_size: int = Field(default=1200, ge=200, le=8000)
    chunk_overlap: int = Field(default=200, ge=0, le=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    similarity_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    rerank: bool = True

    def model_post_init(self, _context: object) -> None:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"collection {self.name}: chunk_overlap must be < chunk_size"
            )
