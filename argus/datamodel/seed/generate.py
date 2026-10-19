#!/usr/bin/env python3
"""Generate synthetic usage events, with deliberate faults injected.

Why faults matter
=================
A seeder that produces clean data proves only that the stack starts. The
sentry and the KPI anomaly detector would report nothing, and "nothing" is
indistinguishable from "broken". So this generator plants four specific,
documented faults and writes a manifest describing them.

The manifest is the contract between this file and the acceptance test: the test
reads it and asserts the sentry produced an alert for every entry. That
closes the loop from generator to detector, which is the single most valuable
property in the whole demo.

Faults planted
--------------
1. ``feed_outage``      one site loses ~85% of its volume for one day
2. ``volume_spike``     one site doubles its volume for one day
3. ``misrating``        one customer class is mis-rated for three days
4. ``collection_shift`` one site's write-off share jumps for four days

Determinism
-----------
The RNG is seeded, so the same ``--seed`` and ``--days`` always produce the same
data. Tests depend on that; so does being able to reproduce a bug report.

Usage:
    python datamodel/seed/generate.py --days 30
    python datamodel/seed/generate.py --days 60 --seed 7 --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Overridable so a container can write the manifest to a shared volume, where
# the acceptance test reads it. The committed copy is the reference example.
MANIFEST_PATH = Path(
    os.getenv("ARGUS_MANIFEST", REPO_ROOT / "datamodel" / "seed" / "fault-manifest.json")
)

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

SITES: list[tuple[str, str, str]] = [
    ("SITE-01", "North Gateway", "north"),
    ("SITE-02", "Harbour Point", "east"),
    ("SITE-03", "Central Exchange", "central"),
    ("SITE-04", "Westfield Junction", "west"),
    ("SITE-05", "Southbank Terminal", "south"),
    ("SITE-06", "Airport Link", "central"),
    ("SITE-07", "Riverside Crossing", "east"),
    ("SITE-08", "Highland Pass", "north"),
]

PARTNERS: list[tuple[str, str, int]] = [
    ("PRT-HOME", "Home Network", 1),
    ("PRT-ALPHA", "Alpha Interconnect", 0),
    ("PRT-BETA", "Beta Roaming", 0),
    ("PRT-GAMMA", "Gamma Settlement", 0),
]

PRODUCTS: list[tuple[str, str, float]] = [
    ("PRD-STD", "Standard Access", 4.50),
    ("PRD-PLUS", "Plus Access", 7.25),
    ("PRD-BULK", "Bulk Transit", 2.80),
    ("PRD-PREM", "Premium Express", 12.00),
]

CHANNELS = ["PREPAID", "POSTPAID", "INVOICE", "CASH"]
CHANNEL_WEIGHTS = [0.42, 0.31, 0.18, 0.09]

CUSTOMER_CLASSES = ["RESIDENTIAL", "BUSINESS", "ENTERPRISE"]
CLASS_WEIGHTS = [0.63, 0.28, 0.09]

# Baseline collection outcome mix. The collection_shift fault perturbs this.
STATUSES = ["COLLECTED", "PENDING", "WRITTEN_OFF", "DISPUTED"]
STATUS_WEIGHTS = [0.86, 0.09, 0.03, 0.02]

#: Mean events per site per day before seasonality.
BASE_VOLUME = 260


# --------------------------------------------------------------------------
# Fault definitions
# --------------------------------------------------------------------------


@dataclass
class Fault:
    """A planted fault and the probe expected to catch it."""

    id: str
    kind: str
    description: str
    expect_probe: str
    site_id: str | None = None
    customer_class: str | None = None
    dates: list[str] = field(default_factory=list)
    #: Human-readable magnitude, for the manifest and the runbook.
    magnitude: str = ""


def plan_faults(days: list[date], rng: random.Random) -> list[Fault]:
    """Choose where the faults land.

    Placed in the middle of the window on purpose: the anomaly detector needs
    trailing history to have a baseline, and a fault on day 1 or the last day
    would be unfairly hard or trivially clipped.
    """
    if len(days) < 14:
        raise SystemExit(
            f"need at least 14 days to plant faults with usable history, got {len(days)}"
        )

    # Keep a clean warmup so rolling baselines are established before anything
    # anomalous appears.
    warmup = max(7, len(days) // 4)
    candidates = days[warmup:-1]

    outage_day = candidates[len(candidates) // 5]
    spike_day = candidates[len(candidates) // 2]
    misrating_start = candidates[int(len(candidates) * 0.68)]
    shift_start = candidates[int(len(candidates) * 0.82)]

    misrating_days = [
        d for d in (misrating_start + timedelta(days=i) for i in range(3)) if d in set(days)
    ]
    shift_days = [
        d for d in (shift_start + timedelta(days=i) for i in range(4)) if d in set(days)
    ]

    return [
        Fault(
            id="feed_outage",
            kind="feed_outage",
            description=(
                "Ingestion partially failed: the site reported ~15% of its "
                "normal event volume for one day."
            ),
            expect_probe="PR-FEED-COMPLETENESS",
            site_id="SITE-03",
            dates=[outage_day.isoformat()],
            magnitude="-85% volume vs trailing mean",
        ),
        Fault(
            id="volume_spike",
            kind="volume_spike",
            description=(
                "Duplicate ingestion: the site reported roughly double its "
                "normal event volume for one day."
            ),
            expect_probe="PR-VOLUME-SPIKE",
            site_id="SITE-06",
            dates=[spike_day.isoformat()],
            magnitude="+110% volume vs trailing mean",
        ),
        Fault(
            id="misrating",
            kind="misrating",
            description=(
                "Rating engine regression: ENTERPRISE events were billed at the "
                "wrong rate for three consecutive days."
            ),
            expect_probe="PR-RATING-ACCURACY",
            customer_class="ENTERPRISE",
            dates=[d.isoformat() for d in misrating_days],
            magnitude="~55% of ENTERPRISE events mis-rated (baseline ~2%)",
        ),
        Fault(
            id="collection_shift",
            kind="collection_shift",
            description=(
                "Collection process breakdown: the site wrote off a far larger "
                "share of revenue than usual across four days."
            ),
            expect_probe="PR-COLLECTION-DRIFT",
            site_id="SITE-05",
            dates=[d.isoformat() for d in shift_days],
            magnitude="write-off share 3% -> ~26%",
        ),
    ]


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def seasonality(day: date) -> float:
    """Multiplier combining a weekly cycle with a mild annual swing.

    Weekends are quieter, which gives the anomaly detector a genuine weekly
    period to model. Without it, a seasonal-naive forecast would be
    indistinguishable from a flat mean and the forecast tests would prove
    nothing.
    """
    weekday = day.weekday()
    weekly = 1.0 if weekday < 5 else 0.62
    annual = 1.0 + 0.12 * math.sin(2 * math.pi * day.timetuple().tm_yday / 365.0)
    return weekly * annual


def _pick(rng: random.Random, options: list[str], weights: list[float]) -> str:
    return rng.choices(options, weights=weights, k=1)[0]


def generate_events(
    days: list[date], faults: list[Fault], rng: random.Random
) -> tuple[list[tuple], dict[str, int]]:
    """Build the full event list plus a per-fault affected-row tally."""
    by_id = {f.id: f for f in faults}
    outage = by_id["feed_outage"]
    spike = by_id["volume_spike"]
    misrating = by_id["misrating"]
    shift = by_id["collection_shift"]

    outage_dates = set(outage.dates)
    spike_dates = set(spike.dates)
    misrating_dates = set(misrating.dates)
    shift_dates = set(shift.dates)

    product_price = {pid: price for pid, _, price in PRODUCTS}
    partner_home = {pid: home for pid, _, home in PARTNERS}

    rows: list[tuple] = []
    touched: dict[str, int] = {f.id: 0 for f in faults}

    for day in days:
        iso = day.isoformat()
        for site_id, _site_name, _region in SITES:
            volume = BASE_VOLUME * seasonality(day)
            volume *= rng.uniform(0.90, 1.10)  # day-to-day noise

            if site_id == outage.site_id and iso in outage_dates:
                volume *= 0.15
            if site_id == spike.site_id and iso in spike_dates:
                volume *= 2.10

            count = max(1, int(round(volume)))

            for _ in range(count):
                channel = _pick(rng, CHANNELS, CHANNEL_WEIGHTS)
                customer_class = _pick(rng, CUSTOMER_CLASSES, CLASS_WEIGHTS)
                product_id, _, base_price = PRODUCTS[
                    rng.randrange(len(PRODUCTS))
                ]
                partner_id = (
                    "PRT-HOME"
                    if rng.random() < 0.78
                    else PARTNERS[1 + rng.randrange(len(PARTNERS) - 1)][0]
                )

                # -- collection status, perturbed by the shift fault ----------
                weights = STATUS_WEIGHTS
                if site_id == shift.site_id and iso in shift_dates:
                    weights = [0.62, 0.09, 0.26, 0.03]
                    touched["collection_shift"] += 1
                status = _pick(rng, STATUSES, weights)

                # -- rating correctness, perturbed by the misrating fault ----
                misrated = rng.random() < 0.02
                if customer_class == misrating.customer_class and iso in misrating_dates:
                    misrated = rng.random() < 0.55
                    touched["misrating"] += 1

                is_billed = 0 if rng.random() < 0.015 else 1
                is_matched = 0 if rng.random() < 0.012 else 1

                expected = round(
                    base_price
                    * {"RESIDENTIAL": 1.0, "BUSINESS": 1.35, "ENTERPRISE": 1.9}[
                        customer_class
                    ],
                    2,
                )
                # A mis-rated event is billed at the wrong amount; that gap is
                # exactly what the leakage KPI is meant to surface.
                billed = 0.0 if not is_billed else round(
                    expected * (rng.uniform(0.55, 0.8) if misrated else 1.0), 2
                )
                discount = (
                    round(billed * rng.uniform(0.03, 0.12), 2)
                    if rng.random() < 0.18
                    else 0.0
                )
                collected = {
                    "COLLECTED": round(billed - discount, 2),
                    "PENDING": 0.0,
                    "WRITTEN_OFF": 0.0,
                    "DISPUTED": round((billed - discount) * 0.5, 2),
                }[status]

                # Spread events across the working day.
                hour = min(23, max(0, int(rng.gauss(13, 4))))
                ts = datetime(
                    day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60)
                )

                rows.append(
                    (
                        uuid.UUID(int=rng.getrandbits(128)),
                        ts,
                        site_id,
                        partner_id,
                        f"ACCT-{rng.randrange(1, 40000):06d}",
                        product_id,
                        partner_home[partner_id],
                        channel,
                        customer_class,
                        status,
                        expected,
                        billed,
                        collected,
                        discount,
                        is_billed,
                        0 if misrated else 1,
                        is_matched,
                    )
                )

            if site_id == outage.site_id and iso in outage_dates:
                touched["feed_outage"] += count
            if site_id == spike.site_id and iso in spike_dates:
                touched["volume_spike"] += count

    return rows, touched


EVENT_COLUMNS = [
    "event_id",
    "event_ts",
    "site_id",
    "partner_id",
    "account_id",
    "product_id",
    "is_home_partner",
    "channel",
    "customer_class",
    "collection_status",
    "amount_expected",
    "amount_billed",
    "amount_collected",
    "amount_discount",
    "is_billed",
    "is_rated_correctly",
    "is_matched",
]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def connect():
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "app"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "dev_password"),
        database="analytics",
    )


def load(client, rows: list[tuple], batch_size: int = 50_000) -> None:
    """Truncate then insert.

    Truncation is required, not merely tidy: the aggregates are
    SummingMergeTree, so re-inserting the same events would *add* to the
    existing dimension rows rather than replace them, silently doubling every
    figure on the dashboard.
    """
    for table in (
        "analytics.usage_event",
        "analytics.usage_daily_agg",
        "analytics.leakage_daily_agg",
        "analytics.site",
        "analytics.partner",
        "analytics.product",
    ):
        client.command(f"TRUNCATE TABLE IF EXISTS {table}")

    client.insert(
        "analytics.site",
        [list(s) for s in SITES],
        column_names=["site_id", "site_name", "region"],
    )
    client.insert(
        "analytics.partner",
        [list(p) for p in PARTNERS],
        column_names=["partner_id", "partner_name", "is_home_partner"],
    )
    client.insert(
        "analytics.product",
        [list(p) for p in PRODUCTS],
        column_names=["product_id", "product_name", "unit_price"],
    )

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        client.insert("analytics.usage_event", batch, column_names=EVENT_COLUMNS)
        print(f"    inserted {start + len(batch):,}/{len(rows):,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=int(os.getenv("SEED_DAYS", "30")),
        help="days of history to generate (minimum 14)",
    )
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and write the manifest, but do not touch ClickHouse",
    )
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    end = date.today()
    days = [end - timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    faults = plan_faults(days, rng)
    print(f"generating {args.days} days ({days[0]} .. {days[-1]}) seed={args.seed}")

    rows, touched = generate_events(days, faults, rng)
    print(f"  {len(rows):,} events across {len(SITES)} sites")

    print("  planted faults:")
    for fault in faults:
        print(
            f"    - {fault.id:17s} {','.join(fault.dates):32s} "
            f"{touched[fault.id]:>7,} rows  ({fault.magnitude})"
        )

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "seed_days": args.days,
        "start_date": days[0].isoformat(),
        "end_date": days[-1].isoformat(),
        "event_count": len(rows),
        "faults": [asdict(f) | {"affected_rows": touched[f.id]} for f in faults],
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shown = (
        MANIFEST_PATH.relative_to(REPO_ROOT)
        if MANIFEST_PATH.is_relative_to(REPO_ROOT)
        else MANIFEST_PATH
    )
    print(f"  manifest -> {shown}")

    if args.dry_run:
        print("  dry run: ClickHouse untouched")
        return 0

    client = connect()
    try:
        load(client, rows)
        # The MVs populate as the base table is written, so verify through them.
        agg = client.query(
            "SELECT count(), uniqExact(usage_date) FROM analytics.usage_daily_agg"
        ).result_rows[0]
        print(f"  usage_daily_agg: {agg[0]:,} rows over {agg[1]} days")
    finally:
        client.close()

    print("seed complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
