"""Postgres access via a psycopg connection pool.

Used by the KPI engine, sentry, agent (conversation history) and the RAG
vector store. The pool is opened lazily so importing this module never touches
the network -- unit tests import it freely.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class PostgresClient:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: ConnectionPool | None = None

    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(
                self._dsn,
                min_size=self._min,
                max_size=self._max,
                open=True,
                kwargs={"row_factory": dict_row},
            )
        return self._pool

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        with self.pool.connection() as conn:
            yield conn

    def fetch_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or {})
            return list(cur.fetchall())

    def fetch_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchone()

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        """Run a statement and return the affected row count."""
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.rowcount

    def execute_many(self, sql: str, rows: list[dict[str, Any]]) -> int:
        """Batch-insert in a single round trip.

        KPI recalculation writes thousands of points at a time; issuing those
        one statement at a time dominates the runtime.
        """
        if not rows:
            return 0
        with self.connection() as conn, conn.cursor() as cur:
            cur.executemany(sql, rows)
            return cur.rowcount

    def ping(self, timeout: float = 2.0) -> bool:
        """Reachability for health checks -- bounded, so /health never hangs.

        The pool otherwise waits up to 30 seconds for a connection, longer than
        any container health-check timeout; a database that does not exist yet
        would then make a running service look dead.
        """
        try:
            with self.pool.connection(timeout=timeout) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
        except Exception:
            return False

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
