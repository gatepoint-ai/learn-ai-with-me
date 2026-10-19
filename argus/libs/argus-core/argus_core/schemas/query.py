"""Query request contract — the input the SQL compiler accepts.

This module deliberately contains no SQL. Everything here is a *validated
description* of a query; turning it into SQL is `argus_core.sqlbuilder`'s job. The
split matters: if a value cannot be expressed in these models, it cannot reach
the compiler at all.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Identifiers are matched against a lens's declared fields, so this pattern is
# defence in depth rather than the primary control. It still runs first.
IDENTIFIER_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$"


class AggregateFunction(StrEnum):
    """The complete set of aggregates a caller may request.

    A closed enum, not a string passed through to SQL. Adding a function is a
    deliberate code change with a test, which is the point.
    """

    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class FilterOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


#: Operators that take no value at all.
NULLARY_OPERATORS = frozenset({FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL})
#: Operators whose value must be a non-empty sequence.
SEQUENCE_OPERATORS = frozenset({FilterOperator.IN, FilterOperator.NOT_IN})


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class FieldSelection(BaseModel):
    """A plain (non-aggregated) column in the projection."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(pattern=IDENTIFIER_PATTERN)
    alias: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)

    @property
    def output_name(self) -> str:
        return self.alias or self.field


class AggregateSelection(BaseModel):
    """An aggregate in the projection, e.g. ``sum(amount_collected) AS total``."""

    model_config = ConfigDict(extra="forbid")

    function: AggregateFunction
    field: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    alias: str = Field(pattern=IDENTIFIER_PATTERN)

    @model_validator(mode="after")
    def _count_may_omit_field(self) -> AggregateSelection:
        # count() is the only aggregate that is meaningful without a column.
        if self.function is not AggregateFunction.COUNT and self.field is None:
            raise ValueError(f"aggregate {self.function} requires a field")
        return self

    @property
    def output_name(self) -> str:
        return self.alias


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(pattern=IDENTIFIER_PATTERN)
    op: FilterOperator = FilterOperator.EQ
    value: Any = None

    @model_validator(mode="after")
    def _value_matches_operator(self) -> Filter:
        if self.op in NULLARY_OPERATORS:
            if self.value is not None:
                raise ValueError(f"operator {self.op} takes no value")
            return self

        if self.value is None:
            raise ValueError(f"operator {self.op} requires a value")

        if self.op in SEQUENCE_OPERATORS:
            if not isinstance(self.value, (list, tuple)) or not self.value:
                raise ValueError(f"operator {self.op} requires a non-empty list")
            if len(self.value) > 1000:
                raise ValueError(f"operator {self.op} accepts at most 1000 values")

        if self.op is FilterOperator.BETWEEN:
            if not isinstance(self.value, (list, tuple)) or len(self.value) != 2:
                raise ValueError("operator between requires exactly two values")

        return self


class SortSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(pattern=IDENTIFIER_PATTERN)
    direction: SortDirection = SortDirection.ASC


class QueryRequest(BaseModel):
    """A complete, validated query against one prism lens."""

    model_config = ConfigDict(extra="forbid")

    fields: list[FieldSelection] = Field(default_factory=list)
    aggregates: list[AggregateSelection] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[SortSpec] = Field(default_factory=list)
    limit: int = Field(default=1000, ge=1, le=100_000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _projection_not_empty(self) -> QueryRequest:
        if not self.fields and not self.aggregates:
            raise ValueError("query must select at least one field or aggregate")
        return self

    @model_validator(mode="after")
    def _group_by_identifiers_valid(self) -> QueryRequest:
        import re

        for name in self.group_by:
            if not re.match(IDENTIFIER_PATTERN, name):
                raise ValueError(f"invalid group_by identifier: {name!r}")
        return self

    @model_validator(mode="after")
    def _aggregates_require_grouping(self) -> QueryRequest:
        """Reject the mixed projection that silently returns nonsense.

        Selecting a plain field alongside an aggregate without grouping by that
        field is an error in Postgres and, worse, returns an arbitrary row in
        ClickHouse. Catch it here so both dialects behave the same.
        """
        if not self.aggregates:
            return self
        grouped = set(self.group_by)
        ungrouped = [f.field for f in self.fields if f.field not in grouped]
        if ungrouped:
            raise ValueError(
                "fields mixed with aggregates must appear in group_by: "
                + ", ".join(sorted(ungrouped))
            )
        return self
