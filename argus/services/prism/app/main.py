"""Prism -- the query layer.

Serves lenses: named SQL views with a closed list of queryable fields. Every
other service reads analytics data through here, so this is the single place
where SQL is generated and the single place where a cache lives.

The API is deliberately small. Three endpoints list and describe lenses, one
executes a query. Everything else -- KPI maths, rule evaluation, narrative --
belongs to the services that consume this one.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from argus_core.clients.cache import CacheClient, cache_key, fingerprint
from argus_core.config import ConfigError, load_lenses
from argus_core.logging import configure_logging, get_logger
from argus_core.schemas.lens import LensSpec, UnknownFieldError
from argus_core.schemas.query import QueryRequest
from argus_core.settings import (
    ClickHouseSettings,
    ConfigPaths,
    LogSettings,
    PostgresSettings,
    ValkeySettings,
)
from argus_core.sqlbuilder import compile_query

from .datasources import DatasourceError, DatasourceRegistry

log = get_logger("prism")

API = "/prism/v1"


class LensState:
    """Everything loaded once at startup.

    Config is read here and never re-read. A lens change requires a restart,
    which is the right trade: it makes the running config knowable, and the
    cache fingerprint is derived from it.
    """

    def __init__(self) -> None:
        self.log_settings = LogSettings(service_name="prism")
        self.paths = ConfigPaths()
        self.cache = CacheClient(ValkeySettings())
        self.registry = DatasourceRegistry(ClickHouseSettings(), PostgresSettings())
        self.lenses: dict[str, LensSpec] = {}
        self.fingerprints: dict[str, str] = {}

    def load(self) -> None:
        self.lenses = load_lenses(self.paths.lenses)
        # The fingerprint folds the lens definition into every cache key, so
        # editing a lens's SQL invalidates its cached results without a manual
        # flush. Without it, a schema change serves stale rows for cache_life.
        self.fingerprints = {
            name: fingerprint(
                lens.sql, ",".join(f.field for f in lens.fields), lens.dialect.value
            )
            for name, lens in self.lenses.items()
        }
        log.info(
            "lenses_loaded",
            count=len(self.lenses),
            names=sorted(self.lenses),
        )

    def lens(self, name: str) -> LensSpec:
        try:
            return self.lenses[name]
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "unknown_lens",
                    "message": f"no lens named {name!r}",
                    "available": sorted(self.lenses),
                },
            ) from None


state = LensState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(
        state.log_settings.service_name,
        state.log_settings.log_level,
        state.log_settings.log_json,
    )
    try:
        state.load()
    except ConfigError as exc:
        # Refuse to start on bad config. Serving a partially-loaded lens set
        # would mean some queries silently 404 instead of the deploy failing.
        log.error("config_invalid", problems=exc.problems)
        raise

    yield

    state.cache.close()


app = FastAPI(
    title="Argus Prism",
    description="Lens query layer",
    version="0.1.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


class FieldInfo(BaseModel):
    field: str
    data_type: str
    label: str
    description: str | None = None
    allow_filter: bool
    allow_group_by: bool


class LensSummary(BaseModel):
    name: str
    label: str
    description: str
    datasource: str
    dialect: str
    tags: list[str]
    field_count: int


class LensDetail(LensSummary):
    fields: list[FieldInfo]
    cache_life: int
    default_limit: int


class QueryResponse(BaseModel):
    lens: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    cached: bool
    elapsed_ms: float
    #: Returned so a caller debugging a wrong number can see the exact SQL.
    sql: str | None = Field(default=None)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus datasource reachability.

    Reports 200 whenever the service itself is up, even if a datasource is
    down: Prism with one broken datasource still serves the other lenses, and
    reporting unhealthy would take it out of rotation for no reason.
    """
    return {
        "status": "ok",
        "lenses": len(state.lenses),
        "cache": "up" if state.cache.ping() else "unavailable",
        "datasources": state.registry.health(),
    }


@app.get(f"{API}/lenses", response_model=list[LensSummary])
def list_lenses(tag: str | None = None) -> list[LensSummary]:
    """List available lenses, optionally filtered by tag."""
    out = []
    for name, lens in sorted(state.lenses.items()):
        if tag and tag not in lens.tags:
            continue
        out.append(
            LensSummary(
                name=name,
                label=lens.label,
                description=lens.description.strip(),
                datasource=lens.datasource,
                dialect=lens.dialect.value,
                tags=lens.tags,
                field_count=len(lens.fields),
            )
        )
    return out


