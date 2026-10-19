#!/usr/bin/env python3
"""Acceptance gate: did the rule engine catch exactly what the seeder planted?

Reads the fault manifest the seeder wrote, reads alerts through Prism (the
same path the dashboard and the Analyst use), and checks every fault.

The gate is written to fail closed. The three ways a naive gate passes when it
should not are each a hard failure here:

1. **Vacuous truth.** "All cases passed" is true of zero cases. A manifest with
   no faults, or a rule engine that never ran, fails -- it does not pass.
2. **Partial runs.** Every fault date is checked, not a sample. A run that
   covered two of three days of a fault fails.
3. **Silent false positives.** An open alert the manifest cannot explain is a
   failure too. Catching every fault by alerting on everything is not a pass.

Exit code 0 only when the pass rate is exactly 1.0 -- not "high".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

PRISM = os.getenv("PRISM_URL", "http://prism:8000")
MANIFEST = Path(os.getenv("ARGUS_MANIFEST", "datamodel/seed/fault-manifest.json"))


def fetch_alerts(client: httpx.Client) -> list[dict]:
    response = client.post(
        f"{PRISM}/prism/v1/lenses/alerts/query",
        json={
            "fields": [{"field": f} for f in ("period", "probe_id", "subject_id", "status", "summary")],
            "limit": 100000,
        },
    )
    response.raise_for_status()
    return response.json()["rows"]


def main() -> int:
    if not MANIFEST.exists():
        print(f"FAIL: no fault manifest at {MANIFEST} -- has the seeder run?")
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    faults = manifest.get("faults", [])
    if not faults:
        print("FAIL: the manifest lists zero faults. A gate over zero cases proves nothing.")
        return 1

    with httpx.Client(timeout=60) as client:
        alerts = fetch_alerts(client)
    if not alerts:
        print("FAIL: there are no alerts at all -- did Sentry run?")
        return 1

    index: dict[tuple[str, str, str], str] = {
        (a["probe_id"], a["subject_id"], str(a["period"])[:10]): a["status"] for a in alerts
    }

    checks = passed = 0
    explained: set[tuple[str, str, str]] = set()
    print(f"Acceptance -- {len(faults)} planted cases, seed {manifest.get('seed')}, "
          f"{manifest.get('start_date')} .. {manifest.get('end_date')}\n")

    for fault in faults:
        expect = fault.get("expect", "alert")
        probe = fault["expect_probe"]
        subject = fault.get("site_id") or fault.get("customer_class")
        for day in fault["dates"]:
            checks += 1
            if expect == "alert":
                key = (probe, subject, day)
                status = index.get(key)
                ok = status == "open"
                explained.add(key)
                detail = f"{probe} on {subject}: {status or 'no alert'}"
            else:
                # Negative control: every alert from this probe on this day
                # must be suppressed, and there must be at least one -- an
                # absent alert would mean the probe never looked.
                day_alerts = {k: s for k, s in index.items() if k[0] == probe and k[2] == day}
                open_ones = [k for k, s in day_alerts.items() if s == "open"]
                ok = bool(day_alerts) and not open_ones
                explained.update(day_alerts)
                detail = (f"{probe}: {len(day_alerts)} breaches, "
                          f"{len(day_alerts) - len(open_ones)} suppressed, {len(open_ones)} open")
            passed += ok
            print(f"  {'PASS' if ok else 'FAIL'}  {fault['id']:17s} {day}  "
                  f"expect {expect:10s} {detail}")

    unexplained = [(k, s) for k, s in index.items() if s == "open" and k not in explained]
    print()
    if unexplained:
        print(f"  {len(unexplained)} open alert(s) the manifest does not explain:")
        for (probe, subject, day), _ in sorted(unexplained):
            print(f"    - {probe} {subject} {day}")
    else:
        print("  No unexplained open alerts.")

    rate = passed / checks if checks else 0.0
    print(f"\n  {passed}/{checks} checks passed (pass rate {rate:.2f}), "
          f"{len(unexplained)} unexplained alerts")
    if checks and passed == checks and not unexplained:
        print("  GATE: PASS")
        return 0
    print("  GATE: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
