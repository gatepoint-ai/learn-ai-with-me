"""Datasource registry.

A lens names its datasource (`analytics`, `pulse`, `sentry`) and its dialect.
This module turns that name into something that can execute SQL, so the rest of
Prism never branches on which engine it is talking to.

Connections are opened lazily and cached. A lens pointing at an unreachable
datasource therefore fails on its first query rather than preventing the whole
service from starting -- Prism should still serve the ClickHouse lenses when
Postgres is down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from argus_core.clients.clickhouse import ClickHouseClient
from argus_core.clients.postgres import PostgresClient
from argus_core.logging import get_logger
from argus_core.schemas.lens import Dialect
from argus_core.settings import ClickHouseSettings, PostgresSettings

log = get_logger("prism.datasources")


class DatasourceError(RuntimeError):
    """A lens names a datasource this service cannot reach or does not know."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


class Executor(Protocol):
    def run(self, sql: str, params: dict[str, Any]) -> QueryResult: ...

    def healthy(self) -> bool: ...


class ClickHouseExecutor:
    dialect = Dialect.CLICKHOUSE

    def __init__(self, settings: ClickHouseSettings) -> None:
        self._client = ClickHouseClient(settings)

    def run(self, sql: str, params: dict[str, Any]) -> QueryResult:
        columns, raw = self._client.query(sql, params)
        rows = [dict(zip(columns, r, strict=True)) for r in raw]
        return QueryResult(columns=columns, rows=rows, row_count=len(rows))

    def healthy(self) -> bool:
        return self._client.ping()


class PostgresExecutor:
    dialect = Dialect.POSTGRES

    def __init__(self, dsn: str) -> None:
        self._client = PostgresClient(dsn)

    def run(self, sql: str, params: dict[str, Any]) -> QueryResult:
        rows = self._client.fetch_all(sql, params)
        columns = list(rows[0].keys()) if rows else []
        return QueryResult(columns=columns, rows=rows, row_count=len(rows))

    def healthy(self) -> bool:
        return self._client.ping()


class DatasourceRegistry:
    """Maps a lens's `datasource` name to an executor.

    The Postgres datasources share one server and differ only by database, which
    is why they are derived from a single settings object rather than configured
    separately. Adding a datasource is a line here plus a lens that names it.
    """

    #: Postgres datasource name -> database name. Identical today, but the
    #: indirection is what lets a datasource be renamed without a migration.
    POSTGRES_DATABASES = {
        "pulse": "pulse",
        "sentry": "sentry",
        "analyst": "analyst",
    }

    def __init__(
        self, clickhouse: ClickHouseSettings, postgres: PostgresSettings
    ) -> None:
        self._ch_settings = clickhouse
        self._pg_settings = postgres
        self._executors: dict[str, Executor] = {}

    @property
    def known(self) -> list[str]:
        return ["analytics", *sorted(self.POSTGRES_DATABASES)]

    def get(self, datasource: str) -> Executor:
        if datasource in self._executors:
            return self._executors[datasource]

        if datasource == "analytics":
            executor: Executor = ClickHouseExecutor(self._ch_settings)
        elif datasource in self.POSTGRES_DATABASES:
            database = self.POSTGRES_DATABASES[datasource]
            s = self._pg_settings
            dsn = (
                f"postgresql://{s.postgres_user}:{s.postgres_password}"
                f"@{s.postgres_host}:{s.postgres_port}/{database}"
            )
            executor = PostgresExecutor(dsn)
        else:
            raise DatasourceError(
                f"unknown datasource {datasource!r}; known: {', '.join(self.known)}"
            )

        self._executors[datasource] = executor
        log.info("datasource_opened", datasource=datasource)
        return executor

    def check_dialect(self, datasource: str, dialect: Dialect) -> None:
        """Reject a lens whose dialect contradicts its datasource.

        A lens declaring `datasource: analytics` with `dialect: postgres` would
        compile pyformat placeholders and send them to ClickHouse, which fails
        with an opaque parse error at query time. Catching it at load time names
        the actual problem.
        """
        executor = self.get(datasource)
        actual = getattr(executor, "dialect", None)
        if actual is not None and actual is not dialect:
            raise DatasourceError(
                f"datasource {datasource!r} speaks {actual.value}, "
                f"but the lens declares dialect {dialect.value}"
            )

    def health(self) -> dict[str, bool]:
        """Report reachability for every datasource that has been opened."""
        return {name: ex.healthy() for name, ex in self._executors.items()}
