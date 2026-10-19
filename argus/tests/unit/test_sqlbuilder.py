"""Tests for the SQL compiler.

The compiler is the only component in the platform that turns caller input into
SQL, so it is the whole injection surface. These tests are written first and
deliberately include the attacks: a field name that is really a SQL fragment, an
operator smuggled through a value, a sort direction that is not a direction.

Two invariants are asserted throughout:
  1. Every identifier in the output came from the lens's declared fields.
  2. Every caller-supplied *value* is a bound parameter, never inlined.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus_core.schemas.lens import LensSpec, UnknownFieldError
from argus_core.schemas.query import QueryRequest
from argus_core.sqlbuilder import compile_query


@pytest.fixture
def lens() -> LensSpec:
    return LensSpec(
        name="usage_trends",
        label="Usage Trends",
        dialect="clickhouse",
        sql="SELECT * FROM analytics.usage_daily_agg",
        fields=[
            {"field": "usage_date", "data_type": "date"},
            {"field": "site_id", "data_type": "string"},
            {"field": "site_name", "data_type": "string"},
            {"field": "channel", "data_type": "string"},
            {"field": "event_count", "data_type": "integer"},
            {"field": "amount_collected", "data_type": "decimal"},
            {"field": "internal_note", "data_type": "string", "allow_filter": False},
        ],
    )


@pytest.fixture
def pg_lens(lens: LensSpec) -> LensSpec:
    return lens.model_copy(update={"dialect": "postgres"})


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def test_selects_declared_fields(lens):
    q = QueryRequest(fields=[{"field": "site_id"}, {"field": "event_count"}])
    sql, params = compile_query(lens, q)
    assert "site_id" in sql and "event_count" in sql
    assert params == {}


def test_aggregate_with_group_by(lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        aggregates=[{"function": "sum", "field": "amount_collected", "alias": "total"}],
        group_by=["site_id"],
    )
    sql, _ = compile_query(lens, q)
    assert "sum(" in sql.lower()
    assert "group by" in sql.lower()
    assert "total" in sql


def test_count_without_field_is_allowed(lens):
    q = QueryRequest(aggregates=[{"function": "count", "alias": "n"}])
    sql, _ = compile_query(lens, q)
    assert "count(*)" in sql.lower()


def test_count_distinct_renders_distinct(lens):
    q = QueryRequest(
        aggregates=[{"function": "count_distinct", "field": "site_id", "alias": "sites"}]
    )
    sql, _ = compile_query(lens, q)
    assert "distinct" in sql.lower()


def test_empty_projection_rejected():
    with pytest.raises(ValidationError):
        QueryRequest()


def test_field_mixed_with_aggregate_without_grouping_rejected():
    # Postgres errors on this; ClickHouse silently returns an arbitrary row.
    # Rejecting it keeps the two dialects behaving identically.
    with pytest.raises(ValidationError, match="group_by"):
        QueryRequest(
            fields=[{"field": "site_name"}],
            aggregates=[{"function": "count", "alias": "n"}],
        )


# --------------------------------------------------------------------------
# Unknown identifiers -- the whitelist boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_field",
    [
        "password",           # simply not declared
        "amount_collected2",  # near-miss on a real field
    ],
)
def test_undeclared_field_rejected(lens, bad_field):
    q = QueryRequest(fields=[{"field": bad_field}])
    with pytest.raises(UnknownFieldError):
        compile_query(lens, q)


def test_undeclared_group_by_rejected(lens):
    q = QueryRequest(
        aggregates=[{"function": "count", "alias": "n"}], group_by=["nope"]
    )
    with pytest.raises(UnknownFieldError):
        compile_query(lens, q)


def test_undeclared_order_by_rejected(lens):
    q = QueryRequest(fields=[{"field": "site_id"}], order_by=[{"field": "nope"}])
    with pytest.raises(UnknownFieldError):
        compile_query(lens, q)


def test_filter_on_field_marked_not_filterable_rejected(lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "internal_note", "op": "eq", "value": "x"}],
    )
    with pytest.raises(ValueError, match="not filterable"):
        compile_query(lens, q)


# --------------------------------------------------------------------------
# Injection attempts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "site_id; DROP TABLE users",
        "site_id UNION SELECT 1",
        "site_id--",
        "site_id/*c*/",
        "1=1",
        "site_id)",
        "*",
        "",
        " ",
        "site id",
        "'site_id'",
        "`site_id`",
        '"site_id"',
        "site_id\nDROP TABLE t",
    ],
)
def test_identifier_shaped_injection_rejected_at_validation(payload):
    """A field name that is really a SQL fragment never reaches the compiler.

    The pattern on the pydantic model rejects it, so this fails as a
    ValidationError rather than an UnknownFieldError.
    """
    with pytest.raises(ValidationError):
        QueryRequest(fields=[{"field": payload}])


@pytest.mark.parametrize(
    "payload",
    ["x' OR 1=1 --", "'; DROP TABLE t; --", "%", "_", "\\", "x\x00y"],
)
def test_hostile_filter_values_are_bound_not_inlined(lens, payload):
    """Values may contain anything; the comparison must bind, never inline.

    Asserting ``payload not in sql`` would be wrong for single-character
    payloads like "_", which legitimately occur inside identifiers such as
    ``site_name``. The real invariant is narrower and stronger: the right-hand
    side of the comparison is a placeholder, and the value is in the parameter
    mapping.
    """
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "site_name", "op": "eq", "value": payload}],
    )
    sql, params = compile_query(lens, q)

    assert "WHERE `site_name` = {p0:String}" in sql
    assert params == {"p0": payload}
    # No SQL string literal was emitted anywhere.
    assert "'" not in sql


def test_contains_escapes_like_wildcards(lens):
    """A user searching for "50%" must not match everything."""
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "site_name", "op": "contains", "value": "50%"}],
    )
    _, params = compile_query(lens, q)
    bound = next(iter(params.values()))
    assert "\\%" in bound


def test_alias_cannot_carry_sql(lens):
    with pytest.raises(ValidationError):
        QueryRequest(
            aggregates=[
                {"function": "sum", "field": "event_count", "alias": "x AS y, 1"}
            ]
        )


def test_unknown_aggregate_function_rejected():
    with pytest.raises(ValidationError):
        QueryRequest(aggregates=[{"function": "system", "alias": "x"}])


def test_unknown_operator_rejected():
    with pytest.raises(ValidationError):
        QueryRequest(
            fields=[{"field": "site_id"}],
            filters=[{"field": "site_id", "op": "; drop", "value": 1}],
        )


def test_unknown_sort_direction_rejected():
    with pytest.raises(ValidationError):
        QueryRequest(
            fields=[{"field": "site_id"}],
            order_by=[{"field": "site_id", "direction": "asc; drop table t"}],
        )


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------


def test_in_binds_every_element(lens):
    values = ["SITE_ALPHA", "SITE_BRAVO", "SITE_CHARLIE"]
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "site_id", "op": "in", "value": values}],
    )
    sql, params = compile_query(lens, q)
    assert "IN ({p0:String}, {p1:String}, {p2:String})" in sql
    assert list(params.values()) == values
    for v in values:
        assert v not in sql


def test_empty_in_list_rejected():
    with pytest.raises(ValidationError):
        QueryRequest(
            fields=[{"field": "site_id"}],
            filters=[{"field": "site_id", "op": "in", "value": []}],
        )


def test_between_requires_two_values():
    with pytest.raises(ValidationError):
        QueryRequest(
            fields=[{"field": "site_id"}],
            filters=[{"field": "usage_date", "op": "between", "value": ["2026-01-01"]}],
        )


def test_between_binds_both_bounds(lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[
            {
                "field": "usage_date",
                "op": "between",
                "value": ["2026-01-01", "2026-01-31"],
            }
        ],
    )
    _, params = compile_query(lens, q)
    assert len(params) == 2


def test_is_null_takes_no_parameter(lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "site_name", "op": "is_null"}],
    )
    sql, params = compile_query(lens, q)
    assert "is null" in sql.lower()
    assert params == {}


def test_is_null_with_a_value_rejected():
    with pytest.raises(ValidationError):
        QueryRequest(
            fields=[{"field": "site_id"}],
            filters=[{"field": "site_name", "op": "is_null", "value": "x"}],
        )


def test_multiple_filters_are_conjoined(lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[
            {"field": "site_id", "op": "eq", "value": "S1"},
            {"field": "channel", "op": "ne", "value": "CASH"},
        ],
    )
    sql, params = compile_query(lens, q)
    assert sql.lower().count(" and ") >= 1
    assert len(params) == 2


# --------------------------------------------------------------------------
# Dialects
# --------------------------------------------------------------------------


def test_clickhouse_uses_typed_placeholders(lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "event_count", "op": "gt", "value": 10}],
    )
    sql, _ = compile_query(lens, q)
    assert "{p0:Int64}" in sql


def test_postgres_uses_pyformat_placeholders(pg_lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "event_count", "op": "gt", "value": 10}],
    )
    sql, _ = compile_query(pg_lens, q)
    assert "%(p0)s" in sql


def test_dialects_quote_identifiers_differently(lens, pg_lens):
    q = QueryRequest(fields=[{"field": "site_id"}])
    ch_sql, _ = compile_query(lens, q)
    pg_sql, _ = compile_query(pg_lens, q)
    assert "`site_id`" in ch_sql
    assert '"site_id"' in pg_sql


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


def test_limit_is_always_applied(lens):
    q = QueryRequest(fields=[{"field": "site_id"}])
    sql, _ = compile_query(lens, q)
    assert "limit" in sql.lower()


def test_limit_is_an_integer_literal_not_a_parameter(lens):
    # LIMIT is validated as an int by pydantic; both engines dislike a bound
    # parameter there, so it is formatted directly. Guard that it stays an int.
    q = QueryRequest(fields=[{"field": "site_id"}], limit=42)
    sql, _ = compile_query(lens, q)
    assert "limit 42" in sql.lower()


@pytest.mark.parametrize("bad", [0, -1, 100_001])
def test_out_of_range_limit_rejected(bad):
    with pytest.raises(ValidationError):
        QueryRequest(fields=[{"field": "site_id"}], limit=bad)


def test_offset_emitted_only_when_nonzero(lens):
    without = compile_query(lens, QueryRequest(fields=[{"field": "site_id"}]))[0]
    with_off = compile_query(
        lens, QueryRequest(fields=[{"field": "site_id"}], offset=10)
    )[0]
    assert "offset" not in without.lower()
    assert "offset 10" in with_off.lower()


# --------------------------------------------------------------------------
# Determinism -- the cache key depends on it
# --------------------------------------------------------------------------


def test_compilation_is_deterministic(lens):
    q = QueryRequest(
        fields=[{"field": "site_id"}],
        filters=[{"field": "channel", "op": "in", "value": ["A", "B"]}],
        order_by=[{"field": "site_id", "direction": "desc"}],
    )
    assert compile_query(lens, q) == compile_query(lens, q)
