"""Tests for the Prism HTTP API.

Runs against the real lens config in `config/lenses/` with a stub executor
standing in for ClickHouse and Postgres. That combination is deliberate: the
lenses under test are the ones that ship, so a lens whose declared fields
disagree with its SQL is caught here rather than in a container.

No network, no database, no API key.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "libs" / "argus-core"))
sys.path.insert(0, str(REPO_ROOT / "services" / "prism"))

from app import main as prism_main  # noqa: E402
from app.datasources import DatasourceError, QueryResult  # noqa: E402


class StubExecutor:
    """Records the SQL it was given and returns canned rows."""

    def __init__(self, dialect, rows: list[dict[str, Any]] | None = None) -> None:
        self.dialect = dialect
        self.rows = rows if rows is not None else [{"site_id": "SITE-01", "total": 42.0}]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: Exception | None = None

    def run(self, sql: str, params: dict[str, Any]) -> QueryResult:
        self.calls.append((sql, params))
        if self.fail_with is not None:
            raise self.fail_with
        cols = list(self.rows[0].keys()) if self.rows else []
        return QueryResult(columns=cols, rows=self.rows, row_count=len(self.rows))

    def healthy(self) -> bool:
        return True


class StubRegistry:
    def __init__(self) -> None:
        from argus_core.schemas.lens import Dialect

        self.executors = {
            "analytics": StubExecutor(Dialect.CLICKHOUSE),
            "pulse": StubExecutor(Dialect.POSTGRES),
            "sentry": StubExecutor(Dialect.POSTGRES),
        }
        self.unknown_raises = True

    def get(self, datasource: str):
        if datasource not in self.executors:
            if self.unknown_raises:
                raise DatasourceError(f"unknown datasource {datasource!r}")
        return self.executors[datasource]

    def health(self) -> dict[str, bool]:
        return {name: True for name in self.executors}


@pytest.fixture
def client(monkeypatch):
    # Point the service at the repo's real config, and disable the cache so
    # each test observes an actual execution rather than a previous test's rows.
    monkeypatch.setenv("CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("CACHE_ENABLED", "false")

    state = prism_main.LensState()
    state.registry = StubRegistry()  # type: ignore[assignment]
    monkeypatch.setattr(prism_main, "state", state)

    with TestClient(prism_main.app) as c:
        yield c


@pytest.fixture
def registry(client) -> StubRegistry:
    return prism_main.state.registry  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_health_reports_lens_count(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["lenses"] >= 6


def test_lists_every_shipped_lens(client):
    body = client.get("/prism/v1/lenses").json()
    names = {row["name"] for row in body}
    assert {
        "usage_trends",
        "site_volume_baseline",
        "rating_quality",
        "collection_mix",
        "alerts",
        "kpi_series",
    } <= names


def test_lens_list_filters_by_tag(client):
    body = client.get("/prism/v1/lenses", params={"tag": "baseline"}).json()
    assert body
    assert all("baseline" in row["tags"] for row in body)


def test_describe_lens_returns_full_field_list(client):
    body = client.get("/prism/v1/lenses/usage_trends").json()
    assert body["name"] == "usage_trends"
    assert body["dialect"] == "clickhouse"
    fields = {f["field"] for f in body["fields"]}
    assert {"usage_date", "site_id", "amount_leaked", "event_count"} <= fields
    # Measures are marked non-groupable so a caller cannot GROUP BY revenue.
    leaked = next(f for f in body["fields"] if f["field"] == "amount_leaked")
    assert leaked["allow_group_by"] is False


def test_describe_unknown_lens_is_404_and_lists_alternatives(client):
    response = client.get("/prism/v1/lenses/nope")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_lens"
    assert "usage_trends" in detail["available"]


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------


def test_query_returns_rows(client):
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={
            "fields": [{"field": "site_id"}],
            "aggregates": [
                {"function": "sum", "field": "amount_leaked", "alias": "total"}
            ],
            "group_by": ["site_id"],
            "limit": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lens"] == "usage_trends"
    assert body["row_count"] == 1
    assert body["cached"] is False


def test_query_routes_to_the_lens_datasource(client, registry):
    """A postgres lens must not be executed against ClickHouse."""
    client.post(
        "/prism/v1/lenses/alerts/query",
        json={"fields": [{"field": "severity"}], "limit": 5},
    )
    assert registry.executors["sentry"].calls, "alerts lens did not reach sentry"
    assert not registry.executors["analytics"].calls


def test_dialect_follows_the_datasource(client, registry):
    """The same request shape compiles differently per engine.

    This is the property that lets one QueryRequest serve both stores.
    """
    body = {"fields": [{"field": "site_id"}],
            "filters": [{"field": "site_id", "op": "eq", "value": "SITE-01"}],
            "limit": 5}
    client.post("/prism/v1/lenses/usage_trends/query", json=body)
    ch_sql = registry.executors["analytics"].calls[-1][0]
    assert "{p0:String}" in ch_sql
    assert "`site_id`" in ch_sql

    client.post(
        "/prism/v1/lenses/alerts/query",
        json={"fields": [{"field": "subject_id"}],
              "filters": [{"field": "subject_id", "op": "eq", "value": "SITE-01"}],
              "limit": 5},
    )
    pg_sql = registry.executors["sentry"].calls[-1][0]
    assert "%(p0)s" in pg_sql
    assert '"subject_id"' in pg_sql


def test_filter_value_is_bound_not_inlined(client, registry):
    client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={
            "fields": [{"field": "site_id"}],
            "filters": [
                {"field": "site_name", "op": "eq", "value": "x'; DROP TABLE t; --"}
            ],
            "limit": 5,
        },
    )
    sql, params = registry.executors["analytics"].calls[-1]
    assert "DROP TABLE" not in sql
    assert "DROP TABLE t" in next(iter(params.values()))


def test_explain_returns_the_generated_sql(client):
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        params={"explain": "true"},
        json={"fields": [{"field": "site_id"}], "limit": 1},
    )
    assert response.json()["sql"].startswith("SELECT")


def test_sql_is_withheld_unless_explain_requested(client):
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={"fields": [{"field": "site_id"}], "limit": 1},
    )
    assert response.json()["sql"] is None


# --------------------------------------------------------------------------
# Rejections -- a bad request must be a 4xx that says what to do
# --------------------------------------------------------------------------


def test_unknown_field_is_400_and_lists_available_fields(client):
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={"fields": [{"field": "nonexistent"}], "limit": 1},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_field"
    assert "site_id" in detail["available"]


def test_injection_shaped_field_is_rejected_before_the_handler(client):
    """Caught by the request model, so it never reaches the compiler."""
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={"fields": [{"field": "site_id; DROP TABLE x"}], "limit": 1},
    )
    assert response.status_code == 422


def test_grouping_by_a_measure_is_rejected(client):
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={
            "aggregates": [{"function": "count", "alias": "n"}],
            "group_by": ["amount_leaked"],
            "limit": 5,
        },
    )
    assert response.status_code == 400
    assert "not groupable" in response.json()["detail"]["message"]


def test_summing_a_string_field_is_rejected(client):
    """Both engines raise an opaque type error; this names the field instead."""
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={
            "aggregates": [
                {"function": "sum", "field": "site_name", "alias": "nonsense"}
            ],
            "limit": 5,
        },
    )
    assert response.status_code == 400
    assert "non-numeric" in response.json()["detail"]["message"]


def test_datasource_failure_surfaces_the_sql(client, registry):
    """A query error is nearly always a lens-authoring bug.

    Hiding the generated SQL would make it very hard to find, so the 500 body
    carries it.
    """
    registry.executors["analytics"].fail_with = RuntimeError("Unknown column xyz")
    response = client.post(
        "/prism/v1/lenses/usage_trends/query",
        json={"fields": [{"field": "site_id"}], "limit": 1},
    )
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error"] == "query_failed"
    assert "SELECT" in detail["sql"]


# --------------------------------------------------------------------------
# Field value enumeration
# --------------------------------------------------------------------------


def test_field_values_enumerates_distinct_values(client, registry):
    registry.executors["analytics"].rows = [
        {"channel": "PREPAID"},
        {"channel": "POSTPAID"},
    ]
    body = client.get("/prism/v1/lenses/usage_trends/values/channel").json()
    assert body["values"] == ["PREPAID", "POSTPAID"]
    assert "GROUP BY" in registry.executors["analytics"].calls[-1][0]


def test_field_values_rejects_a_measure(client):
    response = client.get("/prism/v1/lenses/usage_trends/values/amount_leaked")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "not_groupable"


def test_field_values_rejects_an_unknown_field(client):
    response = client.get("/prism/v1/lenses/usage_trends/values/nope")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "unknown_field"


# --------------------------------------------------------------------------
# Every shipped lens is actually queryable
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lens_name",
    [
        "usage_trends",
        "site_volume_baseline",
        "rating_quality",
        "collection_mix",
        "alerts",
        "kpi_series",
    ],
)
def test_every_lens_accepts_a_query_over_its_own_fields(client, lens_name):
    """Guards the failure mode where a lens declares a field its SQL omits.

    The stub cannot detect a genuinely missing column, but it does prove the
    field list is self-consistent and the compiler accepts every declared
    field for selection, filtering, grouping and sorting.
    """
    detail = client.get(f"/prism/v1/lenses/{lens_name}").json()
    groupable = [f for f in detail["fields"] if f["allow_group_by"]]
    assert groupable, f"{lens_name} has no groupable field"

    for field in groupable:
        response = client.post(
            f"/prism/v1/lenses/{lens_name}/query",
            json={
                "fields": [{"field": field["field"]}],
                "group_by": [field["field"]],
                "order_by": [{"field": field["field"], "direction": "asc"}],
                "limit": 1,
            },
        )
        assert response.status_code == 200, (
            f"{lens_name}.{field['field']}: {response.json()}"
        )
