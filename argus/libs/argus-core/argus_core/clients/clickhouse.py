"""ClickHouse access.

Thin on purpose: the compiler already produced parameterised SQL, so this layer
only executes it and normalises the result. Server-side binding is used, which
is why the compiler emits the typed `{name:Type}` placeholder syntax.
"""

from __future__ import annotations

from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from ..settings import ClickHouseSettings


class ClickHouseClient:
    def __init__(self, settings: ClickHouseSettings) -> None:
        self._settings = settings
        self._client: Client | None = None

    def connect(self) -> Client:
        if self._client is None:
            s = self._settings
            self._client = clickhouse_connect.get_client(
                host=s.clickhouse_host,
                port=s.clickhouse_port,
                username=s.clickhouse_user,
                password=s.clickhouse_password,
                database=s.clickhouse_database,
                secure=s.clickhouse_secure,
            )
        return self._client

    def query(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> tuple[list[str], list[list[Any]]]:
        """Execute and return (column_names, rows)."""
        result = self.connect().query(
            sql,
            parameters=parameters or {},
            # Keep decimals exact rather than letting them become floats on the
            # way out; revenue figures are summed downstream.
            query_formats={"Decimal": "string"},
        )
        return list(result.column_names), [list(row) for row in result.result_rows]

    def query_dicts(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        columns, rows = self.query(sql, parameters)
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> None:
        self.connect().command(sql, parameters=parameters or {})

    def ping(self) -> bool:
        try:
            self.connect().command("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
