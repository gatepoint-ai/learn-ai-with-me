"""Hybrid retrieval arithmetic -- pure, no I/O.

Two retrievers, two kinds of evidence:

* **Vector** similarity finds passages that *mean* the same as the question,
  even with no words in common.
* **Keyword** (full-text) search finds passages that contain the question's
  exact tokens -- control IDs, policy codes, reason codes -- which embeddings
  tend to blur together.

They are fused by a weighted sum of normalised scores, with the weight set per
collection. Then an optional cross-encoder reranks the short list by reading
question and passage together, which is slower but much more precise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# Words that carry no retrieval signal. Postgres' 'simple' configuration keeps
# IDs like CTL-REV-004 intact but has no stopword list, so we drop these here --
# otherwise "what", "does" and "the" would match every chunk equally.
STOPWORDS = frozenset(
    """a an and are as at be by can could did do does for from had has have how i if in
    is it its me my of on or our should so than that the their them then there these
    they this to us was we were what when where which who whom why will with would you
    your about after before any all also into more most no not only other out over
    same some such up very""".split()
)

TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")


def keyword_terms(question: str) -> list[str]:
    """Search terms for the keyword retriever, IDs kept whole."""
    seen: list[str] = []
    for token in TOKEN.findall(question):
        t = token.lower()
        if t not in STOPWORDS and len(t) > 1 and t not in seen:
            seen.append(t)
    return seen


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    source_path: str
    title: str
    heading: str
    path: str
    content: str
    vector_score: float = 0.0
    keyword_score: float = 0.0
    hybrid_score: float = 0.0
    rerank_score: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def citation(self) -> str:
        return f"{self.title} > {self.heading}"


def fuse(
    vector_hits: list[Hit], keyword_hits: list[Hit], keyword_weight: float
) -> list[Hit]:
    """Combine both result lists into one, ranked by weighted normalised score.

    Vector scores are cosine similarities, already on a 0..1 scale. Keyword
    ranks have no fixed scale, so they are divided by the best keyword score
    in this result set. A chunk found by only one retriever scores zero on the
    other.
    """
    best_kw = max((h.keyword_score for h in keyword_hits), default=0.0) or 1.0
    merged: dict[int, Hit] = {}
    for h in vector_hits:
        merged[h.chunk_id] = h
    for h in keyword_hits:
        norm = h.keyword_score / best_kw
        if h.chunk_id in merged:
            merged[h.chunk_id] = replace(merged[h.chunk_id], keyword_score=norm)
        else:
            merged[h.chunk_id] = replace(h, keyword_score=norm)
    out = [
        replace(h, hybrid_score=(1 - keyword_weight) * h.vector_score + keyword_weight * h.keyword_score)
        for h in merged.values()
    ]
    return sorted(out, key=lambda h: h.hybrid_score, reverse=True)


def apply_threshold(hits: list[Hit], threshold: float) -> list[Hit]:
    """Drop weak semantic-only matches.

    A chunk the keyword search found contains the question's exact terms and
    is kept. A chunk found only by vector similarity must clear the
    collection's threshold -- otherwise every question returns *something*,
    and "I don't know" becomes impossible to say.
    """
    return [h for h in hits if h.keyword_score > 0 or h.vector_score >= threshold]


def rerank_order(hits: list[Hit], scores: list[float]) -> list[Hit]:
    rescored = [replace(h, rerank_score=float(s)) for h, s in zip(hits, scores, strict=True)]
    return sorted(rescored, key=lambda h: h.rerank_score, reverse=True)
