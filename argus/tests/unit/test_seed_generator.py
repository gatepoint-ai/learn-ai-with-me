"""Tests for the synthetic data generator.

The generator is upstream of everything that detects anything, so these tests
assert the planted faults are genuinely detectable *before* any detector exists.
If a fault is not separable from noise here, a probe that "finds" it later is
alert noise, and the acceptance test would pass for the wrong reason.

These tests are pure computation -- no ClickHouse, no network.
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "datamodel" / "seed"))

import random  # noqa: E402

from generate import (  # noqa: E402
    EVENT_COLUMNS,
    SITES,
    generate_events,
    plan_faults,
    seasonality,
)

DAYS = 30
SEED = 20260825

COL = {name: i for i, name in enumerate(EVENT_COLUMNS)}


@pytest.fixture(scope="module")
def dataset():
    rng = random.Random(SEED)
    end = date(2026, 8, 25)
    days = [end - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    faults = plan_faults(days, rng)
    rows, touched = generate_events(days, faults, rng)
    return days, {f.id: f for f in faults}, rows, touched


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_generates_a_plausible_volume(dataset):
    _, _, rows, _ = dataset
    assert 30_000 < len(rows) < 90_000


def test_every_row_has_every_column(dataset):
    _, _, rows, _ = dataset
    assert all(len(r) == len(EVENT_COLUMNS) for r in rows)


def test_is_deterministic_for_a_fixed_seed():
    end = date(2026, 8, 25)
    days = [end - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]

    def run():
        rng = random.Random(SEED)
        faults = plan_faults(days, rng)
        rows, _ = generate_events(days, faults, rng)
        return rows

    first, second = run(), run()
    assert len(first) == len(second)
    assert first[0] == second[0]
    assert first[-1] == second[-1]


def test_different_seeds_produce_different_data():
    end = date(2026, 8, 25)
    days = [end - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    out = []
    for seed in (1, 2):
        rng = random.Random(seed)
        faults = plan_faults(days, rng)
        rows, _ = generate_events(days, faults, rng)
        out.append(len(rows))
    assert out[0] != out[1]


def test_refuses_too_short_a_window():
    days = [date(2026, 8, 25) - timedelta(days=i) for i in range(9, -1, -1)]
    with pytest.raises(SystemExit, match="at least 14 days"):
        plan_faults(days, random.Random(1))


def test_weekends_are_quieter_than_weekdays():
    # A real weekly period is what makes seasonal forecasting meaningful.
    monday = date(2026, 8, 24)
    saturday = date(2026, 8, 22)
    assert seasonality(saturday) < seasonality(monday)


def test_amounts_are_internally_consistent(dataset):
    _, _, rows, _ = dataset
    for row in rows[:4000]:
        billed = row[COL["amount_billed"]]
        collected = row[COL["amount_collected"]]
        discount = row[COL["amount_discount"]]
        status = row[COL["collection_status"]]
        assert billed >= 0 and collected >= 0 and discount >= 0
        # Never collect more than was billed net of discount.
        assert collected <= billed - discount + 0.01
        if status in {"PENDING", "WRITTEN_OFF"}:
            assert collected == 0.0
        if not row[COL["is_billed"]]:
            assert billed == 0.0


# --------------------------------------------------------------------------
# Fault detectability -- the point of the file
# --------------------------------------------------------------------------


def _daily_counts_by_site(rows) -> dict[str, dict[date, int]]:
    counts: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        counts[row[COL["site_id"]]][row[COL["event_ts"]].date()] += 1
    return counts


def test_feed_outage_is_far_below_the_site_baseline(dataset):
    _, faults, rows, _ = dataset
    fault = faults["feed_outage"]
    day = date.fromisoformat(fault.dates[0])
    series = _daily_counts_by_site(rows)[fault.site_id]

    baseline = statistics.mean(v for d, v in series.items() if d != day)
    observed = series[day]

    # The probe threshold is a 20% drop; the fault must clear it decisively.
    assert observed < baseline * 0.30, f"outage {observed} vs baseline {baseline:.0f}"


def test_volume_spike_is_far_above_the_site_baseline(dataset):
    _, faults, rows, _ = dataset
    fault = faults["volume_spike"]
    day = date.fromisoformat(fault.dates[0])
    series = _daily_counts_by_site(rows)[fault.site_id]

    baseline = statistics.mean(v for d, v in series.items() if d != day)
    observed = series[day]

    assert observed > baseline * 1.60, f"spike {observed} vs baseline {baseline:.0f}"


def test_misrating_fault_lifts_the_error_rate_for_one_class(dataset):
    _, faults, rows, _ = dataset
    fault = faults["misrating"]
    affected = set(fault.dates)

    inside = outside = 0
    inside_bad = outside_bad = 0
    for row in rows:
        if row[COL["customer_class"]] != fault.customer_class:
            continue
        iso = row[COL["event_ts"]].date().isoformat()
        bad = row[COL["is_rated_correctly"]] == 0
        if iso in affected:
            inside += 1
            inside_bad += bad
        else:
            outside += 1
            outside_bad += bad

    inside_rate = inside_bad / inside
    outside_rate = outside_bad / outside
    assert outside_rate < 0.06, f"baseline error rate too high: {outside_rate:.3f}"
    assert inside_rate > 0.40, f"fault rate too low: {inside_rate:.3f}"


def test_misrating_fault_is_confined_to_its_class(dataset):
    """A fault that leaks into other dimensions would make the control ambiguous."""
    _, faults, rows, _ = dataset
    fault = faults["misrating"]
    affected = set(fault.dates)

    other_inside = other_inside_bad = 0
    for row in rows:
        if row[COL["customer_class"]] == fault.customer_class:
            continue
        if row[COL["event_ts"]].date().isoformat() in affected:
            other_inside += 1
            other_inside_bad += row[COL["is_rated_correctly"]] == 0

    rate = other_inside_bad / other_inside
    assert rate < 0.06, f"fault leaked into other classes: {rate:.3f}"


def test_collection_shift_raises_writeoff_share_at_one_site(dataset):
    _, faults, rows, _ = dataset
    fault = faults["collection_shift"]
    affected = set(fault.dates)

    inside = outside = 0
    inside_wo = outside_wo = 0
    for row in rows:
        if row[COL["site_id"]] != fault.site_id:
            continue
        iso = row[COL["event_ts"]].date().isoformat()
        wo = row[COL["collection_status"]] == "WRITTEN_OFF"
        if iso in affected:
            inside += 1
            inside_wo += wo
        else:
            outside += 1
            outside_wo += wo

    inside_share = inside_wo / inside
    outside_share = outside_wo / outside
    # The control tests percentage-point drift; assert a wide, unambiguous gap.
    assert outside_share < 0.06
    assert inside_share > 0.18
    assert (inside_share - outside_share) > 0.12


def test_faults_do_not_overlap_in_time(dataset):
    """Overlapping faults would make attribution ambiguous.

    If the spike and the outage landed on the same day at the same site, no
    control could cleanly claim either one, and the acceptance test would be
    asserting something meaningless.
    """
    _, faults, _, _ = dataset
    seen: dict[tuple[str, str], str] = {}
    for fault in faults.values():
        scope = fault.site_id or fault.customer_class or "*"
        for day in fault.dates:
            key = (scope, day)
            assert key not in seen, (
                f"{fault.id} overlaps {seen[key]} on {day} for {scope}"
            )
            seen[key] = fault.id


def test_every_fault_reports_affected_rows(dataset):
    _, faults, _, touched = dataset
    for fault_id in faults:
        assert touched[fault_id] > 0, f"{fault_id} affected no rows"


def test_every_fault_names_the_control_expected_to_catch_it(dataset):
    """The manifest is the seeder-to-sentry contract.

    A fault with no expected probe could never be asserted on, so the
    acceptance test would silently skip it.
    """
    _, faults, _, _ = dataset
    for fault in faults.values():
        assert fault.expect_probe.startswith("PR-")
        assert fault.dates
        assert fault.magnitude


def test_all_sites_produce_data(dataset):
    _, _, rows, _ = dataset
    seen = {row[COL["site_id"]] for row in rows}
    assert seen == {s[0] for s in SITES}
