"""Tests for Sentry's judgement -- `services/sentry/sentry/engine.py`.

Pure functions: no network, no database. These pin down the decisions the
acceptance gate depends on -- which rows breach, when a breach is really one
systemic event, and that the alert says why in plain numbers.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "libs" / "argus-core"))
sys.path.insert(0, str(REPO_ROOT / "services" / "sentry"))

from argus_core.config import (  # noqa: E402
    ConfigError,
    load_kpis,
    load_lenses,
    load_rules,
    validate_lens_references,
)
from argus_core.schemas.rules import Comparator, Probe, RuleSet  # noqa: E402
from sentry import engine  # noqa: E402

DAY = date(2026, 10, 4)


def probe(**overrides) -> Probe:
    spec = {
        "id": "PR-TEST",
        "name": "Feed completeness",
        "lens": "site_volume_baseline",
        "query": {"fields": [{"field": f} for f in
                             ("usage_date", "site_id", "volume_change_ratio", "event_count")]},
        "value_field": "volume_change_ratio",
        "subject_field": "site_id",
        "subject_type": "site",
        "comparator": "lt",
        "threshold": -0.6,
        "alert_type": 1,
        "evidence_fields": ["event_count"],
    }
    spec.update(overrides)
    return Probe.model_validate(spec)


def row(site: str, ratio: float, day: date = DAY) -> dict:
    return {"usage_date": day.isoformat(), "site_id": site,
            "volume_change_ratio": ratio, "event_count": 100}


# --------------------------------------------------------------------------
# Shipped config
# --------------------------------------------------------------------------


def test_shipped_rules_validate_against_shipped_lenses():
    lenses = load_lenses(REPO_ROOT / "config" / "lenses")
    rules = load_rules(REPO_ROOT / "config" / "rules")
    validate_lens_references(lenses, load_kpis(REPO_ROOT / "config" / "kpis"), rules)
    for rs in rules.values():
        for p in rs.all_probes:
            engine.check_probe(p)


def test_every_probe_the_seeder_expects_exists():
    """The fault manifest names probes; a renamed probe would fail silently."""
    rules = load_rules(REPO_ROOT / "config" / "rules")
    ids = {p.id for rs in rules.values() for p in rs.all_probes}
    assert {"PR-FEED-COMPLETENESS", "PR-VOLUME-SPIKE",
            "PR-RATING-ACCURACY", "PR-COLLECTION-DRIFT"} <= ids


def test_cross_check_reports_a_probe_on_an_unknown_field():
    """Regression: this check used to crash on an undefined name instead of reporting."""
    lenses = load_lenses(REPO_ROOT / "config" / "lenses")
    bad = RuleSet.model_validate({
        "name": "bad",
        "subject_types": [{"id": "site"}],
        "alert_types": [{"id": 1, "name": "Volume"}],
        "rules": [{"id": "R", "name": "R", "probes": [
            probe(value_field="no_such_field",
                  query={"fields": [{"field": "usage_date"}]}).model_dump()]}],
    })
    with pytest.raises(ConfigError, match="no_such_field"):
        validate_lens_references(lenses, rules={"bad": bad})


def test_probe_must_select_the_fields_it_reads():
    with pytest.raises(ValueError, match="does not select"):
        engine.check_probe(probe(query={"fields": [{"field": "usage_date"}]}))


# --------------------------------------------------------------------------
# Comparators
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comparator, value, baseline, threshold, expected",
    [
        (Comparator.LT, -0.85, None, -0.6, True),
        (Comparator.LT, -0.40, None, -0.6, False),
        (Comparator.GT, 1.4, None, 0.6, True),
        (Comparator.PP_DRIFT_ABOVE, 0.52, 0.02, 0.15, True),
        (Comparator.PP_DRIFT_ABOVE, 0.05, 0.02, 0.15, False),
        (Comparator.PCT_CHANGE_ABOVE, 150, 100, 0.4, True),
        (Comparator.PCT_CHANGE_BELOW, 50, 100, 0.4, True),
        (Comparator.PCT_CHANGE_BELOW, 90, 100, 0.4, False),
    ],
)
def test_comparators(comparator, value, baseline, threshold, expected):
    assert engine.breaches(comparator, value, baseline, threshold)[0] is expected


def test_drift_without_a_baseline_never_breaches():
    assert engine.breaches(Comparator.PP_DRIFT_ABOVE, 0.9, None, 0.1) == (False, None)


# --------------------------------------------------------------------------
# Evaluation and suppression
# --------------------------------------------------------------------------


def test_one_site_down_is_an_open_alert_with_a_plain_summary():
    rows = [row("SITE-03", -0.85)] + [row(f"SITE-0{i}", 0.02) for i in (1, 2, 4, 5)]
    result = engine.evaluate(probe(suppress_when_share=0.5), rows)
    assert len(result.breaches) == 1
    alert = result.breaches[0]
    assert (alert.subject_id, alert.status) == ("SITE-03", "open")
    assert "SITE-03 on 2026-10-04 measured -85%, below the limit of -60%" in alert.summary
    assert alert.evidence == {"event_count": 100}


def test_every_site_down_on_one_day_is_suppressed_as_systemic():
    rows = [row(f"SITE-0{i}", -0.75) for i in range(1, 9)]
    result = engine.evaluate(probe(suppress_when_share=0.5), rows)
    assert len(result.breaches) == 8
    assert {b.status for b in result.breaches} == {"suppressed"}
    assert "8 of 8 sites breached" in result.breaches[0].summary


def test_suppression_is_judged_per_day():
    """A systemic day must not hide a genuine single-site fault on another day."""
    other = date(2026, 10, 1)
    rows = [row(f"SITE-0{i}", -0.75) for i in range(1, 9)]
    rows += [row("SITE-03", -0.85, other)] + [row(f"SITE-0{i}", 0.0, other) for i in (1, 2, 4)]
    result = engine.evaluate(probe(suppress_when_share=0.5), rows)
    by_day = {(b.period, b.status) for b in result.breaches}
    assert (other, "open") in by_day
    assert (DAY, "suppressed") in by_day


def test_without_suppression_configured_everything_stays_open():
    rows = [row(f"SITE-0{i}", -0.75) for i in range(1, 9)]
    result = engine.evaluate(probe(), rows)
    assert {b.status for b in result.breaches} == {"open"}


def test_examined_scope_covers_clean_rows_for_auto_close():
    rows = [row("SITE-01", 0.01), row("SITE-03", -0.85)]
    result = engine.evaluate(probe(), rows)
    assert result.examined == {("SITE-01", DAY), ("SITE-03", DAY)}


def test_drift_summary_states_value_baseline_and_limit():
    p = probe(id="PR-RATE", name="Rating accuracy", value_field="misrated_rate",
              baseline_field="baseline_misrated_rate", subject_field="customer_class",
              subject_type="customer_class", comparator="pp_drift_above", threshold=0.15,
              query={"fields": [{"field": f} for f in ("usage_date", "customer_class",
                                                        "misrated_rate", "baseline_misrated_rate")]},
              evidence_fields=[])
    rows = [{"usage_date": "2026-10-11", "customer_class": "ENTERPRISE",
             "misrated_rate": 0.552, "baseline_misrated_rate": 0.020}]
    [alert] = engine.evaluate(p, rows).breaches
    assert alert.summary == (
        "Rating accuracy: ENTERPRISE on 2026-10-11 was 55.2% against a trailing 2.0% "
        "-- up 53.2 percentage points (limit 15.0 percentage points)."
    )
