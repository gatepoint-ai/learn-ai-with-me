"""Pulse -- the KPI engine.

Reads every input through Prism (it holds no analytics credentials), computes
KPI series, breakdowns, anomalies and forecasts with the pure functions in
`engine`, and writes them to its own database, where Prism's `kpi_series`
lens serves them back to the dashboard and the Analyst.

Adding a KPI is adding a file to `config/kpis/`. There is no KPI-specific code.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from argus_core.clients.postgres import PostgresClient
from argus_core.config import ConfigError, load_kpis, load_lenses, validate_lens_references
from argus_core.logging import configure_logging, get_logger
from argus_core.schemas.kpi import KpiSpec
from argus_core.settings import ConfigPaths, LogSettings, PostgresSettings, ServiceUrls

from . import engine
from .store import KpiResult, PulseStore

log = get_logger("pulse")

API = "/pulse/v1"


class PrismClient:
    """The only way Pulse reaches data."""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def query(self, lens: str, request: dict[str, Any]) -> list[dict[str, Any]]:
        response = self._http.post(f"/prism/v1/lenses/{lens}/query", json=request)
        if response.status_code != 200:
            raise RuntimeError(f"prism {lens}: {response.status_code} {response.text[:300]}")
        return response.json()["rows"]

    def healthy(self) -> bool:
        try:
            return self._http.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._http.close()


class State:
    def __init__(self) -> None:
        self.paths = ConfigPaths()
        self.kpis: dict[str, KpiSpec] = {}
        self.prism: PrismClient | None = None
        self.store: PulseStore | None = None

    def load(self) -> None:
        lenses = load_lenses(self.paths.lenses)
        kpis = load_kpis(self.paths.kpis)
        validate_lens_references(lenses, kpis)
        problems = []
        for kpi in kpis.values():
            try:
                engine.check_spec(kpi)
            except ValueError as exc:
                problems.append(str(exc))
        if problems:
            raise ConfigError(problems)
        self.kpis = kpis


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = LogSettings(service_name="pulse")
    configure_logging(settings.service_name, settings.log_level, settings.log_json)
    try:
        state.load()
    except ConfigError as exc:
        log.error("config_invalid", problems=exc.problems)
        raise
    state.prism = PrismClient(ServiceUrls().prism_url)
    pg = PostgresSettings()
    state.store = PulseStore(
        PostgresClient(
            f"postgresql://{pg.postgres_user}:{pg.postgres_password}"
            f"@{pg.postgres_host}:{pg.postgres_port}/pulse"
        )
    )
    state.store.sync_definitions(state.kpis)
    log.info("pulse_ready", kpis=sorted(state.kpis))
    yield
    state.prism.close()
    state.store.db.close()


app = FastAPI(title="Argus Pulse", description="KPI engine", version="0.1.0", lifespan=lifespan)


def _kpi(kpi_id: str) -> KpiSpec:
    kpi = state.kpis.get(kpi_id)
    if kpi is None:
        raise HTTPException(
            404, {"error": "unknown_kpi", "message": f"no KPI {kpi_id!r}",
                  "known": sorted(state.kpis)}
        )
    return kpi


# --------------------------------------------------------------------------
# Computation
# --------------------------------------------------------------------------


def compute(kpi: KpiSpec) -> KpiResult:
    rows = state.prism.query(kpi.lens, engine.build_request(kpi).model_dump(mode="json"))
    points = engine.rollup(engine.to_points(kpi, rows)[None] if rows else [], kpi.grain)

    breakdowns: dict[str, dict[str, list[engine.Point]]] = {}
    for dimension in kpi.breakdowns:
        dim_rows = state.prism.query(
            kpi.lens, engine.build_request(kpi, dimension).model_dump(mode="json")
        )
        breakdowns[dimension] = {
            value: engine.rollup(series, kpi.grain)
            for value, series in engine.to_points(kpi, dim_rows, dimension).items()
        }

    return KpiResult(
        kpi=kpi,
        points=points,
        breakdowns=breakdowns,
        anomalies=engine.detect_anomalies(kpi, points),
        forecast=engine.forecast(kpi, points),
    )


@app.post(f"{API}/recalculate")
def recalculate(kpi: str | None = None) -> dict[str, Any]:
    """Recompute one KPI, or all of them. Idempotent: safe to run twice."""
    targets = [_kpi(kpi)] if kpi else list(state.kpis.values())
    results = []
    for spec in targets:
        run_id = state.store.start_run(spec.id)
        started = time.perf_counter()
        try:
            result = compute(spec)
            written = state.store.replace(result)
        except Exception as exc:  # recorded, then reported -- never swallowed
            state.store.finish_run(run_id, points=0, anomalies=0, error=str(exc)[:1000])
            log.error("kpi_failed", kpi=spec.id, error=str(exc))
            results.append({"kpi": spec.id, "status": "failed", "error": str(exc)[:300]})
            continue
        state.store.finish_run(run_id, points=written, anomalies=len(result.anomalies))
        results.append(
            {
                "kpi": spec.id,
                "status": "succeeded",
                "periods": len(result.points),
                "points_written": written,
                "anomalies": len(result.anomalies),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )
    failed = sum(r["status"] == "failed" for r in results)
    return {"kpis": len(results), "failed": failed, "results": results}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "kpis": len(state.kpis),
        "prism": "up" if state.prism and state.prism.healthy() else "unavailable",
        "database": "up" if state.store and state.store.db.ping() else "unavailable",
    }


@app.get(f"{API}/kpis")
def list_kpis() -> list[dict[str, Any]]:
    latest = state.store.latest_values()
    out = []
    for kpi in sorted(state.kpis.values(), key=lambda k: k.id):
        last = latest.get(kpi.id)
        value = last["value"] if last else None
        out.append(
            {
                "id": kpi.id,
                "name": kpi.name,
                "description": kpi.description,
                "unit": kpi.unit,
                "direction": kpi.direction.value,
                "grain": kpi.grain.value,
                "breakdowns": kpi.breakdowns,
                "latest_period": last["period"].isoformat() if last else None,
                "latest_value": value,
                "latest_display": engine.format_value(value, kpi.unit) if value is not None else None,
                "status": engine.status(kpi, value),
                "thresholds": kpi.thresholds.model_dump(),
            }
        )
    return out


@app.get(f"{API}/kpis/{{kpi_id}}/series")
def kpi_series(kpi_id: str, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    kpi = _kpi(kpi_id)
    return {
        "kpi": kpi.id,
        "name": kpi.name,
        "unit": kpi.unit,
        "series": state.store.series(kpi.id, start, end),
        "forecast": state.store.forecast(kpi.id),
    }


@app.get(f"{API}/kpis/{{kpi_id}}/breakdown/{{dimension}}")
def kpi_breakdown(kpi_id: str, dimension: str, period: date | None = None) -> dict[str, Any]:
    kpi = _kpi(kpi_id)
    if dimension not in kpi.breakdowns:
        raise HTTPException(
            400, {"error": "unknown_dimension", "message": f"{kpi.id} has no breakdown {dimension!r}",
                  "known": kpi.breakdowns}
        )
    return {"kpi": kpi.id, "dimension": dimension,
            "rows": state.store.breakdown(kpi.id, dimension, period)}


@app.get(f"{API}/anomalies")
def anomalies(since: date | None = None) -> list[dict[str, Any]]:
    return state.store.anomalies(since)
