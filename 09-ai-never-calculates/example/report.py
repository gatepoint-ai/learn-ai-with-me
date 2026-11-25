"""
Lesson 09 - The AI never calculates.

Run:  python report.py      (Python 3.8+, standard library only, made-up data)

The pattern:
  1. SQL computes every number ("the facts").
  2. The language model receives finished facts and only writes the words.
  3. A checker confirms every number in the text exists in the facts.

For contrast, the script also shows what goes wrong when the model is handed
raw rows and asked to do the arithmetic itself. Both "models" here are simple
stand-ins (no API key needed); swap in a real LLM call where marked.
"""

import re
import sqlite3

# ---------------------------------------------------------------------------
# 1. Made-up data: expected vs collected revenue per site per day
# ---------------------------------------------------------------------------
ROWS = [
    # site,     day,          expected,  collected
    ("North",  "2026-10-05",  12000.00,  11880.00),
    ("North",  "2026-10-06",  12500.00,  12375.00),
    ("North",  "2026-10-07",  11800.00,  11210.00),
    ("Harbor", "2026-10-05",   8000.00,   7990.00),
    ("Harbor", "2026-10-06",   8200.00,   8190.00),
    ("Harbor", "2026-10-07",   7900.00,   7895.00),
    ("Ridge",  "2026-10-05",   9500.00,   9120.00),
    ("Ridge",  "2026-10-06",   9700.00,   9215.00),
    ("Ridge",  "2026-10-07",   9600.00,   9216.00),
]


def build_database():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE revenue (site TEXT, day TEXT, expected REAL, collected REAL)"
    )
    con.executemany("INSERT INTO revenue VALUES (?, ?, ?, ?)", ROWS)
    return con


# ---------------------------------------------------------------------------
# 2. The right way: SQL computes the facts
# ---------------------------------------------------------------------------
def compute_facts(con):
    total_expected, total_gap = con.execute(
        "SELECT SUM(expected), SUM(expected - collected) FROM revenue"
    ).fetchone()

    site, site_gap, site_pct = con.execute(
        """
        SELECT site,
               SUM(expected - collected)                        AS gap,
               100.0 * SUM(expected - collected) / SUM(expected) AS pct
        FROM revenue
        GROUP BY site
        ORDER BY gap DESC
        LIMIT 1
        """
    ).fetchone()

    site_count = con.execute("SELECT COUNT(DISTINCT site) FROM revenue").fetchone()[0]

    # Facts are rounded and formatted HERE, once. The model copies them verbatim.
    return {
        "total_gap": f"{total_gap:,.2f}",
        "total_pct": f"{100.0 * total_gap / total_expected:.1f}",
        "site_count": str(site_count),
        "worst_site": site,
        "worst_site_gap": f"{site_gap:,.2f}",
        "worst_site_pct": f"{site_pct:.1f}",
    }


def build_prompt(facts):
    """What you would send to a real LLM. Note: no raw rows, no arithmetic asked."""
    lines = "\n".join(f"- {k}: {v}" for k, v in facts.items())
    return (
        "Write a two-sentence summary for an executive.\n"
        "Use ONLY the facts below, copying every number exactly. Do not calculate.\n"
        f"Facts:\n{lines}\n"
    )


def stand_in_llm_writes_summary(facts):
    """Stand-in for a real LLM call. Replace with your provider's API call(s)."""
    return (
        f"Revenue leakage this period was {facts['total_gap']} "
        f"({facts['total_pct']}% of expected revenue) across {facts['site_count']} sites. "
        f"{facts['worst_site']} needs attention first: {facts['worst_site_gap']} "
        f"lost, {facts['worst_site_pct']}% of its expected revenue."
    )


# ---------------------------------------------------------------------------
# 3. The wrong way: hand the model raw rows and let it do the maths
# ---------------------------------------------------------------------------
def stand_in_llm_does_arithmetic(rows):
    """
    Simulates a classic failure: the model 'adds up' the rows but skips one
    and rounds loosely. Real models make this kind of slip unpredictably.
    """
    approx_gap = sum(e - c for _, _, e, c in rows[:-1])  # silently drops the last row
    return f"Revenue leakage this period was about {approx_gap:,.0f}."


# ---------------------------------------------------------------------------
# 4. The safety net: every number in the text must exist in the facts
# ---------------------------------------------------------------------------
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def to_float(token):
    return float(token.replace(",", ""))


def check_numbers(text, facts):
    allowed = set()
    for value in facts.values():
        for token in NUMBER.findall(value):
            allowed.add(round(to_float(token), 2))
    found = [round(to_float(t), 2) for t in NUMBER.findall(text)]
    return [n for n in found if n not in allowed]


# ---------------------------------------------------------------------------
def main():
    con = build_database()
    facts = compute_facts(con)

    print("=" * 72)
    print("THE RIGHT WAY - SQL computes, the model writes")
    print("=" * 72)
    print("Prompt sent to the model:\n")
    print(build_prompt(facts))
    good = stand_in_llm_writes_summary(facts)
    print("Summary:\n  " + good)
    unknown = check_numbers(good, facts)
    print("\nNumber check:", "PASS - every number comes from the facts"
          if not unknown else f"FAIL - numbers not in the facts: {unknown}")

    print("\n" + "=" * 72)
    print("THE WRONG WAY - the model gets raw rows and does the arithmetic")
    print("=" * 72)
    bad = stand_in_llm_does_arithmetic(ROWS)
    print("Summary:\n  " + bad)
    unknown = check_numbers(bad, facts)
    print("\nNumber check:", "PASS" if not unknown
          else f"FAIL - numbers not in the facts: {unknown}")
    print(f"(The true total is {facts['total_gap']}.)")


if __name__ == "__main__":
    main()
