"""Lesson 06 - two databases, two jobs, two tool families.

"How much leaked?" is a question for the analytics store: exact numbers,
computed by a query. "What does our policy say?" is a question for the
knowledge store: passages from documents, with their source.

The agent gets one family of tools for each, and every tool says when to use
it AND when not to. This script shows the tool descriptions a model would
read, then routes five questions. A stand-in router plays the model, using the
"use for" words; a real model reads the descriptions.

It also shows what goes wrong with only one family: a policy question
answered from numbers.

Standard library only. Made-up data. Run:  python two_tool_families.py
"""

from __future__ import annotations

import json
import sqlite3

# --- The analytics store: numbers --------------------------------------------
def analytics() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE leakage (site TEXT, month TEXT, leaked REAL, write_offs REAL)")
    db.executemany("INSERT INTO leakage VALUES (?, ?, ?, ?)", [
        ("North", "2026-10", 350.0, 120.0),
        ("Ridge", "2026-10", 1150.0, 7000.0),
        ("Harbor", "2026-10", 90.0, 0.0),
    ])
    return db


# --- The knowledge store: documents ------------------------------------------
POLICIES = [
    {"source": "write-off-policy > Approval limits",
     "text": "Write-offs above 5,000 dollars must be approved by the chief financial officer."},
    {"source": "write-off-policy > Evidence",
     "text": "Every write-off needs the invoice number and proof of two collection attempts."},
    {"source": "tariff-change-policy > Freeze",
     "text": "No tariff change may be deployed on the last day of the month."},
]

# --- The tools, as a model would see them -------------------------------------
TOOLS = [
    {"name": "get_leakage",
     "family": "data",
     "description": "Exact leakage and write-off amounts by site and month, computed by a query. "
                    "Use for: how much, which site, totals, trends. "
                    "Do NOT use for: rules, approvals, what is allowed.",
     "use_for": ["how much", "which site", "total", "leak", "this month"]},
    {"name": "search_policies",
     "family": "knowledge",
     "description": "Passages from the company's policy documents, each with its source. "
                    "Use for: who approves, what is allowed, what the rule says. "
                    "Do NOT use for: any amount, count or rate.",
     "use_for": ["who approves", "allowed", "policy", "rule", "can we"]},
]


def get_leakage(db: sqlite3.Connection, question: str) -> str:
    rows = db.execute("SELECT site, leaked, write_offs FROM leakage ORDER BY leaked DESC").fetchall()
    return "; ".join(f"{s}: leaked {l:,.2f}, write-offs {w:,.2f}" for s, l, w in rows)


def _stems(text: str) -> set[str]:
    return {w.strip("?,.\"").lower()[:6] for w in text.split() if len(w) > 3}


def search_policies(question: str) -> str:
    best = max(POLICIES, key=lambda p: len(_stems(question) & _stems(p["text"])))
    return f"\"{best['text']}\" [{best['source']}]"


def stand_in_router(question: str, tools: list[dict]) -> list[dict]:
    q = question.lower()
    chosen = [t for t in tools if any(phrase in q for phrase in t["use_for"])]
    # With one family only, the "model" falls back to the tool it has.
    return chosen or tools[:1]


def answer(db, question: str, tools: list[dict]) -> str:
    lines = []
    for tool in stand_in_router(question, tools):
        result = get_leakage(db, question) if tool["name"] == "get_leakage" else search_policies(question)
        lines.append(f"[{tool['family']} tool: {tool['name']}] {result}")
    return "\n   ".join(lines)


QUESTIONS = [
    "How much leaked at each site in October?",
    "Which site had the biggest write-offs?",
    "Who approves a write-off of 7,000 dollars?",
    "Can we deploy a tariff change on the last day of the month?",
    "Ridge wrote off 7,000 this month. Who approves that?",
]


def main() -> None:
    db = analytics()
    print("=" * 72)
    print("The tool descriptions the model reads")
    print("=" * 72)
    print(json.dumps([{k: t[k] for k in ("name", "description")} for t in TOOLS], indent=2))

    print("\n" + "=" * 72)
    print("Two tool families: each question goes to the right store (the last one needs both)")
    print("=" * 72)
    for q in QUESTIONS:
        print(f"\nQ: {q}\n   {answer(db, q, TOOLS)}")

    print("\n" + "=" * 72)
    print("Only the data family: policy questions get answered from numbers")
    print("=" * 72)
    data_only = [t for t in TOOLS if t["family"] == "data"]
    for q in QUESTIONS[2:4]:
        print(f"\nQ: {q}\n   {answer(db, q, data_only)}")
        print("   -> a list of amounts. Nothing here answers the question.")


if __name__ == "__main__":
    main()
