#!/usr/bin/env python3
"""Run every computation in dependency order: KPIs first, then rules.

Each step is an HTTP call to the service that owns it. A step that is not
deployed yet is reported and skipped, so the bootstrap works at every stage of
the build.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

STEPS = [
    ("pulse", os.getenv("PULSE_URL", "http://pulse:8000"), "/pulse/v1/recalculate"),
]


def wait_healthy(client: httpx.Client, base: str, seconds: int = 90) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if client.get(f"{base}/health").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def main() -> int:
    failures = 0
    with httpx.Client(timeout=300) as client:
        for name, base, path in STEPS:
            if not wait_healthy(client, base):
                print(f"  - {name}: not reachable at {base}, skipping")
                continue
            body = client.post(f"{base}{path}").json()
            failed = body.get("failed", 0)
            failures += failed
            print(f"  {'x' if failed else '+'} {name}: {body.get('kpis', '?')} computed, {failed} failed")
            for r in body.get("results", []):
                extra = r.get("error") or f"{r.get('periods')} periods, {r.get('anomalies')} anomalies"
                print(f"      {r['kpi']:20s} {r['status']:9s} {extra}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
