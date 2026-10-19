"""Tests for the migration statement splitter.

ClickHouse takes one statement per request, so migration scripts must be split.
Splitting SQL on ";" is the obvious implementation and it is wrong: it cuts
string literals and Postgres dollar-quoted blocks in half. These tests pin the
cases that a naive splitter gets wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "datamodel"))

from migrate import discover, split_statements  # noqa: E402


def test_simple_split():
    assert split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_trailing_statement_without_semicolon_is_kept():
    assert split_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]


def test_comment_only_input_yields_nothing():
    assert split_statements("-- just a comment\n") == []
    assert split_statements("/* block */\n;\n") == []


def test_semicolon_inside_string_literal_does_not_split():
    sql = "INSERT INTO t VALUES ('a;b');"
    assert split_statements(sql) == ["INSERT INTO t VALUES ('a;b')"]


def test_doubled_quote_is_an_escape_not_a_terminator():
    sql = "SELECT 'it''s; fine';"
    assert split_statements(sql) == ["SELECT 'it''s; fine'"]


def test_semicolon_inside_double_quoted_identifier_does_not_split():
    sql = 'SELECT "weird;name" FROM t;'
    assert split_statements(sql) == ['SELECT "weird;name" FROM t']


def test_semicolon_inside_line_comment_does_not_split():
    sql = "SELECT 1 -- trailing ; comment\n;SELECT 2;"
    assert split_statements(sql) == ["SELECT 1 -- trailing ; comment", "SELECT 2"]


def test_semicolon_inside_block_comment_does_not_split():
    sql = "SELECT 1 /* ; not a split ; */ ;SELECT 2;"
    out = split_statements(sql)
    assert len(out) == 2
    assert "not a split" in out[0]


def test_dollar_quoted_block_stays_one_statement():
    """The case that motivates the whole scanner.

    A DO block contains its own statements, each ending in a semicolon. Cutting
    on those produces fragments that are individually invalid SQL.
    """
    sql = """
    DO $$
    BEGIN
        CREATE INDEX a ON t (x);
        CREATE INDEX b ON t (y);
    END
    $$;
    SELECT 1;
    """
    out = split_statements(sql)
    assert len(out) == 2
    assert out[0].count("$$") == 2
    assert out[0].count(";") == 2  # the two inner statements survived
    assert out[1] == "SELECT 1"


def test_tagged_dollar_quote_is_matched_by_tag():
    sql = "DO $body$ BEGIN PERFORM 1; END $body$; SELECT 2;"
    out = split_statements(sql)
    assert len(out) == 2
    assert "$body$" in out[0]
    assert out[1] == "SELECT 2"


# --------------------------------------------------------------------------
# Applied to the real migration files
# --------------------------------------------------------------------------

MIGRATIONS = REPO_ROOT / "datamodel" / "migrations"


def _all_up_files() -> list[Path]:
    return sorted(MIGRATIONS.rglob("*.up.sql"))


def test_repo_has_migrations():
    assert _all_up_files(), "no migration files found"


@pytest.mark.parametrize("path", _all_up_files(), ids=lambda p: p.parent.name + "/" + p.name)
def test_real_migration_splits_into_balanced_statements(path: Path):
    """Every emitted statement must be self-contained.

    Balanced quotes and dollar tags per statement is the property that proves
    nothing was cut mid-literal.
    """
    for statement in split_statements(path.read_text(encoding="utf-8")):
        assert statement.count("$$") % 2 == 0, f"unbalanced $$ in: {statement[:80]}"
        # Strip comments before counting quotes: prose in a comment may contain
        # an apostrophe, which is not a SQL literal.
        import re

        body = re.sub(r"--[^\n]*", "", statement)
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        assert body.count("'") % 2 == 0, f"unbalanced quote in: {statement[:80]}"


def test_every_up_migration_has_a_matching_down():
    """A migration with no down file cannot be rolled back.

    The pgvector and postgres schemas are recreated wholesale by `make clean`,
    so this is a warning-level property enforced only where down files exist.
    """
    for directory in {p.parent for p in _all_up_files()}:
        ups = {m.version for m in discover(directory, "up")}
        downs = {m.version for m in discover(directory, "down")}
        orphans = downs - ups
        assert not orphans, f"{directory.name}: down files with no up: {orphans}"


def test_discover_rejects_duplicate_versions(tmp_path: Path):
    (tmp_path / "001_a.up.sql").write_text("SELECT 1;")
    (tmp_path / "001_b.up.sql").write_text("SELECT 2;")
    with pytest.raises(SystemExit, match="duplicate migration version"):
        discover(tmp_path, "up")


def test_discover_rejects_malformed_filename(tmp_path: Path):
    (tmp_path / "no-version.up.sql").write_text("SELECT 1;")
    with pytest.raises(SystemExit, match="malformed migration filename"):
        discover(tmp_path, "up")


def test_discover_orders_numerically_not_lexically(tmp_path: Path):
    # Lexical ordering would put 010 before 009.
    for name in ["001_a.up.sql", "009_b.up.sql", "010_c.up.sql", "002_d.up.sql"]:
        (tmp_path / name).write_text("SELECT 1;")
    assert [m.version for m in discover(tmp_path, "up")] == [1, 2, 9, 10]
