"""Tests for Pulse's arithmetic -- `services/pulse/pulse/engine.py`.

Pure functions, so no network and no database. These pin down the decisions
that make a KPI number trustworthy: ratios rolled up from sums, baselines that
exclude the point being judged, direction-aware anomaly flags, and refusing
configs that would produce a plausible but wrong number.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "libs" / "argus-core"))
sys.path.insert(0, str(REPO_ROOT / "services" / "pulse"))

from argus_core.config import load_kpis, load_lenses, validate_lens_references  # noqa: E402
from argus_core.schemas.kpi import KpiSpec  # noqa: E402
from pulse import engine  # noqa: E402

START = date(2026, 9, 1)


def kpi(**overrides) -> KpiSpec:
    spec = {
        "id": "leak",
        "name": "Leakage",
        "lens": "usage_trends",
        "date_field": "usage_date",
        "measure": {
            "function": "sum",
            "field": "amount_leaked",
            "denominator_function": "sum",
            "denominator_field": "amount_expected",
            "scale": 100,
        },
        "unit": "percent",
        "direction": "lower_is_better",
        "anomaly": {"window": 14, "z_threshold": 3.0, "min_history": 7},
    }
    spec.update(overrides)
    return KpiSpec.model_validate(spec)


def series(values: list[float], denominator: float | None = 100.0) -> list[engine.Point]:
    return [
        engine.Point(START + timedelta(days=i), v * (denominator or 1) / 100 if denominator else v, denominator)
        for i, v in enumerate(values)
    ]


# --------------------------------------------------------------------------
# Shipped config
# --------------------------------------------------------------------------


def test_shipped_kpis_validate_against_shipped_lenses():
    lenses = load_lenses(REPO_ROOT / "config" / "lenses")
    kpis = load_kpis(REPO_ROOT / "config" / "kpis")
    validate_lens_references(lenses, kpis)
    for spec in kpis.values():
        engine.check_spec(spec)
    assert "leakage_rate" in kpis


# --------------------------------------------------------------------------
# Query building
# --------------------------------------------------------------------------


def test_ratio_request_fetches_numerator_and_denominator_per_day():
    request = engine.build_request(kpi())
    assert [a.alias for a in request.aggregates] == ["num", "den"]
    assert request.group_by == ["usage_date"]


def test_breakdown_request_groups_by_the_dimension_too():
    request = engine.build_request(kpi(), "site_id")
    assert request.group_by == ["usage_date", "site_id"]


def test_monthly_average_is_refused_at_load_time():
    """Averaging daily averages gives a wrong number that looks right."""
    bad = kpi(grain="month", measure={"function": "avg", "field": "amount_leaked"})
    with pytest.raises(ValueError, match="cannot be rolled up"):
        engine.check_spec(bad)


# --------------------------------------------------------------------------
# Values and rollup
# --------------------------------------------------------------------------


def test_zero_denominator_is_undefined_not_zero():
    """No traffic is not '0% leakage'."""
    assert engine.Point(START, 0.0, 0.0).value(100) is None


def test_weekly_ratio_is_ratio_of_sums_not_mean_of_ratios():
    # A busy day at 10% and a quiet day at 50%.
    points = [
        engine.Point(date(2026, 9, 7), 100.0, 1000.0),  # Monday
        engine.Point(date(2026, 9, 8), 5.0, 10.0),  # Tuesday, same week
    ]
    week = engine.rollup(points, engine.Grain.WEEK)
    assert len(week) == 1
    assert week[0].value(100) == pytest.approx(105 / 1010 * 100)  # ~10.4%, not 30%


def test_rows_become_one_series_per_dimension_value():
    rows = [
        {"usage_date": "2026-09-02", "site_id": "A", "num": 2, "den": 10},
        {"usage_date": "2026-09-01", "site_id": "A", "num": 1, "den": 10},
        {"usage_date": "2026-09-01", "site_id": "B", "num": 5, "den": 10},
    ]
    out = engine.to_points(kpi(), rows, "site_id")
    assert set(out) == {"A", "B"}
    assert [p.period.day for p in out["A"]] == [1, 2]  # sorted


# --------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------

STEADY = [10.0, 10.2, 9.9, 10.1, 9.8, 10.0, 10.3, 9.9, 10.1, 10.0]


def test_spike_in_bad_direction_is_flagged_with_a_plain_reason():
    found = engine.detect_anomalies(kpi(), series(STEADY + [14.0]))
    assert len(found) == 1
    assert found[0].period == START + timedelta(days=10)
    assert "above its 10-period average" in found[0].reason


def test_improvement_is_not_an_anomaly_for_lower_is_better():
    assert engine.detect_anomalies(kpi(), series(STEADY + [6.0])) == []


def test_neutral_kpi_flags_both_directions():
    neutral = kpi(direction="neutral")
    assert engine.detect_anomalies(neutral, series(STEADY + [6.0]))
    assert engine.detect_anomalies(neutral, series(STEADY + [14.0]))


def test_baseline_excludes_the_point_being_judged():
    """With the point inside its own baseline the z-score would shrink."""
    found = engine.detect_anomalies(kpi(), series(STEADY + [14.0]))
    assert found[0].expected == pytest.approx(sum(STEADY) / len(STEADY))


def test_too_little_history_flags_nothing():
    assert engine.detect_anomalies(kpi(), series(STEADY[:5] + [50.0])) == []


def test_a_sustained_fault_contaminates_its_own_baseline():
    """Documents a known limit: day 3 of a fault is judged against days 1-2.

    The rule engine (Sentry) covers this with fixed thresholds per subject;
    this test exists so the limitation is visible, not hidden.
    """
    found = engine.detect_anomalies(kpi(), series(STEADY + [14.0, 14.0, 14.0, 14.0]))
    assert 1 <= len(found) < 4


# --------------------------------------------------------------------------
# Forecast and status
# --------------------------------------------------------------------------


def test_forecast_repeats_the_weekly_pattern_with_a_band():
    weekly = [10, 10, 10, 10, 10, 20, 20] * 4  # weekends double
    points = series([float(v) for v in weekly])
    out = engine.forecast(kpi(direction="neutral"), points)
    assert len(out) == 7
    assert [round(f.value) for f in out] == [10, 10, 10, 10, 10, 20, 20]
    assert all(f.lower <= f.value <= f.upper for f in out)


def test_status_respects_direction():
    lower = kpi(thresholds={"warning": 5, "critical": 10})
    assert engine.status(lower, 4) == "ok"
    assert engine.status(lower, 6) == "warning"
    assert engine.status(lower, 11) == "critical"
    higher = kpi(direction="higher_is_better", thresholds={"warning": 90, "critical": 80})
    assert engine.status(higher, 95) == "ok"
    assert engine.status(higher, 85) == "warning"
    assert engine.status(higher, 70) == "critical"
    assert engine.status(lower, None) == "unknown"


def test_forecast_band_never_goes_below_zero_for_a_non_negative_measure():
    """A rate with one big spike gets a wide band -- but not a negative rate."""
    values = [2.0, 2.1, 1.9, 2.0, 2.2, 2.0, 1.8] * 3 + [9.0, 8.5, 2.0, 2.1, 1.9, 2.0, 2.0]
    out = engine.forecast(kpi(direction="neutral"), series(values))
    assert out and all(f.lower >= 0 for f in out)
