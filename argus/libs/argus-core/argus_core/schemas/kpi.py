"""KPI specification -- the YAML contract in `config/kpis/`.

A KPI names a prism lens, one measure over it, a time grain, and the
dimensions it should be broken down by. The engine never contains KPI-specific
code: adding a KPI is adding a file.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .query import IDENTIFIER_PATTERN, AggregateFunction


class Grain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class Direction(StrEnum):
    """Which way is bad -- drives breach detection and narrative wording."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    NEUTRAL = "neutral"


class Measure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function: AggregateFunction
    field: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    denominator_function: AggregateFunction | None = None
    denominator_field: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    scale: float = 1.0

    @property
    def is_ratio(self) -> bool:
        return self.denominator_function is not None

    @model_validator(mode="after")
    def _ratio_needs_both_halves(self) -> Measure:
        if self.denominator_function is not None and self.denominator_field is None:
            if self.denominator_function is not AggregateFunction.COUNT:
                raise ValueError("denominator_function requires denominator_field")
        return self


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warning: float | None = None
    critical: float | None = None
    max_change_pct: float | None = Field(default=None, gt=0)


class AnomalyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    window: int = Field(default=30, ge=7, le=365)
    z_threshold: float = Field(default=3.0, gt=0)
    min_history: int = Field(default=14, ge=3)


class ForecastConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    horizon: int = Field(default=7, ge=1, le=90)
    season_length: int = Field(default=7, ge=2)
    window: int = Field(default=28, ge=7)


class KpiSpec(BaseModel):
    """One `config/kpis/<id>.yaml` file."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    name: str
    description: str = ""
    lens: str = Field(pattern=IDENTIFIER_PATTERN)
    date_field: str = Field(pattern=IDENTIFIER_PATTERN)
    measure: Measure
    grain: Grain = Grain.DAY
    unit: str = "count"
    direction: Direction = Direction.NEUTRAL
    breakdowns: list[str] = Field(default_factory=list)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _breakdowns_are_identifiers(self) -> KpiSpec:
        import re

        for name in self.breakdowns:
            if not re.match(IDENTIFIER_PATTERN, name):
                raise ValueError(f"invalid breakdown identifier: {name!r}")
        return self
