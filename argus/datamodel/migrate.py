#!/usr/bin/env python3
"""Apply ordered SQL migrations to ClickHouse, Postgres and pgvector.

Convention:
``NNN_name.up.sql`` / ``NNN_name.down.sql``, applied in numeric order, with the
applied version recorded in a ``schema_migrations`` table per schema.

Two engine differences are handled here rather than pushed onto the SQL author:

* **Statement splitting.** ClickHouse accepts one statement per request, so its
  files are split. Postgres accepts a whole script in one execute -- and *must*
  get it whole, because a ``DO $$ ... $$`` block contains semicolons that any
  naive splitter would cut in half. That is why the split is engine-specific and
  quote-aware rather than a global ``text.split(";")``.

* **Transactionality.** Postgres DDL is transactional, so a failed migration
  rolls back cleanly. ClickHouse DDL is not, so a failure there leaves the
  schema half-applied and is recorded as ``dirty`` for a human to resolve --
  silently retrying would compound the damage.

Usage:
    python datamodel/migrate.py up
    python datamodel/migrate.py up --engine clickhouse
    python datamodel/migrate.py status
    python datamodel/migrate.py down --engine postgres --schema pulse --steps 1
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_ROOT = REPO_ROOT / "datamodel" / "migrations"

VERSION_RE = re.compile(r"^(\d+)_(.+)\.(up|down)\.sql$")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.version:03d}_{self.name}"


def discover(directory: Path, direction: str = "up") -> list[Migration]:
    """Return migrations in `directory`, ordered by version.

    Raises on a duplicate version: two files claiming version 007 would apply in
    filesystem order, which differs between machines.
    """
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob(f"*.{direction}.sql")):
        match = VERSION_RE.match(path.name)
        if not match:
            raise SystemExit(f"malformed migration filename: {path.name}")
        version = int(match.group(1))
        if version in found:
            raise SystemExit(
                f"duplicate migration version {version} in {directory}: "
                f"{found[version].path.name} and {path.name}"
            )
        found[version] = Migration(version, match.group(2), path)
    return [found[v] for v in sorted(found)]


def split_statements(sql: str) -> list[str]:
    """Split a script into statements, respecting quotes and dollar-quoting.

    Used for ClickHouse only. A plain split on ";" would break both string
    literals containing semicolons and Postgres DO blocks, so the scanner tracks
    whether it is inside a single quote, a double quote, a line comment, a block
    comment, or a dollar-quoted body.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_single = in_double = in_line_comment = in_block_comment = False
    dollar_tag: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if in_single:
            buf.append(ch)
            # Doubled quote is an escaped quote, not a terminator.
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        # Not inside anything.
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        dollar = re.match(r"\$[A-Za-z_]*\$", sql[i:])
        if dollar:
            dollar_tag = dollar.group(0)
            buf.append(dollar_tag)
            i += len(dollar_tag)
            continue
        if ch == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf)
    if tail.strip():
        statements.append(tail)

    return [s.strip() for s in statements if _has_sql(s)]


def _has_sql(statement: str) -> bool:
    """True if the statement contains anything besides comments and whitespace."""
    stripped = re.sub(r"--[^\n]*", "", statement)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    return bool(stripped.strip())


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------


