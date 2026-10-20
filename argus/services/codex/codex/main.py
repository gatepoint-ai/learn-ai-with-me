"""Codex -- the knowledge base.

Ingests the Markdown documents under `config/codex/docs/`, one collection per
`config/codex/collections/*.yaml`, and answers retrieval requests with hybrid
search plus reranking. Codex retrieves; it never answers. Composing an answer
from passages is the Analyst's job, and it must cite what Codex returned.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from argus_core.clients.postgres import PostgresClient
from argus_core.config import ConfigError, load_collections
from argus_core.logging import configure_logging, get_logger
from argus_core.schemas.knowledge import CollectionSpec
from argus_core.settings import CodexDbSettings, ConfigPaths, EmbeddingSettings, LogSettings

from . import chunker, search
from .models import LocalModels
from .store import CodexStore

log = get_logger("codex")

API = "/codex/v1"


class State:
    def __init__(self) -> None:
        self.paths = ConfigPaths()
        self.collections: dict[str, CollectionSpec] = {}
        self.models: LocalModels | None = None
        self.store: CodexStore | None = None
        self.embedding = EmbeddingSettings()


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = LogSettings(service_name="codex")
    configure_logging(settings.service_name, settings.log_level, settings.log_json)
    try:
        state.collections = load_collections(state.paths.collections)
    except ConfigError as exc:
        log.error("config_invalid", problems=exc.problems)
        raise
    emb = state.embedding
    state.models = LocalModels(emb.embedding_model, emb.rerank_model if emb.rerank_enabled else None)
    state.models.warm()
    state.store = CodexStore(PostgresClient(CodexDbSettings().dsn))
    log.info("codex_ready", collections=sorted(state.collections))
    yield
    state.store.db.close()


app = FastAPI(title="Argus Codex", description="Knowledge base", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


def ingest_collection(spec: CollectionSpec) -> dict[str, Any]:
    docs_root = Path(state.paths.knowledge_docs)
    files = sorted({p for pattern in spec.sources for p in docs_root.glob(pattern) if p.is_file()})
    if not files:
        raise HTTPException(500, {"error": "no_documents",
                                  "message": f"collection {spec.name}: no files match {spec.sources}"})
    dim = state.models.dimension()
    if dim != state.embedding.embedding_dim:
        raise HTTPException(500, {"error": "dimension_mismatch",
                                  "message": f"model gives {dim}d, config says {state.embedding.embedding_dim}d"})
    state.store.ensure_collection(spec, state.embedding.embedding_model, dim)

    added = unchanged = chunks_total = 0
    keep: set[str] = set()
    for path in files:
        rel = path.relative_to(docs_root).as_posix()
        keep.add(rel)
        text = path.read_text(encoding="utf-8")
        digest = chunker.content_hash(text)
        if state.store.document_hash(spec.name, rel) == digest:
            unchanged += 1
            continue
        title = chunker.title_of(text, path.stem)
        chunks = chunker.chunk_document(text, title, spec.chunk_size, spec.chunk_overlap)
        # Embed heading + text: a chunk that says "approval limits" under the
        # heading "WO-POL-2" should be findable by either.
        vectors = state.models.embed_passages([f"{c.path}\n{c.content}" for c in chunks])
        state.store.replace_document(spec.name, rel, title, digest, len(text.encode()), chunks, vectors)
        added += 1
        chunks_total += len(chunks)
    removed = state.store.remove_missing(spec.name, keep)
    state.store.refresh_counts(spec.name)
    return {"collection": spec.name, "documents": len(files), "ingested": added,
            "unchanged": unchanged, "removed": removed, "chunks_written": chunks_total}


@app.post(f"{API}/ingest")
def ingest(collection: str | None = None) -> dict[str, Any]:
    targets = [_collection(collection)] if collection else list(state.collections.values())
    started = time.perf_counter()
    results = [ingest_collection(spec) for spec in targets]
    return {"collections": len(results), "failed": 0, "results": results,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    collection: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    rerank: bool | None = None


def run_search(req: SearchRequest) -> dict[str, Any]:
    spec = _collection(req.collection) if req.collection else None
    top_k = req.top_k or (spec.top_k if spec else 5)
    weight = spec.keyword_weight if spec else 0.5
    threshold = spec.similarity_threshold if spec else 0.5
    pool = max(15, top_k * 3)

    vector_hits = keyword_hits = []
    if req.mode in ("hybrid", "vector"):
        vector_hits = state.store.vector_search(req.collection, state.models.embed_query(req.query), pool)
    if req.mode in ("hybrid", "keyword"):
        keyword_hits = state.store.keyword_search(req.collection, search.keyword_terms(req.query), pool)

    w = {"hybrid": weight, "vector": 0.0, "keyword": 1.0}[req.mode]
    hits = search.fuse(vector_hits, keyword_hits, w)
    if req.mode != "keyword":
        hits = search.apply_threshold(hits, threshold)

    use_rerank = req.rerank if req.rerank is not None else (spec.rerank if spec else True)
    if use_rerank and hits:
        short = hits[:pool]
        scores = state.models.rerank(req.query, [f"{h.path}\n{h.content}" for h in short])
        if scores is not None:
            hits = search.rerank_order(short, scores)
    hits = hits[:top_k]
    return {
        "query": req.query,
        "collection": req.collection,
        "mode": req.mode,
        "reranked": bool(use_rerank),
        "hits": [
            {"citation": h.citation, "source_path": h.source_path, "heading": h.heading,
             "content": h.content, "scores": {"vector": round(h.vector_score, 4),
             "keyword": round(h.keyword_score, 4), "hybrid": round(h.hybrid_score, 4),
             "rerank": None if h.rerank_score is None else round(h.rerank_score, 4)}}
            for h in hits
        ],
    }


@app.post(f"{API}/search")
def search_endpoint(req: SearchRequest) -> dict[str, Any]:
    return run_search(req)


@app.get(f"{API}/collections")
def collections() -> list[dict[str, Any]]:
    stored = {r["name"]: r for r in state.store.collections()}
    return [
        {"name": s.name, "description": s.description.strip(), "keyword_weight": s.keyword_weight,
         "top_k": s.top_k, "documents": stored.get(s.name, {}).get("document_count", 0),
         "chunks": stored.get(s.name, {}).get("chunk_count", 0),
         "embedding_model": stored.get(s.name, {}).get("embedding_model")}
        for s in state.collections.values()
    ]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "collections": len(state.collections),
            "database": "up" if state.store and state.store.db.ping() else "unavailable",
            "embedding_model": state.embedding.embedding_model}


def _collection(name: str) -> CollectionSpec:
    spec = state.collections.get(name)
    if spec is None:
        raise HTTPException(404, {"error": "unknown_collection", "message": f"no collection {name!r}",
                                  "known": sorted(state.collections)})
    return spec
