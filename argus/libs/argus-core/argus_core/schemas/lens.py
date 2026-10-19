"""Prism lens specification — the YAML contract in `config/lenses/`.

A lens is a named, read-only SQL view over a datasource plus a *closed list of
fields* callers may touch. That field list is the security boundary: the SQL
compiler resolves every identifier through it, so a column the lens does not
declare cannot be selected, filtered, grouped, or sorted.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .query import IDENTIFIER_PATTERN


class Dialect(StrEnum):
    CLICKHOUSE = "clickhouse"
    POSTGRES = "postgres"


class DataType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"

    @property
    def is_numeric(self) -> bool:
        return self in {DataType.INTEGER, DataType.FLOAT, DataType.DECIMAL}


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(pattern=IDENTIFIER_PATTERN)
    data_type: DataType
    label: str | None = None
    description: str | None = None
    allow_filter: bool = True
    allow_group_by: bool = True

    @model_validator(mode="after")
    def _default_label(self) -> FieldSpec:
        if self.label is None:
            object.__setattr__(self, "label", self.field.replace("_", " ").title())
        return self


class LensSpec(BaseModel):
    """One `config/lenses/<name>.yaml` file."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=IDENTIFIER_PATTERN)
    label: str
    description: str = ""
    datasource: str = "analytics"
    dialect: Dialect = Dialect.CLICKHOUSE
    sql: str
    fields: list[FieldSpec]
    tags: list[str] = Field(default_factory=list)
    allow_cache: bool = True
    cache_life: int = Field(default=1800, ge=0, le=86_400)
    default_limit: int = Field(default=1000, ge=1, le=100_000)

    @model_validator(mode="after")
    def _fields_unique(self) -> LensSpec:
        seen: set[str] = set()
        for spec in self.fields:
            if spec.field in seen:
                raise ValueError(f"duplicate field in lens {self.name}: {spec.field}")
            seen.add(spec.field)
        if not self.fields:
            raise ValueError(f"lens {self.name} declares no fields")
        return self

    @model_validator(mode="after")
    def _sql_is_a_single_read(self) -> LensSpec:
        """Reject anything but a single SELECT.

        Lens SQL is authored by us, not by callers, so this is a guard against
        an editing mistake rather than an attack — but a stray semicolon in a
        lens file would become a second statement wrapped in every query, so
        it is worth failing at load time instead of query time.
        """
        body = self.sql.strip().rstrip(";").strip()
        if not body:
            raise ValueError(f"lens {self.name} has empty sql")
        lowered = body.lower()
        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise ValueError(f"lens {self.name} sql must be a SELECT or WITH query")
        if ";" in body:
            raise ValueError(f"lens {self.name} sql must be a single statement")
        object.__setattr__(self, "sql", body)
        return self

    # -- lookup helpers used by the compiler ------------------------------------

    @property
    def field_map(self) -> dict[str, FieldSpec]:
        return {spec.field: spec for spec in self.fields}

    def resolve(self, name: str) -> FieldSpec:
        """Return the declared field, or raise ``UnknownFieldError``.

        Every identifier that reaches SQL passes through here.
        """
        try:
            return self.field_map[name]
        except KeyError:
            raise UnknownFieldError(self.name, name, sorted(self.field_map)) from None


class UnknownFieldError(ValueError):
    """Raised when a query references a field the lens does not declare."""

    def __init__(self, lens: str, field: str, known: list[str]) -> None:
        self.lens = lens
        self.field = field
        self.known = known
        super().__init__(
            f"lens {lens!r} has no field {field!r}; known fields: {', '.join(known)}"
        )