class ClickHouseTarget:
    engine = "clickhouse"

    def __init__(self, schema: str) -> None:
        import clickhouse_connect

        self.schema = schema
        self.client = clickhouse_connect.get_client(
            host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            username=os.getenv("CLICKHOUSE_USER", "app"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "dev_password"),
        )

    def ensure_bookkeeping(self) -> None:
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self.schema}")
        self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.schema}.schema_migrations (
                version   Int64,
                name      String,
                dirty     UInt8,
                applied_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(applied_at) ORDER BY version
            """
        )

    def applied(self) -> dict[int, bool]:
        rows = self.client.query(
            f"SELECT version, dirty FROM {self.schema}.schema_migrations FINAL "
            "ORDER BY version"
        ).result_rows
        return {int(v): bool(d) for v, d in rows}

    def apply(self, migration: Migration) -> None:
        sql = migration.path.read_text(encoding="utf-8")
        # ClickHouse DDL is not transactional, so mark dirty up front. If a
        # statement fails, the row stays dirty and `status` reports it.
        self._record(migration, dirty=True)
        for statement in split_statements(sql):
            self.client.command(statement)
        self._record(migration, dirty=False)

    def _record(self, migration: Migration, *, dirty: bool) -> None:
        self.client.command(
            f"INSERT INTO {self.schema}.schema_migrations (version, name, dirty) "
            f"VALUES ({migration.version}, '{migration.name}', {int(dirty)})"
        )

    def remove(self, version: int) -> None:
        self.client.command(
            f"ALTER TABLE {self.schema}.schema_migrations DELETE WHERE version = {version}"
        )

    def close(self) -> None:
        self.client.close()


def _ensure_database(psycopg, server: str, database: str) -> None:
    """Create the database if it does not exist yet.

    One Postgres server holds several service databases. Creating them here,
    rather than in a container init script, means a fresh server, an existing
    one and a managed cloud instance all bootstrap the same way.
    """
    with psycopg.connect(f"{server}/postgres", autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if not exists:
            # Identifier, not a value: it comes from the TARGETS table in this
            # file, never from input, and is validated by the pattern below.
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", database):
                raise ValueError(f"refusing to create database {database!r}")
            admin.execute(f'CREATE DATABASE "{database}"')
            print(f"    created database {database}")


class PostgresTarget:
    engine = "postgres"

    def __init__(self, schema: str, *, host_env: str = "POSTGRES") -> None:
        import psycopg

        self.schema = schema
        # The vector store is a separate instance with its own env prefix.
        prefix = host_env
        default_host = "codex-db" if prefix == "CODEX" else "postgres"
        server = (
            f"postgresql://{os.getenv(f'{prefix}_USER', 'argus')}:"
            f"{os.getenv(f'{prefix}_PASSWORD', 'dev_password')}@"
            f"{os.getenv(f'{prefix}_HOST', default_host)}:"
            f"{os.getenv(f'{prefix}_PORT', '5432')}"
        )
        _ensure_database(psycopg, server, schema)
        self.conn = psycopg.connect(f"{server}/{schema}", autocommit=False)

    def ensure_bookkeeping(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    BIGINT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    dirty      BOOLEAN NOT NULL DEFAULT FALSE,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        self.conn.commit()

    def applied(self) -> dict[int, bool]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT version, dirty FROM schema_migrations ORDER BY version")
            return {int(v): bool(d) for v, d in cur.fetchall()}

    def apply(self, migration: Migration) -> None:
        sql = migration.path.read_text(encoding="utf-8")
        # Postgres DDL is transactional: the whole file plus its bookkeeping row
        # commit together, so there is no dirty state to recover from.
        with self.conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (version, name, dirty) "
                "VALUES (%s, %s, FALSE) ON CONFLICT (version) DO UPDATE "
                "SET dirty = FALSE, applied_at = now()",
                (migration.version, migration.name),
            )
        self.conn.commit()

    def remove(self, version: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

#: (engine, schema, env prefix) for every migratable schema, in apply order.
TARGETS: list[tuple[str, str, str]] = [
    ("clickhouse", "analytics", "CLICKHOUSE"),
    ("postgres", "pulse", "POSTGRES"),
    ("postgres", "sentry", "POSTGRES"),
    ("postgres", "analyst", "POSTGRES"),
    ("pgvector", "codex", "CODEX"),
]


def directory_for(engine: str, schema: str) -> Path:
    return MIGRATIONS_ROOT / engine / schema


def open_target(engine: str, schema: str, prefix: str):
    if engine == "clickhouse":
        return ClickHouseTarget(schema)
    return PostgresTarget(schema, host_env=prefix)


def selected_targets(engine: str | None, schema: str | None):
    for eng, sch, prefix in TARGETS:
        if engine and eng != engine:
            continue
        if schema and sch != schema:
            continue
        yield eng, sch, prefix


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    failures = 0
    for engine, schema, prefix in selected_targets(args.engine, args.schema):
        directory = directory_for(engine, schema)
        if not directory.is_dir():
            print(f"  - {engine}/{schema}: no migrations directory, skipping")
            continue

        migrations = discover(directory, "up")
        target = _connect_with_retry(engine, schema, prefix, args.wait)
        if target is None:
            print(f"  x {engine}/{schema}: could not connect")
            failures += 1
            continue

        try:
            target.ensure_bookkeeping()
            applied = target.applied()

            dirty = [v for v, is_dirty in applied.items() if is_dirty]
            if dirty:
                print(
                    f"  x {engine}/{schema}: version(s) {dirty} are dirty -- a "
                    "previous run failed mid-flight. Resolve by hand, then "
                    "delete the row from schema_migrations."
                )
                failures += 1
                continue

            pending = [m for m in migrations if m.version not in applied]
            if not pending:
                latest = max(applied) if applied else 0
                print(f"  = {engine}/{schema}: up to date (v{latest})")
                continue

            for migration in pending:
                started = time.monotonic()
                try:
                    target.apply(migration)
                except Exception as exc:
                    print(f"  x {engine}/{schema}: {migration.label} FAILED: {exc}")
                    failures += 1
                    break
                elapsed = (time.monotonic() - started) * 1000
                print(f"  + {engine}/{schema}: {migration.label} ({elapsed:.0f}ms)")
        finally:
            target.close()

    return 1 if failures else 0


def cmd_status(args: argparse.Namespace) -> int:
    problems = 0
    for engine, schema, prefix in selected_targets(args.engine, args.schema):
        directory = directory_for(engine, schema)
        if not directory.is_dir():
            continue
        migrations = discover(directory, "up")
        latest_file = migrations[-1].version if migrations else 0

        target = _connect_with_retry(engine, schema, prefix, args.wait)
        if target is None:
            print(f"  x {engine}/{schema}: unreachable")
            problems += 1
            continue
        try:
            target.ensure_bookkeeping()
            applied = target.applied()
            current = max(applied) if applied else 0
            dirty = [v for v, d in applied.items() if d]
            if dirty:
                print(f"  x {engine}/{schema}: v{current} DIRTY {dirty}")
                problems += 1
            elif current != latest_file:
                print(
                    f"  ! {engine}/{schema}: at v{current}, latest file is "
                    f"v{latest_file} (lagging)"
                )
                problems += 1
            else:
                print(f"  = {engine}/{schema}: v{current} clean")
        finally:
            target.close()
    return 1 if problems else 0


def cmd_down(args: argparse.Namespace) -> int:
    if not args.schema:
        raise SystemExit("down requires --schema (it is destructive; be explicit)")

    for engine, schema, prefix in selected_targets(args.engine, args.schema):
        directory = directory_for(engine, schema)
        downs = {m.version: m for m in discover(directory, "down")}
        target = _connect_with_retry(engine, schema, prefix, args.wait)
        if target is None:
            raise SystemExit(f"cannot connect to {engine}/{schema}")
        try:
            target.ensure_bookkeeping()
            applied = sorted(target.applied(), reverse=True)
            for version in applied[: args.steps]:
                migration = downs.get(version)
                if migration is None:
                    print(f"  ! {engine}/{schema}: no down file for v{version}, stopping")
                    break
                sql = migration.path.read_text(encoding="utf-8")
                if engine == "clickhouse":
                    for statement in split_statements(sql):
                        target.client.command(statement)
                else:
                    with target.conn.cursor() as cur:
                        cur.execute(sql)
                    target.conn.commit()
                target.remove(version)
                print(f"  - {engine}/{schema}: reverted {migration.label}")
        finally:
            target.close()
    return 0


def _connect_with_retry(engine: str, schema: str, prefix: str, wait: int):
    """Retry connection for `wait` seconds.

    Compose healthchecks reduce the need for this, but the migrate container can
    still win the race against a database that is accepting TCP before it is
    ready to serve.
    """
    deadline = time.monotonic() + wait
    last: Exception | None = None
    while True:
        try:
            return open_target(engine, schema, prefix)
        except Exception as exc:
            last = exc
            if time.monotonic() >= deadline:
                if wait:
                    print(f"    ({engine}/{schema} last error: {last})")
                return None
            time.sleep(1.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "down", "status"])
    parser.add_argument("--engine", choices=["clickhouse", "postgres", "pgvector"])
    parser.add_argument("--schema")
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument(
        "--wait", type=int, default=60, help="seconds to wait for a database"
    )
    args = parser.parse_args(argv)

    print(f"migrate {args.command}")
    handler = {"up": cmd_up, "down": cmd_down, "status": cmd_status}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
