"""Pulse's arithmetic -- pure functions, no network, no database.

Everything a KPI number depends on lives here, so the whole of it is unit
tested without a running stack:

* turning a `KpiSpec` into the Prism query that fetches its inputs,
* turning Prism rows into a daily series of (numerator, denominator) points,
* rolling daily points up to a week or a month,
* flagging anomalies against each point's own trailing history,
* a weekly-seasonal forecast with a confidence band.

The rule the platform is built on applies here first: **numbers are computed,
never generated.** Nothing in this module talks to a language model, and the
Analyst only ever reads what this module wrote.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from argus_core.schemas.kpi import Direction, Grain, KpiSpec
from argus_core.schemas.query import AggregateFunction, QueryRequest

#: Aggregates whose daily values can be added up to a week or a month. An
#: average or a distinct count cannot, so those KPIs are day-grain only.
ADDITIVE = frozenset({AggregateFunction.SUM, AggregateFunction.COUNT})

NUMERATOR = "num"
DENOMINATOR = "den"


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    period: date
    numerator: float
    denominator: float | None  # None for plain (non-ratio) measures

    def value(self, scale: float) -> float | None:
        if self.denominator is None:
            return self.numerator * scale
        if self.denominator == 0:
            return None  # undefined, not zero: no traffic is not "0% leakage"
        return self.numerator / self.denominator * scale


@dataclass(frozen=True)
class Anomaly:
    period: date
    value: float
    expected: float
    deviation: float
    z_score: float
    severity: str
    reason: str


@dataclass(frozen=True)
class ForecastPoint:
    period: date
    value: float
    lower: float
    upper: float


# --------------------------------------------------------------------------
# 1. Query building
# --------------------------------------------------------------------------


def check_spec(kpi: KpiSpec) -> None:
    """Reject a KPI the engine cannot compute correctly.

    Rolling an average up to a month by averaging daily averages gives a wrong
    number that looks right. Better to refuse the config at startup.
    """
    if kpi.grain is Grain.DAY:
        return
    functions = [kpi.measure.function]
    if kpi.measure.denominator_function is not None:
        functions.append(kpi.measure.denominator_function)
    bad = [f.value for f in functions if f not in ADDITIVE]
    if bad:
        raise ValueError(
            f"kpi {kpi.id}: grain {kpi.grain.value} needs additive measures; "
            f"{', '.join(bad)} cannot be rolled up"
        )


def build_request(kpi: KpiSpec, dimension: str | None = None) -> QueryRequest:
    """The Prism query for one KPI: numerator (and denominator) per day.

    Always fetched at day grain; `rollup` handles weeks and months, so the
    lens never needs date functions and the SQL compiler stays closed.
    """
    group = [kpi.date_field] + ([dimension] if dimension else [])
    aggregates: list[dict[str, Any]] = [
        _aggregate(kpi.measure.function, kpi.measure.field, NUMERATOR)
    ]
    if kpi.measure.is_ratio:
        aggregates.append(
            _aggregate(
                kpi.measure.denominator_function,
                kpi.measure.denominator_field,
                DENOMINATOR,
            )
        )
    return QueryRequest(
        fields=[{"field": f} for f in group],
        aggregates=aggregates,
        group_by=group,
        order_by=[{"field": kpi.date_field, "direction": "asc"}],
        limit=100_000,
    )


def _aggregate(function: AggregateFunction, field: str | None, alias: str) -> dict[str, Any]:
    spec: dict[str, Any] = {"function": function.value, "alias": alias}
    if field is not None:
        spec["field"] = field
    return spec


# --------------------------------------------------------------------------
# 2. Rows -> points
# --------------------------------------------------------------------------


def to_points(
    kpi: KpiSpec, rows: list[dict[str, Any]], dimension: str | None = None
) -> dict[str | None, list[Point]]:
    """Group Prism rows into one daily series per dimension value.

    Without a dimension the result has a single key, ``None``.
    """
    series: dict[str | None, list[Point]] = {}
    for row in rows:
        key = None if dimension is None else str(row[dimension])
        series.setdefault(key, []).append(
            Point(
                period=_as_date(row[kpi.date_field]),
                numerator=float(row[NUMERATOR] or 0),
                denominator=(
                    float(row.get(DENOMINATOR) or 0) if kpi.measure.is_ratio else None
                ),
            )
        )
    for points in series.values():
        points.sort(key=lambda p: p.period)
    return series


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def rollup(points: list[Point], grain: Grain) -> list[Point]:
    """Sum daily numerators and denominators into weeks or months.

    A weekly ratio is sum(num) / sum(den) for the week -- never the mean of
    seven daily ratios, which over-weights quiet days.
    """
    if grain is Grain.DAY:
        return points
    buckets: dict[date, list[Point]] = {}
    for p in points:
        start = (
            p.period - timedelta(days=p.period.weekday())
            if grain is Grain.WEEK
            else p.period.replace(day=1)
        )
        buckets.setdefault(start, []).append(p)
    out = []
    for start in sorted(buckets):
        group = buckets[start]
        dens = [p.denominator for p in group]
        out.append(
            Point(
                period=start,
                numerator=sum(p.numerator for p in group),
                denominator=None if dens[0] is None else sum(d or 0 for d in dens),
            )
        )
    return out


# --------------------------------------------------------------------------
# 3. Anomalies
# --------------------------------------------------------------------------


def detect_anomalies(kpi: KpiSpec, points: list[Point]) -> list[Anomaly]:
    """Flag points far from their own trailing history.

    The baseline for a point is the `window` points *before* it -- never
    including it. A large anomaly included in its own baseline drags the mean
    toward itself and hides the very breach being looked for.

    Direction matters: for a lower-is-better KPI only rises are flagged, for
    higher-is-better only falls. A neutral KPI is flagged both ways.
    """
    cfg = kpi.anomaly
    if not cfg.enabled:
        return []
    values = [(p.period, p.value(kpi.measure.scale)) for p in points]
    found: list[Anomaly] = []
    for i, (period, value) in enumerate(values):
        if value is None:
            continue
        history = [v for _, v in values[max(0, i - cfg.window) : i] if v is not None]
        if len(history) < cfg.min_history:
            continue
        mean = statistics.fmean(history)
        stdev = statistics.pstdev(history)
        if stdev == 0:
            continue
        z = (value - mean) / stdev
        if not _is_bad_direction(kpi.direction, z) or abs(z) < cfg.z_threshold:
            continue
        found.append(
            Anomaly(
                period=period,
                value=value,
                expected=mean,
                deviation=value - mean,
                z_score=z,
                severity=_severity(abs(z), cfg.z_threshold),
                reason=_reason(kpi, value, mean, z, len(history)),
            )
        )
    return found


def _is_bad_direction(direction: Direction, z: float) -> bool:
    if direction is Direction.LOWER_IS_BETTER:
        return z > 0
    if direction is Direction.HIGHER_IS_BETTER:
        return z < 0
    return True


def _severity(abs_z: float, threshold: float) -> str:
    if abs_z >= threshold * 2:
        return "critical"
    if abs_z >= threshold * 1.5:
        return "high"
    return "medium"


def _reason(kpi: KpiSpec, value: float, mean: float, z: float, n: int) -> str:
    """A deterministic sentence, so the alert is explainable with no LLM at all."""
    word = "above" if z > 0 else "below"
    return (
        f"{kpi.name} was {format_value(value, kpi.unit)}, "
        f"{abs(z):.1f} standard deviations {word} its {n}-period average of "
        f"{format_value(mean, kpi.unit)}."
    )


def format_value(value: float, unit: str) -> str:
    if unit == "percent":
        return f"{value:.2f}%"
    if unit == "currency":
        return f"{value:,.2f}"
    if abs(value - round(value)) < 1e-9:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


# --------------------------------------------------------------------------
# 4. Forecast
# --------------------------------------------------------------------------


def forecast(kpi: KpiSpec, points: list[Point]) -> list[ForecastPoint]:
    """Seasonal-naive forecast with an empirical confidence band.

    Each future period is the mean of the same position in the last few
    seasons (for daily data with season 7: the last few Mondays predict next
    Monday). The band is +/- 1.96 standard deviations of the in-sample errors
    of that same method. Deliberately simple: explainable in one sentence, and
    hard to be confidently wrong with.
    """
    cfg = kpi.forecast
    if not cfg.enabled:
        return []
    history = [(p.period, p.value(kpi.measure.scale)) for p in points]
    history = [(d, v) for d, v in history if v is not None][-cfg.window :]
    season = cfg.season_length if kpi.grain is Grain.DAY else 1
    if len(history) < season * 2:
        return []

    values = [v for _, v in history]

    def predict(index: int) -> float:
        same_slot = [values[j] for j in range(index - season, -1, -season)][:4]
        return statistics.fmean(same_slot)

    errors = [values[i] - predict(i) for i in range(season, len(values))]
    spread = 1.96 * (statistics.pstdev(errors) if len(errors) > 1 else 0.0)

    # A rate, a count or an amount that has never been negative cannot go
    # negative next week either; the band is clipped rather than drawn below 0.
    floor = 0.0 if min(values) >= 0 else None

    step = _step(kpi.grain)
    last = history[-1][0]
    out: list[ForecastPoint] = []
    extended = list(values)
    for h in range(1, cfg.horizon + 1):
        value = predict(len(extended))
        extended.append(value)
        widen = math.sqrt(h)  # uncertainty grows with distance
        out.append(
            ForecastPoint(
                period=_advance(last, step, h),
                value=value,
                lower=max(floor, value - spread * widen) if floor is not None else value - spread * widen,
                upper=value + spread * widen,
            )
        )
    return out


def _step(grain: Grain) -> str:
    return grain.value


def _advance(start: date, grain: str, n: int) -> date:
    if grain == "day":
        return start + timedelta(days=n)
    if grain == "week":
        return start + timedelta(weeks=n)
    month = start.month - 1 + n
    return start.replace(year=start.year + month // 12, month=month % 12 + 1, day=1)


# --------------------------------------------------------------------------
# 5. Status against thresholds
# --------------------------------------------------------------------------


def status(kpi: KpiSpec, value: float | None) -> str:
    """ok / warning / critical for the latest value, respecting direction."""
    if value is None:
        return "unknown"
    t = kpi.thresholds
    if t.warning is None and t.critical is None:
        return "ok"
    worse = (lambda a, b: a >= b) if kpi.direction is not Direction.HIGHER_IS_BETTER else (
        lambda a, b: a <= b
    )
    if t.critical is not None and worse(value, t.critical):
        return "critical"
    if t.warning is not None and worse(value, t.warning):
        return "warning"
    return "ok"
