"""Sentry's writes to its own database.

Two properties matter more than anything else here:

* **Idempotent.** An alert is keyed on (probe, subject, period), so running the
  rules twice updates alerts instead of stacking duplicates.
* **Self-closing.** If a re-run no longer finds a breach the probe examined
  before, the alert closes. Without this the open list becomes a graveyard of
  resolved problems, and people stop trusting the open count -- which is the
  only number on the page that matters.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from argus_core.clients.postgres import PostgresClient
from argus_core.schemas.rules import RuleSet

from .engine import Evaluation


class SentryStore:
    def __init__(self, client: PostgresClient) -> None:
        self.db = client

    def sync_rules(self, rulesets: dict[str, RuleSet]) -> None:
        """Mirror config into the rule tables so the alerts lens can join on it."""
        with self.db.connection() as conn, conn.transaction(), conn.cursor() as cur:
            for name, rs in rulesets.items():
                for st in rs.subject_types:
                    cur.execute(
                        "INSERT INTO subject_type (id, description) VALUES (%s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
                        (st.id, st.description),
                    )
                for at in rs.alert_types:
                    cur.execute(
                        "INSERT INTO alert_type (id, name, description) VALUES (%s, %s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
                        "description = EXCLUDED.description",
                        (at.id, at.name, at.description),
                    )
                for rule in rs.rules:
                    cur.execute(
                        """
                        INSERT INTO rule (id, name, description, process, owner, ruleset, active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,
                            description = EXCLUDED.description, process = EXCLUDED.process,
                            owner = EXCLUDED.owner, ruleset = EXCLUDED.ruleset,
                            active = EXCLUDED.active, updated_at = now()
                        """,
                        (rule.id, rule.name, rule.description, rule.process, rule.owner,
                         name, rule.active),
                    )
                    for p in rule.probes:
                        cur.execute(
                            """
                            INSERT INTO probe (id, rule_id, name, description, lens, comparator,
                                threshold, severity, alert_type, active, spec)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET rule_id = EXCLUDED.rule_id,
                                name = EXCLUDED.name, description = EXCLUDED.description,
                                lens = EXCLUDED.lens, comparator = EXCLUDED.comparator,
                                threshold = EXCLUDED.threshold, severity = EXCLUDED.severity,
                                alert_type = EXCLUDED.alert_type, active = EXCLUDED.active,
                                spec = EXCLUDED.spec, updated_at = now()
                            """,
                            (p.id, rule.id, p.name, p.description, p.lens, p.comparator.value,
                             p.threshold, p.severity.value, p.alert_type,
                             p.active and rule.active, p.model_dump_json()),
                        )

    def start_run(self) -> int:
        return int(self.db.fetch_one("INSERT INTO sentry_run DEFAULT VALUES RETURNING id")["id"])

    def finish_run(self, run_id: int, *, probes: int, rows: int, alerts: int,
                   error: str | None = None) -> None:
        self.db.execute(
            """
            UPDATE sentry_run SET status = %(s)s, probes_evaluated = %(p)s,
                rows_examined = %(r)s, alerts_written = %(a)s, error = %(e)s,
                finished_at = now()
            WHERE id = %(id)s
            """,
            {"s": "failed" if error else "succeeded", "p": probes, "r": rows,
             "a": alerts, "e": error, "id": run_id},
        )

    def apply(self, probe_id: str, evaluation: Evaluation) -> dict[str, int]:
        """Upsert this probe's breaches and close the ones that cleared."""
        counts = {"open": 0, "suppressed": 0, "closed": 0}
        with self.db.connection() as conn, conn.transaction(), conn.cursor() as cur:
            for b in evaluation.breaches:
                cur.execute(
                    """
                    INSERT INTO alert (probe_id, subject_type, subject_id, period, alert_type,
                        severity, observed_value, baseline_value, threshold, deviation,
                        summary, evidence, status)
                    VALUES (%(probe)s, %(st)s, %(sid)s, %(period)s, %(at)s, %(sev)s, %(obs)s,
                        %(base)s, %(thr)s, %(dev)s, %(sum)s, %(ev)s, %(status)s)
                    ON CONFLICT (probe_id, subject_id, period) DO UPDATE SET
                        severity = EXCLUDED.severity, observed_value = EXCLUDED.observed_value,
                        baseline_value = EXCLUDED.baseline_value, threshold = EXCLUDED.threshold,
                        deviation = EXCLUDED.deviation, summary = EXCLUDED.summary,
                        evidence = EXCLUDED.evidence, status = EXCLUDED.status,
                        updated_at = now()
                    """,
                    {"probe": b.probe_id, "st": b.subject_type, "sid": b.subject_id,
                     "period": b.period, "at": b.alert_type, "sev": b.severity,
                     "obs": b.observed, "base": b.baseline, "thr": b.threshold,
                     "dev": b.deviation, "sum": b.summary,
                     "ev": json.dumps(b.evidence, default=str), "status": b.status},
                )
                counts[b.status] += 1

            still = {(b.subject_id, b.period) for b in evaluation.breaches}
            cleared = [(s, p) for s, p in evaluation.examined if (s, p) not in still]
            for subject, period in cleared:
                cur.execute(
                    "UPDATE alert SET status = 'closed', updated_at = now() "
                    "WHERE probe_id = %s AND subject_id = %s AND period = %s "
                    "AND status <> 'closed'",
                    (probe_id, subject, period),
                )
                counts["closed"] += cur.rowcount
        return counts

    # -- reads ---------------------------------------------------------------

    def alerts(self, status: str | None, since: date | None, severity: str | None) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT a.id, a.period, a.probe_id, p.name AS probe_name, r.name AS rule_name,
                   a.subject_type, a.subject_id, a.severity, a.status, a.observed_value,
                   a.baseline_value, a.threshold, a.deviation, a.summary, a.evidence,
                   a.detected_at, a.updated_at
            FROM alert a JOIN probe p ON p.id = a.probe_id JOIN rule r ON r.id = p.rule_id
            WHERE (%(st)s::text IS NULL OR a.status = %(st)s::text)
              AND (%(since)s::date IS NULL OR a.period >= %(since)s::date)
              AND (%(sev)s::text IS NULL OR a.severity = %(sev)s::text)
            ORDER BY a.period DESC, a.severity, a.probe_id
            """,
            {"st": status, "since": since, "sev": severity},
        )

    def latest_run(self) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM sentry_run ORDER BY id DESC LIMIT 1")

    def counts(self) -> dict[str, int]:
        rows = self.db.fetch_all("SELECT status, count(*) AS n FROM alert GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
