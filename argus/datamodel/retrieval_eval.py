#!/usr/bin/env python3
"""Measure retrieval on our own documents before trusting any default.

Runs every question in `config/codex/eval/retrieval-questions.yaml` through
Codex in four configurations -- keyword only, vector only, hybrid, hybrid +
rerank -- and reports hit@3 (the expected section is in the top 3) for exact-ID
questions and plain-English questions separately. The split is the point: a
single average hides that each retriever fails on a different kind of question.

Also checks the opposite failure: off-topic questions must return nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml

CODEX = os.getenv("CODEX_URL", "http://codex:8000")
QUESTIONS = Path(os.getenv("ARGUS_RETRIEVAL_EVAL", "/config/codex/eval/retrieval-questions.yaml"))

CONFIGS = [
    ("keyword only", {"mode": "keyword", "rerank": False}),
    ("vector only", {"mode": "vector", "rerank": False}),
    ("hybrid", {"mode": "hybrid", "rerank": False}),
    ("hybrid + rerank", {"mode": "hybrid", "rerank": True}),
]

OFF_TOPIC = [
    "What is the capital of France?",
    "How do I bake sourdough bread?",
    "Who won the football world cup in 2014?",
]


def main() -> int:
    spec = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    cases = spec["questions"]
    if not cases:
        print("FAIL: no evaluation questions -- a score over zero cases means nothing.")
        return 1
    kinds = sorted({c["kind"] for c in cases})

    with httpx.Client(timeout=120) as client:
        def search(question: str, **opts) -> list[dict]:
            r = client.post(f"{CODEX}/codex/v1/search",
                            json={"query": question, "top_k": 3, **opts})
            r.raise_for_status()
            return r.json()["hits"]

        print(f"Retrieval evaluation -- {len(cases)} questions, hit@3\n")
        header = f"  {'configuration':18s}" + "".join(f"{k:>12s}" for k in kinds) + f"{'all':>12s}"
        print(header)
        misses: dict[str, list[str]] = {}
        for name, opts in CONFIGS:
            hits_by_kind = {k: [0, 0] for k in kinds}
            for case in cases:
                top = [h["heading"] for h in search(case["question"], **opts)]
                ok = case["expect"] in top
                hits_by_kind[case["kind"]][0] += ok
                hits_by_kind[case["kind"]][1] += 1
                if not ok:
                    misses.setdefault(name, []).append(f"[{case['kind']}] {case['question']}")
            total_ok = sum(v[0] for v in hits_by_kind.values())
            cells = "".join(f"{f'{ok}/{n}':>12s}" for ok, n in hits_by_kind.values())
            print(f"  {name:18s}{cells}{f'{total_ok}/{len(cases)}':>12s}")

        print("\n  Off-topic questions (should return nothing):")
        leaks = 0
        for q in OFF_TOPIC:
            hits = search(q, mode="hybrid", rerank=True)
            leaks += bool(hits)
            print(f"    {'ok  ' if not hits else 'LEAK'} {q} -> {len(hits)} hits")

        for name, missed in misses.items():
            print(f"\n  Missed by {name}:")
            for m in missed:
                print(f"    - {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
