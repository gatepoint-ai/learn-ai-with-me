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
    ("codex", os.getenv("CODEX_URL", "http://codex:8000"), "/codex/v1/ingest"),
    ("pulse", os.getenv("PULSE_URL", "http://pulse:8000"), "/pulse/v1/recalculate"),
    ("sentry", os.getenv("SENTRY_URL", "http://sentry:8000"), "/sentry/v1/run"),
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
            total = body.get("kpis", body.get("probes", body.get("collections", "?")))
            print(f"  {'x' if failed else '+'} {name}: {total} evaluated, {failed} failed")
            for r in body.get("results", []):
                label = r.get("kpi") or r.get("probe") or r.get("collection")
                if r.get("error"):
                    extra = r["error"]
                elif "chunks_written" in r:
                    extra = (f"{r['documents']} documents: {r['ingested']} ingested, "
                             f"{r['unchanged']} unchanged, {r['chunks_written']} chunks")
                    r = {**r, "status": "succeeded"}
                elif "periods" in r:
                    extra = f"{r['periods']} periods, {r['anomalies']} anomalies"
                else:
                    extra = (f"{r['rows']} rows -> {r['open']} open, "
                             f"{r['suppressed']} suppressed, {r['closed']} closed")
                print(f"      {label:22s} {r['status']:9s} {extra}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
