"""Codex's knowledge store -- pgvector plus Postgres full-text search.

Two guards matter here:

* **No mixed vector spaces.** A collection records the model and dimension
  that produced its vectors. Ingesting with a different model is refused, not
  silently mixed -- vectors from two models are not comparable, and retrieval
  would quietly get worse with no error anywhere.
* **Idempotent ingest.** A document is keyed on its path and its content
  hash. Re-ingesting an unchanged file is a no-op; a changed file replaces its
  chunks in one transaction.
"""

from __future__ import annotations

import json
from typing import Any

from argus_core.clients.postgres import PostgresClient
from argus_core.schemas.knowledge import CollectionSpec

from .chunker import Chunk
from .search import Hit


class ModelMismatch(RuntimeError):
    pass


def _vec(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class CodexStore:
    def __init__(self, client: PostgresClient) -> None:
        self.db = client

    def ensure_collection(self, spec: CollectionSpec, model: str, dim: int) -> None:
        row = self.db.fetch_one(
            "SELECT embedding_model, embedding_dim FROM collection WHERE name = %(n)s",
            {"n": spec.name},
        )
        if row and (row["embedding_model"], row["embedding_dim"]) != (model, dim):
            raise ModelMismatch(
                f"collection {spec.name} was embedded with {row['embedding_model']} "
                f"({row['embedding_dim']}d); refusing to add {model} ({dim}d). "
                "Re-embed the collection from scratch to change model."
            )
        self.db.execute(
            """
            INSERT INTO collection (name, description, embedding_model, embedding_dim,
                                    chunk_size, chunk_overlap)
            VALUES (%(n)s, %(d)s, %(m)s, %(dim)s, %(cs)s, %(co)s)
            ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,
                chunk_size = EXCLUDED.chunk_size, chunk_overlap = EXCLUDED.chunk_overlap,
                updated_at = now()
            """,
            {"n": spec.name, "d": spec.description.strip(), "m": model, "dim": dim,
             "cs": spec.chunk_size, "co": spec.chunk_overlap},
        )

    def document_hash(self, collection: str, path: str) -> str | None:
        row = self.db.fetch_one(
            "SELECT content_hash FROM document WHERE collection = %(c)s AND source_path = %(p)s",
            {"c": collection, "p": path},
        )
        return row["content_hash"] if row else None

    def replace_document(self, collection: str, path: str, title: str, digest: str,
                         size: int, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        with self.db.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM document WHERE collection = %s AND source_path = %s",
                        (collection, path))
            cur.execute(
                "INSERT INTO document (collection, source_path, title, content_hash, byte_size) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (collection, path, title, digest, size),
            )
            doc_id = cur.fetchone()["id"]
            cur.executemany(
                "INSERT INTO chunk (document_id, collection, chunk_index, content, heading, "
                "token_estimate, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
                [(doc_id, collection, c.index, c.content, c.heading, c.token_estimate, _vec(v))
                 for c, v in zip(chunks, vectors, strict=True)],
            )

    def remove_missing(self, collection: str, keep: set[str]) -> int:
        rows = self.db.fetch_all(
            "SELECT source_path FROM document WHERE collection = %(c)s", {"c": collection}
        )
        gone = [r["source_path"] for r in rows if r["source_path"] not in keep]
        for path in gone:
            self.db.execute(
                "DELETE FROM document WHERE collection = %(c)s AND source_path = %(p)s",
                {"c": collection, "p": path},
            )
        return len(gone)

    def refresh_counts(self, collection: str) -> None:
        self.db.execute(
            """
            UPDATE collection SET
                document_count = (SELECT count(*) FROM document WHERE collection = %(c)s),
                chunk_count = (SELECT count(*) FROM chunk WHERE collection = %(c)s),
                ingested_at = now(), updated_at = now()
            WHERE name = %(c)s
            """,
            {"c": collection},
        )

    def collections(self) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT * FROM collection ORDER BY name")

    # -- retrieval -------------------------------------------------------------

    _SELECT = """
        SELECT c.id AS chunk_id, d.source_path, d.title, c.heading, c.content
    """

    def vector_search(self, collection: str | None, vector: list[float], limit: int) -> list[Hit]:
        rows = self.db.fetch_all(
            self._SELECT + """,
                   1 - (c.embedding <=> %(v)s::vector) AS score
            FROM chunk c JOIN document d ON d.id = c.document_id
            WHERE (%(col)s::text IS NULL OR c.collection = %(col)s::text)
            ORDER BY c.embedding <=> %(v)s::vector
            LIMIT %(k)s
            """,
            {"v": _vec(vector), "col": collection, "k": limit},
        )
        return [_hit(r, vector_score=float(r["score"])) for r in rows]

    def keyword_search(self, collection: str | None, terms: list[str], limit: int) -> list[Hit]:
        if not terms:
            return []
        # OR of the terms, each parsed by the same 'simple' configuration that
        # built the index -- so CTL-REV-004 matches as one token. Values are
        # bound; only the number of placeholders depends on the input.
        query = " || ".join(f"plainto_tsquery('simple', %(t{i})s)" for i in range(len(terms)))
        params: dict[str, Any] = {f"t{i}": t for i, t in enumerate(terms)}
        params.update({"col": collection, "k": limit})
        rows = self.db.fetch_all(
            self._SELECT + f""",
                   ts_rank_cd(c.search_text, q.query, 32) AS score
            FROM chunk c JOIN document d ON d.id = c.document_id,
                 (SELECT {query} AS query) AS q
            WHERE c.search_text @@ q.query
              AND (%(col)s::text IS NULL OR c.collection = %(col)s::text)
            ORDER BY score DESC
            LIMIT %(k)s
            """,
            params,
        )
        return [_hit(r, keyword_score=float(r["score"])) for r in rows]


def _hit(row: dict[str, Any], **scores: float) -> Hit:
    return Hit(
        chunk_id=int(row["chunk_id"]),
        source_path=row["source_path"],
        title=row["title"],
        heading=row["heading"],
        path=f"{row['title']} > {row['heading']}",
        content=row["content"],
        **scores,
    )


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
