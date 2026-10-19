"""Pulse's writes to its own database.

Recalculation replaces a KPI's rows inside one transaction: delete the
window, insert the new points. A run that dies half way rolls back, so the
dashboard never shows half a series.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from argus_core.clients.postgres import PostgresClient
from argus_core.schemas.kpi import KpiSpec

from .engine import Anomaly, ForecastPoint, Point


@dataclass(frozen=True)
class KpiResult:
    kpi: KpiSpec
    points: list[Point]
    breakdowns: dict[str, dict[str, list[Point]]]  # dimension -> value -> points
    anomalies: list[Anomaly]
    forecast: list[ForecastPoint]


class PulseStore:
    def __init__(self, client: PostgresClient) -> None:
        self.db = client

    def sync_definitions(self, kpis: dict[str, KpiSpec]) -> None:
        """Mirror config into kpi_definition so lenses can join on it."""
        with self.db.connection() as conn, conn.cursor() as cur:
            for kpi in kpis.values():
                cur.execute(
                    """
                    INSERT INTO kpi_definition
                        (kpi_id, name, description, lens, unit, grain, direction, tags, spec)
                    VALUES (%(id)s, %(name)s, %(description)s, %(lens)s, %(unit)s,
                            %(grain)s, %(direction)s, %(tags)s, %(spec)s)
                    ON CONFLICT (kpi_id) DO UPDATE SET
                        name = EXCLUDED.name, description = EXCLUDED.description,
                        lens = EXCLUDED.lens, unit = EXCLUDED.unit,
                        grain = EXCLUDED.grain, direction = EXCLUDED.direction,
                        tags = EXCLUDED.tags, spec = EXCLUDED.spec, updated_at = now()
                    """,
                    {
                        "id": kpi.id,
                        "name": kpi.name,
                        "description": kpi.description,
                        "lens": kpi.lens,
                        "unit": kpi.unit,
                        "grain": kpi.grain.value,
                        "direction": kpi.direction.value,
                        "tags": json.dumps(kpi.tags),
                        "spec": kpi.model_dump_json(),
                    },
                )

    def start_run(self, kpi_id: str | None) -> int:
        row = self.db.fetch_one(
            "INSERT INTO kpi_run (kpi_id) VALUES (%(k)s) RETURNING id", {"k": kpi_id}
        )
        return int(row["id"])

    def finish_run(
        self, run_id: int, *, points: int, anomalies: int, error: str | None = None
    ) -> None:
        self.db.execute(
            """
            UPDATE kpi_run SET status = %(s)s, points_written = %(p)s,
                anomalies_found = %(a)s, error = %(e)s, finished_at = now()
            WHERE id = %(id)s
            """,
            {
                "s": "failed" if error else "succeeded",
                "p": points,
                "a": anomalies,
                "e": error,
                "id": run_id,
            },
        )

    def replace(self, result: KpiResult) -> int:
        """Replace everything for one KPI atomically. Returns points written."""
        kpi = result.kpi
        scale = kpi.measure.scale
        written = 0
        with self.db.connection() as conn:
            with conn.transaction(), conn.cursor() as cur:
                for table in ("kpi_value", "kpi_breakdown", "kpi_forecast", "kpi_anomaly"):
                    cur.execute(f"DELETE FROM {table} WHERE kpi_id = %(k)s", {"k": kpi.id})

                values = [
                    {
                        "k": kpi.id,
                        "p": p.period,
                        "v": v,
                        "n": p.numerator if p.denominator is not None else None,
                        "d": p.denominator,
                    }
                    for p in result.points
                    if (v := p.value(scale)) is not None
                ]
                cur.executemany(
                    "INSERT INTO kpi_value (kpi_id, period, value, numerator, denominator) "
                    "VALUES (%(k)s, %(p)s, %(v)s, %(n)s, %(d)s)",
                    values,
                )
                written += len(values)

                rows: list[dict[str, Any]] = []
                for dimension, series in result.breakdowns.items():
                    for dim_value, points in series.items():
                        for p in points:
                            v = p.value(scale)
                            if v is not None:
                                rows.append(
                                    {"k": kpi.id, "p": p.period, "dim": dimension,
                                     "dv": dim_value, "v": v}
                                )
                if rows:
                    cur.executemany(
                        "INSERT INTO kpi_breakdown (kpi_id, period, dimension, dimension_value, value) "
                        "VALUES (%(k)s, %(p)s, %(dim)s, %(dv)s, %(v)s)",
                        rows,
                    )
                written += len(rows)

                if result.forecast:
                    cur.executemany(
                        "INSERT INTO kpi_forecast (kpi_id, period, forecast_value, "
                        "confidence_lower, confidence_upper) "
                        "VALUES (%(k)s, %(p)s, %(v)s, %(lo)s, %(hi)s)",
                        [
                            {"k": kpi.id, "p": f.period, "v": f.value,
                             "lo": f.lower, "hi": f.upper}
                            for f in result.forecast
                        ],
                    )

                if result.anomalies:
                    cur.executemany(
                        "INSERT INTO kpi_anomaly (kpi_id, period, value, expected, deviation, "
                        "z_score, severity, reason) "
                        "VALUES (%(k)s, %(p)s, %(v)s, %(e)s, %(dev)s, %(z)s, %(s)s, %(r)s)",
                        [
                            {"k": kpi.id, "p": a.period, "v": a.value, "e": a.expected,
                             "dev": a.deviation, "z": a.z_score, "s": a.severity,
                             "r": a.reason}
                            for a in result.anomalies
                        ],
                    )
        return written

    # -- reads used by the API ---------------------------------------------

    def latest_values(self) -> dict[str, dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT DISTINCT ON (kpi_id) kpi_id, period, value
            FROM kpi_value ORDER BY kpi_id, period DESC
            """
        )
        return {r["kpi_id"]: r for r in rows}

    def series(self, kpi_id: str, start: date | None, end: date | None) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT v.period, v.value, v.numerator, v.denominator,
                   a.expected, a.z_score, a.severity, a.reason
            FROM kpi_value v
            LEFT JOIN kpi_anomaly a ON a.kpi_id = v.kpi_id AND a.period = v.period
            WHERE v.kpi_id = %(k)s
              AND (%(s)s::date IS NULL OR v.period >= %(s)s::date)
              AND (%(e)s::date IS NULL OR v.period <= %(e)s::date)
            ORDER BY v.period
            """,
            {"k": kpi_id, "s": start, "e": end},
        )

    def forecast(self, kpi_id: str) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            "SELECT period, forecast_value, confidence_lower, confidence_upper "
            "FROM kpi_forecast WHERE kpi_id = %(k)s ORDER BY period",
            {"k": kpi_id},
        )

    def breakdown(self, kpi_id: str, dimension: str, period: date | None) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT period, dimension_value, value FROM kpi_breakdown
            WHERE kpi_id = %(k)s AND dimension = %(d)s
              AND period = COALESCE(%(p)s::date,
                    (SELECT max(period) FROM kpi_breakdown WHERE kpi_id = %(k)s))
            ORDER BY value DESC
            """,
            {"k": kpi_id, "d": dimension, "p": period},
        )

    def anomalies(self, since: date | None) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT a.kpi_id, d.name AS kpi_name, a.period, a.value, a.expected,
                   a.z_score, a.severity, a.reason
            FROM kpi_anomaly a JOIN kpi_definition d USING (kpi_id)
            WHERE (%(s)s::date IS NULL OR a.period >= %(s)s::date)
            ORDER BY a.period DESC, abs(a.z_score) DESC
            """,
            {"s": since},
        )