@app.get(f"{API}/lenses/{{name}}", response_model=LensDetail)
def describe_lens(name: str) -> LensDetail:
    """Full field list for one lens.

    This is what the Analyst calls before composing a query -- it is how the
    model learns which fields exist without being handed the whole catalogue in
    its system prompt.
    """
    lens = state.lens(name)
    return LensDetail(
        name=lens.name,
        label=lens.label,
        description=lens.description.strip(),
        datasource=lens.datasource,
        dialect=lens.dialect.value,
        tags=lens.tags,
        field_count=len(lens.fields),
        cache_life=lens.cache_life,
        default_limit=lens.default_limit,
        fields=[
            FieldInfo(
                field=f.field,
                data_type=f.data_type.value,
                label=f.label or f.field,
                description=f.description,
                allow_filter=f.allow_filter,
                allow_group_by=f.allow_group_by,
            )
            for f in lens.fields
        ],
    )


@app.get(f"{API}/lenses/{{name}}/values/{{field}}")
def field_values(name: str, field: str, limit: int = 200) -> dict[str, Any]:
    """Distinct values for one field.

    Powers dashboard filter dropdowns, and lets the Analyst discover that a
    channel is spelled `PREPAID` rather than guessing `prepaid` and getting an
    empty result it cannot explain.
    """
    lens = state.lens(name)
    spec = _resolve_or_400(lens, field)
    if not spec.allow_group_by:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_groupable",
                "message": f"field {field!r} cannot be enumerated",
            },
        )

    request = QueryRequest(
        fields=[{"field": field}],
        group_by=[field],
        order_by=[{"field": field, "direction": "asc"}],
        limit=min(limit, 1000),
    )
    result = _execute(lens, request, include_sql=False)
    return {
        "lens": name,
        "field": field,
        "values": [row[field] for row in result.rows],
        "count": result.row_count,
    }


@app.post(f"{API}/lenses/{{name}}/query", response_model=QueryResponse)
def query_lens(
    name: str, request: QueryRequest, explain: bool = False
) -> QueryResponse:
    """Execute a query against a lens.

    `explain=true` returns the generated SQL alongside the rows. That is a
    debugging aid, not a second code path -- the same SQL runs either way.
    """
    lens = state.lens(name)
    return _execute(lens, request, include_sql=explain)


@app.post(f"{API}/cache/flush")
def flush_cache(lens: str | None = None) -> dict[str, Any]:
    """Drop cached results, for the whole service or one lens."""
    prefix = f"prism:{lens}:" if lens else "prism:"
    removed = state.cache.delete_prefix(prefix)
    log.info("cache_flushed", lens=lens, removed=removed)
    return {"flushed": removed, "lens": lens}


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _resolve_or_400(lens: LensSpec, field: str):
    try:
        return lens.resolve(field)
    except UnknownFieldError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_field",
                "message": str(exc),
                "lens": lens.name,
                "available": exc.known,
            },
        ) from None


def _execute(lens: LensSpec, request: QueryRequest, *, include_sql: bool):
    started = time.perf_counter()

    try:
        sql, params = compile_query(lens, request)
    except UnknownFieldError as exc:
        # A 400, not a 500: the caller named a field that does not exist, and
        # the response tells them which fields do.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_field",
                "message": str(exc),
                "lens": lens.name,
                "available": exc.known,
            },
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_query", "message": str(exc), "lens": lens.name},
        ) from None

    key = None
    if lens.allow_cache and state.cache.enabled:
        key = cache_key(
            lens.name,
            state.fingerprints[lens.name],
            request.model_dump(mode="json"),
        )
        hit = state.cache.get(key)
        if hit is not None:
            elapsed = (time.perf_counter() - started) * 1000
            return QueryResponse(
                lens=lens.name,
                columns=hit["columns"],
                rows=hit["rows"],
                row_count=hit["row_count"],
                cached=True,
                elapsed_ms=round(elapsed, 2),
                sql=sql if include_sql else None,
            )

    try:
        executor = state.registry.get(lens.datasource)
        result = executor.run(sql, params)
    except DatasourceError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "datasource_unavailable", "message": str(exc)},
        ) from None
    except Exception as exc:
        # The generated SQL is included because a query error here is almost
        # always a lens-authoring bug, and hiding the SQL makes it unfindable.
        log.error(
            "query_failed", lens=lens.name, error=str(exc), sql=sql, params=params
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "query_failed",
                "message": str(exc),
                "lens": lens.name,
                "sql": sql,
            },
        ) from None

    if key is not None:
        state.cache.set(
            key,
            {
                "columns": result.columns,
                "rows": result.rows,
                "row_count": result.row_count,
            },
            lens.cache_life,
        )

    elapsed = (time.perf_counter() - started) * 1000
    log.info(
        "query_ok",
        lens=lens.name,
        rows=result.row_count,
        elapsed_ms=round(elapsed, 2),
    )
    return QueryResponse(
        lens=lens.name,
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        cached=False,
        elapsed_ms=round(elapsed, 2),
        sql=sql if include_sql else None,
    )


@app.exception_handler(ConfigError)
async def config_error_handler(_: Request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "config_invalid", "problems": exc.problems},
    )
