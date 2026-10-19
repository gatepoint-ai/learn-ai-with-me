"""Sentry -- the rule engine.

Runs every probe in `config/rules/` against its Prism lens, judges each
subject separately, suppresses systemic events, and keeps the alert table
current: new breaches open, cleared breaches close. Like Pulse, it holds no
analytics credentials -- every row it judges came through Prism.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import httpx
from fastapi import FastAPI

from argus_core.clients.postgres import PostgresClient
from argus_core.config import ConfigError, load_lenses, load_rules, validate_lens_references
from argus_core.logging import configure_logging, get_logger
from argus_core.schemas.rules import Probe, RuleSet
from argus_core.settings import ConfigPaths, LogSettings, PostgresSettings, ServiceUrls

from . import engine
from .store import SentryStore

log = get_logger("sentry")

API = "/sentry/v1"


class State:
    def __init__(self) -> None:
        self.paths = ConfigPaths()
        self.rulesets: dict[str, RuleSet] = {}
        self.prism: httpx.Client | None = None
        self.store: SentryStore | None = None

    def load(self) -> None:
        lenses = load_lenses(self.paths.lenses)
        rulesets = load_rules(self.paths.rules)
        validate_lens_references(lenses, rules=rulesets)
        problems = []
        for rs in rulesets.values():
            for probe in rs.all_probes:
                try:
                    engine.check_probe(probe)
                except ValueError as exc:
                    problems.append(str(exc))
        if problems:
            raise ConfigError(problems)
        self.rulesets = rulesets

    @property
    def probes(self) -> list[Probe]:
        return [p for rs in self.rulesets.values() for p in rs.all_probes]


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = LogSettings(service_name="sentry")
    configure_logging(settings.service_name, settings.log_level, settings.log_json)
    try:
        state.load()
    except ConfigError as exc:
        log.error("config_invalid", problems=exc.problems)
        raise
    state.prism = httpx.Client(base_url=ServiceUrls().prism_url, timeout=60)
    pg = PostgresSettings()
    state.store = SentryStore(
        PostgresClient(
            f"postgresql://{pg.postgres_user}:{pg.postgres_password}"
            f"@{pg.postgres_host}:{pg.postgres_port}/sentry"
        )
    )
    state.store.sync_rules(state.rulesets)
    log.info("sentry_ready", probes=[p.id for p in state.probes])
    yield
    state.prism.close()
    state.store.db.close()


app = FastAPI(title="Argus Sentry", description="Rule engine", version="0.1.0", lifespan=lifespan)


def _rows(probe: Probe) -> list[dict[str, Any]]:
    response = state.prism.post(
        f"/prism/v1/lenses/{probe.lens}/query", json=probe.query.model_dump(mode="json")
    )
    if response.status_code != 200:
        raise RuntimeError(f"prism {probe.lens}: {response.status_code} {response.text[:300]}")
    return response.json()["rows"]


@app.post(f"{API}/run")
def run() -> dict[str, Any]:
    """Evaluate every active probe. Idempotent: safe to run twice."""
    run_id = state.store.start_run()
    started = time.perf_counter()
    results, rows_total, written, errors = [], 0, 0, []
    for probe in state.probes:
        try:
            evaluation = engine.evaluate(probe, _rows(probe))
            counts = state.store.apply(probe.id, evaluation)
        except Exception as exc:  # recorded per probe; the others still run
            errors.append(f"{probe.id}: {exc}")
            log.error("probe_failed", probe=probe.id, error=str(exc))
            results.append({"probe": probe.id, "status": "failed", "error": str(exc)[:300]})
            continue
        rows_total += evaluation.rows_examined
        written += counts["open"] + counts["suppressed"]
        results.append({"probe": probe.id, "status": "succeeded",
                        "rows": evaluation.rows_examined, **counts})
    state.store.finish_run(run_id, probes=len(state.probes), rows=rows_total,
                           alerts=written, error="; ".join(errors)[:1000] or None)
    return {
        "run_id": run_id,
        "probes": len(state.probes),
        "failed": len(errors),
        "rows_examined": rows_total,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "results": results,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        prism = state.prism.get("/health").status_code == 200
    except Exception:
        prism = False
    return {"status": "ok", "probes": len(state.probes),
            "prism": "up" if prism else "unavailable",
            "database": "up" if state.store and state.store.db.ping() else "unavailable"}


@app.get(f"{API}/alerts")
def alerts(status: str | None = "open", since: date | None = None,
           severity: str | None = None) -> list[dict[str, Any]]:
    """Open alerts by default. Pass status=all for every status."""
    return state.store.alerts(None if status == "all" else status, since, severity)


@app.get(f"{API}/summary")
def summary() -> dict[str, Any]:
    return {"counts": state.store.counts(), "latest_run": state.store.latest_run()}


@app.get(f"{API}/rules")
def rules() -> list[dict[str, Any]]:
    return [
        {"ruleset": name, "rule": rule.id, "name": rule.name, "owner": rule.owner,
         "process": rule.process,
         "probes": [{"id": p.id, "name": p.name, "lens": p.lens,
                     "comparator": p.comparator.value, "threshold": p.threshold,
                     "severity": p.severity.value,
                     "suppress_when_share": p.suppress_when_share} for p in rule.probes]}
        for name, rs in state.rulesets.items() for rule in rs.rules
    ]
