"""Lesson 05 - give the model a catalog, not a keyboard.

The model never writes SQL. It fills in a small form: which domain, which
fields, which filters. The platform checks every name against a declared
catalog and writes the SQL itself, with every value bound as a parameter.

Standard library only. Made-up data in an in-memory SQLite database.
Run:  python catalog_query.py
"""

from __future__ import annotations

import sqlite3

# ---------------------------------------------------------------------------
# The catalog: what may be asked. Business labels, readable fields, filterable
# fields. Anything not listed here does not exist as far as the model knows.
# ---------------------------------------------------------------------------
CATALOG = {
    "leakage_by_site": {
        "label": "Revenue leakage by site and day",
        "table": "daily_site_revenue",
        "fields": {
            "day": "Day",
            "site": "Site",
            "expected": "Expected revenue",
            "collected": "Collected revenue",
            "leaked": "Leaked revenue (expected minus collected)",
        },
        "filterable": {"day", "site"},
        "measures": {"expected", "collected", "leaked"},
    },
}

OPERATORS = {"eq": "=", "gte": ">=", "lte": "<="}


class Rejected(ValueError):
    """The request names something the catalog does not declare."""


def compile_request(request: dict) -> tuple[str, list]:
    """Turn the model's form into SQL. Every identifier comes from the catalog."""
    domain = CATALOG.get(request.get("domain", ""))
    if domain is None:
        raise Rejected(f"unknown domain {request.get('domain')!r}")

    def field(name: str) -> str:
        if name not in domain["fields"]:
            raise Rejected(f"field {name!r} is not in the catalog for this domain")
        return name  # safe: it is one of our own declared names, never the request's text

    group = [field(f) for f in request.get("group_by", [])]
    measures = []
    for m in request.get("sum", []):
        if field(m) not in domain["measures"]:
            raise Rejected(f"{m!r} cannot be summed")
        measures.append(m)
    sums = [f"SUM({m}) AS {m}" for m in measures]

    where, params = [], []
    for f in request.get("filters", []):
        name = field(f["field"])
        if name not in domain["filterable"]:
            raise Rejected(f"{name!r} cannot be filtered on")
        op = OPERATORS.get(f.get("op", ""))
        if op is None:
            raise Rejected(f"operator {f.get('op')!r} is not allowed")
        where.append(f"{name} {op} ?")
        params.append(f["value"])  # bound as a parameter, never pasted into the SQL

    sql = f"SELECT {', '.join(group + sums)} FROM {domain['table']}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        order_by = measures[0] if measures else group[0]
        sql += f" GROUP BY {', '.join(group)} ORDER BY {order_by} DESC"
    return sql + " LIMIT 50", params  # bounded: no request can scan everything


def make_database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE daily_site_revenue (day TEXT, site TEXT, expected REAL, collected REAL, leaked REAL)")
    rows = [
        ("2026-10-01", "North", 10000, 9800), ("2026-10-01", "Ridge", 8000, 7600),
        ("2026-10-01", "Harbor", 6000, 5940), ("2026-10-02", "North", 10200, 10050),
        ("2026-10-02", "Ridge", 8100, 7350), ("2026-10-02", "Harbor", 5900, 5870),
    ]
    db.executemany("INSERT INTO daily_site_revenue VALUES (?, ?, ?, ?, ?)",
                   [(d, s, e, c, e - c) for d, s, e, c in rows])
    return db


def run(db: sqlite3.Connection, title: str, request: dict) -> None:
    print(f"\n{title}\n  model's form: {request}")
    try:
        sql, params = compile_request(request)
    except Rejected as reason:
        print(f"  REJECTED: {reason}")
        return
    print(f"  SQL written by the platform: {sql}\n  parameters: {params}")
    for row in db.execute(sql, params):
        print(f"    {row}")


def main() -> None:
    db = make_database()
    print("=" * 72)
    print("Question: which site leaked the most in October?")
    print("=" * 72)

    run(db, "1. A normal request", {
        "domain": "leakage_by_site", "group_by": ["site"], "sum": ["leaked"],
        "filters": [{"field": "day", "op": "gte", "value": "2026-10-01"}]})

    run(db, "2. Same question, asked by the dashboard: same form, same SQL, same number", {
        "domain": "leakage_by_site", "group_by": ["site"], "sum": ["leaked"],
        "filters": [{"field": "day", "op": "gte", "value": "2026-10-01"}]})

    run(db, "3. The model invents a column that sounds right", {
        "domain": "leakage_by_site", "group_by": ["site"], "sum": ["revenue_lost"]})

    run(db, "4. A field name carrying an injection attempt", {
        "domain": "leakage_by_site", "group_by": ["site; DROP TABLE daily_site_revenue; --"],
        "sum": ["leaked"]})

    run(db, "5. A hostile filter value: bound as data, so it simply matches nothing", {
        "domain": "leakage_by_site", "group_by": ["site"], "sum": ["leaked"],
        "filters": [{"field": "site", "op": "eq", "value": "North' OR '1'='1"}]})

    print("\nThe table is still there:", db.execute("SELECT COUNT(*) FROM daily_site_revenue").fetchone()[0], "rows")


if __name__ == "__main__":
    main()
