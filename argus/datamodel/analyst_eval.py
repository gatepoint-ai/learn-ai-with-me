#!/usr/bin/env python3
"""Run the Analyst's golden questions and score each answer.

Reads `config/analyst/eval.yaml`, asks every question through the Analyst API,
and checks the properties listed per case: which tools were (and were not)
used, what the answer must and must not say, and whether every figure was
found in the tool results. Paced for a free-tier token-per-minute limit.

Fails closed: zero cases is a failure, and a case that errors counts as failed.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

ANALYST = os.getenv("ANALYST_URL", "http://analyst:8000")
CASES = Path(os.getenv("ARGUS_ANALYST_EVAL", "/config/analyst/eval.yaml"))
PAUSE = float(os.getenv("ANALYST_EVAL_PAUSE", "20"))


def score(case: dict, reply: dict) -> list[str]:
    """Return the list of failed checks (empty = pass)."""
    failures: list[str] = []
    answer = reply.get("answer", "")
    norm = answer.replace("‑", "-").replace("‐", "-").replace("–", "-")
    used = {t["name"] for t in reply.get("tools_used", [])}
    if case.get("tools_any") and not used & set(case["tools_any"]):
        failures.append(f"expected one of {case['tools_any']}, used {sorted(used) or 'none'}")
    if bad := used & set(case.get("tools_none", [])):
        failures.append(f"used {sorted(bad)}, which this question should not need")
    for pattern in case.get("must_include", []):
        if not re.search(pattern, norm, re.IGNORECASE):
            failures.append(f"missing /{pattern}/")
    for pattern in case.get("must_not_include", []):
        if re.search(pattern, norm, re.IGNORECASE):
            failures.append(f"should not say /{pattern}/")
    if case.get("numbers_ok") and not (reply.get("numbers") or {}).get("ok", False):
        failures.append(f"unverified figures {reply['numbers']['unverified']}")
    return failures


def main() -> int:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8")).get("cases", [])
    if not cases:
        print("FAIL: zero evaluation cases -- a score over nothing proves nothing.")
        return 1
    passed = 0
    tokens = 0
    print(f"Analyst evaluation -- {len(cases)} questions\n")
    with httpx.Client(timeout=300) as client:
        for i, case in enumerate(cases):
            if i:
                time.sleep(PAUSE)
            try:
                r = client.post(f"{ANALYST}/analyst/v1/ask", json={"question": case["question"]})
                reply = r.json()
                if r.status_code != 200:
                    failures = [f"HTTP {r.status_code}: {str(reply)[:160]}"]
                else:
                    failures = score(case, reply)
                    tokens += sum(reply.get("tokens", {}).values())
            except httpx.HTTPError as exc:
                failures, reply = [f"request failed: {exc}"], {}
            ok = not failures
            passed += ok
            tools = ",".join(t["name"] for t in reply.get("tools_used", [])) or "-"
            print(f"  {'PASS' if ok else 'FAIL'}  {case['id']:16s} tools: {tools}")
            for f in failures:
                print(f"          - {f}")
    print(f"\n  {passed}/{len(cases)} passed, {tokens:,} tokens")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
