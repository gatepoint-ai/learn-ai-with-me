"""Compile a validated `QueryRequest` against a `LensSpec` into SQL.

This is the only place in the platform where caller input becomes SQL, so the
rules are absolute and stated once here:

**Identifiers** never come from the request. Each name is looked up in the
lens's declared fields via `LensSpec.resolve`, and the *resolved* field's
name is what gets emitted. A name that is not declared raises; a name that is
declared is by construction a bare identifier, because `FieldSpec.field` is
pattern-validated. Quoting is applied on top as defence in depth.

**Values** are never emitted. Every literal becomes a bound parameter in the
returned mapping. The one exception is `LIMIT`/`OFFSET`, which both engines
refuse as bound parameters — those are pydantic-validated integers and are
formatted with `%d`, so they cannot carry text.

**Functions and operators** come from closed enums, so there is no path from a
request string to a SQL keyword.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from .schemas.lens import DataType, Dialect, LensSpec, FieldSpec
from .schemas.query import (
    AggregateFunction,
    AggregateSelection,
    Filter,
    FilterOperator,
    QueryRequest,
    SortDirection,
)

__all__ = ["compile_query", "CompiledQuery"]

CompiledQuery = tuple[str, dict[str, Any]]

#: Aggregate function -> SQL template. Both dialects share this spelling.
_AGGREGATE_SQL: dict[AggregateFunction, str] = {
    AggregateFunction.SUM: "sum({col})",
    AggregateFunction.COUNT: "count({col})",
    AggregateFunction.COUNT_DISTINCT: "count(DISTINCT {col})",
    AggregateFunction.AVG: "avg({col})",
    AggregateFunction.MIN: "min({col})",
    AggregateFunction.MAX: "max({col})",
}

#: Comparison operators that map to a single binary SQL operator.
_BINARY_SQL: dict[FilterOperator, str] = {
    FilterOperator.EQ: "=",
    FilterOperator.NE: "!=",
    FilterOperator.GT: ">",
    FilterOperator.GTE: ">=",
    FilterOperator.LT: "<",
    FilterOperator.LTE: "<=",
}

_DIRECTION_SQL: dict[SortDirection, str] = {
    SortDirection.ASC: "ASC",
    SortDirection.DESC: "DESC",
}


class _Dialect(Protocol):
    """The two things that differ between ClickHouse and Postgres."""

    def quote(self, identifier: str) -> str: ...

    def placeholder(self, name: str, value: Any, declared: DataType | None) -> str: ...


class _ClickHouse:
    """ClickHouse server-side parameter binding: ``{name:Type}``.

    The type is mandatory in that syntax, so it is derived from the lens's
    declared `data_type` where available and from the Python value otherwise.
    """

    _TYPE_BY_DATATYPE: dict[DataType, str] = {
        DataType.STRING: "String",
        DataType.INTEGER: "Int64",
        DataType.FLOAT: "Float64",
        DataType.DECIMAL: "Float64",
        DataType.BOOLEAN: "Bool",
        DataType.DATE: "Date",
        DataType.DATETIME: "DateTime",
    }

    def quote(self, identifier: str) -> str:
        return f"`{identifier}`"

    def placeholder(self, name: str, value: Any, declared: DataType | None) -> str:
        return f"{{{name}:{self._ch_type(value, declared)}}}"

    def _ch_type(self, value: Any, declared: DataType | None) -> str:
        # A declared Date column filtered with a string still wants Date, so the
        # declaration wins whenever the value is a plain scalar.
        if declared is not None and not isinstance(value, bool):
            return self._TYPE_BY_DATATYPE[declared]
        if isinstance(value, bool):
            return "Bool"
        if isinstance(value, int):
            return "Int64"
        if isinstance(value, (float, Decimal)):
            return "Float64"
        if isinstance(value, datetime):
            return "DateTime"
        if isinstance(value, date):
            return "Date"
        return "String"


class _Postgres:
    """psycopg pyformat binding: ``%(name)s``."""

    def quote(self, identifier: str) -> str:
        return f'"{identifier}"'

    def placeholder(self, name: str, value: Any, declared: DataType | None) -> str:
        return f"%({name})s"


_DIALECTS: dict[Dialect, _Dialect] = {
    Dialect.CLICKHOUSE: _ClickHouse(),
    Dialect.POSTGRES: _Postgres(),
}


class _ParamBag:
    """Accumulates bound parameters and hands out stable placeholder names.

    Names are positional (``p0``, ``p1``, ...) so that compiling the same
    request twice produces byte-identical SQL. The prism cache keys on that.
    """

    def __init__(self, dialect: _Dialect) -> None:
        self._dialect = dialect
        self._values: dict[str, Any] = {}

    def bind(self, value: Any, declared: DataType | None) -> str:
        name = f"p{len(self._values)}"
        self._values[name] = value
        return self._dialect.placeholder(name, value, declared)

    @property
    def values(self) -> dict[str, Any]:
        return self._values


def compile_query(lens: LensSpec, request: QueryRequest) -> CompiledQuery:
    """Compile `request` against `lens`.

    Returns the SQL text and the parameter mapping to pass to the driver.

    Raises:
        UnknownFieldError: a referenced field is not declared by the lens.
        ValueError: a field is declared but not permitted for that use.
    """
    dialect = _DIALECTS[lens.dialect]
    params = _ParamBag(dialect)

    projection = _build_projection(lens, request, dialect)
    where = _build_where(lens, request.filters, dialect, params)
    group_by = _build_group_by(lens, request, dialect)
    order_by = _build_order_by(lens, request, dialect)

    # The lens SQL is wrapped rather than edited. Callers therefore cannot
    # reach past the lens's own projection, and a lens can be as complex as
    # it likes (CTEs, joins) without the compiler needing to understand it.
    parts = [
        f"SELECT {', '.join(projection)}",
        f"FROM ({lens.sql}) AS d",
    ]
    if where:
        parts.append(f"WHERE {where}")
    if group_by:
        parts.append(f"GROUP BY {', '.join(group_by)}")
    if order_by:
        parts.append(f"ORDER BY {', '.join(order_by)}")
    # Validated ints, formatted with %d -- they cannot carry text.
    parts.append("LIMIT %d" % request.limit)
    if request.offset:
        parts.append("OFFSET %d" % request.offset)

    return "\n".join(parts), params.values


# --------------------------------------------------------------------------
# Clause builders
# --------------------------------------------------------------------------


def _build_projection(
    lens: LensSpec, request: QueryRequest, dialect: _Dialect
) -> list[str]:
    out: list[str] = []

    for selection in request.fields:
        spec = lens.resolve(selection.field)
        col = dialect.quote(spec.field)
        if selection.alias and selection.alias != spec.field:
            out.append(f"{col} AS {dialect.quote(selection.alias)}")
        else:
            out.append(col)

    for aggregate in request.aggregates:
        out.append(_build_aggregate(lens, aggregate, dialect))

    return out


def _build_aggregate(
    lens: LensSpec, aggregate: AggregateSelection, dialect: _Dialect
) -> str:
    if aggregate.field is None:
        # Only count() reaches here; the schema enforces that.
        col = "*"
    else:
        spec = lens.resolve(aggregate.field)
        _reject_non_numeric_aggregate(aggregate.function, spec)
        col = dialect.quote(spec.field)

    body = _AGGREGATE_SQL[aggregate.function].format(col=col)
    return f"{body} AS {dialect.quote(aggregate.alias)}"


def _reject_non_numeric_aggregate(
    function: AggregateFunction, spec: FieldSpec
) -> None:
    """Fail fast on sum/avg over a string.

    ClickHouse raises a type error and Postgres raises a different one; both
    surface as an opaque 500 from the driver. Catching it here gives the caller
    a message naming the field.
    """
    numeric_only = {AggregateFunction.SUM, AggregateFunction.AVG}
    if function in numeric_only and not spec.data_type.is_numeric:
        raise ValueError(
            f"cannot apply {function} to non-numeric field {spec.field!r} "
            f"(declared {spec.data_type})"
        )


def _build_where(
    lens: LensSpec,
    filters: list[Filter],
    dialect: _Dialect,
    params: _ParamBag,
) -> str:
    clauses = [_build_filter(lens, f, dialect, params) for f in filters]
    return " AND ".join(clauses)


def _build_filter(
    lens: LensSpec, flt: Filter, dialect: _Dialect, params: _ParamBag
) -> str:
    spec = lens.resolve(flt.field)
    if not spec.allow_filter:
        raise ValueError(f"field {spec.field!r} is not filterable in lens {lens.name!r}")

    col = dialect.quote(spec.field)
    dt = spec.data_type

    if flt.op is FilterOperator.IS_NULL:
        return f"{col} IS NULL"
    if flt.op is FilterOperator.IS_NOT_NULL:
        return f"{col} IS NOT NULL"

    if flt.op in _BINARY_SQL:
        return f"{col} {_BINARY_SQL[flt.op]} {params.bind(flt.value, dt)}"

    if flt.op in (FilterOperator.IN, FilterOperator.NOT_IN):
        placeholders = ", ".join(params.bind(v, dt) for v in flt.value)
        keyword = "IN" if flt.op is FilterOperator.IN else "NOT IN"
        return f"{col} {keyword} ({placeholders})"

    if flt.op is FilterOperator.BETWEEN:
        low, high = flt.value
        return f"{col} BETWEEN {params.bind(low, dt)} AND {params.bind(high, dt)}"

    if flt.op in (FilterOperator.CONTAINS, FilterOperator.STARTS_WITH):
        pattern = _like_pattern(flt.op, flt.value)
        # Both engines default to backslash as the LIKE escape character.
        return f"{col} LIKE {params.bind(pattern, DataType.STRING)}"

    raise AssertionError(f"unhandled operator {flt.op}")  # pragma: no cover


def _like_pattern(op: FilterOperator, value: Any) -> str:
    """Build a LIKE pattern, escaping wildcards the user typed literally.

    Without this, searching for "50%" matches every row beginning "50", and
    searching for "_" matches everything.
    """
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%" if op is FilterOperator.CONTAINS else f"{escaped}%"


def _build_group_by(
    lens: LensSpec, request: QueryRequest, dialect: _Dialect
) -> list[str]:
    out: list[str] = []
    for name in request.group_by:
        spec = lens.resolve(name)
        if not spec.allow_group_by:
            raise ValueError(
                f"field {spec.field!r} is not groupable in lens {lens.name!r}"
            )
        out.append(dialect.quote(spec.field))
    return out


def _build_order_by(
    lens: LensSpec, request: QueryRequest, dialect: _Dialect
) -> list[str]:
    # An aggregate alias is a legitimate sort target ("top sites by revenue"),
    # so aliases introduced by this query are resolvable alongside real fields.
    aliases = {a.alias for a in request.aggregates} | {
        f.alias for f in request.fields if f.alias
    }
    out: list[str] = []
    for sort in request.order_by:
        if sort.field in aliases:
            col = dialect.quote(sort.field)
        else:
            col = dialect.quote(lens.resolve(sort.field).field)
        out.append(f"{col} {_DIRECTION_SQL[sort.direction]}")
    return out
