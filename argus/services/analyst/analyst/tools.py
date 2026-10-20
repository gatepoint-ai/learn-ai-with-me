"""The Analyst's tools -- what it is given, and what it is deliberately not.

Two families, kept apart on purpose:

* **Data tools** read finished numbers: lenses through Prism, KPIs from Pulse,
  alerts from Sentry. They answer "how much", "where", "when".
* **The knowledge tool** reads policy passages from Codex. It answers "how does
  the process work", "what does the policy say", "who owns it".

A model that is not told the difference will answer a policy question from a
stale number, or a number question from a policy document. Each description
therefore says when to use the tool and when NOT to.

Every tool is a read. The list at the bottom names the capabilities the agent
is denied, each with the failure the denial prevents. "We told the model not
to" is not a control; "the model cannot" is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

#: Rows handed to the model per tool call. A lens can return thousands; the
#: model needs a sample and the count, not a context window full of rows.
MAX_ROWS = 40
MAX_CHARS = 8000


@dataclass(frozen=True)
class Tool:
    name: str
    family: str  # "data" | "knowledge"
    description: str
    parameters: dict[str, Any]
    call: Callable[[dict[str, Any]], Any]

    def schema(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


DENIED: list[dict[str, str]] = [
    {"capability": "Run the rules or recalculate KPIs",
     "prevents": "An answer that changes the numbers it is explaining."},
    {"capability": "Open, close or edit an alert",
     "prevents": "The model marking its own findings as resolved -- grading its own work."},
    {"capability": "Write SQL or reach a database",
     "prevents": "Invented columns, bypassed field whitelists, and two answers to one question."},
    {"capability": "Change config: lenses, KPIs, rules, thresholds",
     "prevents": "A prompt-injected instruction quietly moving a threshold."},
    {"capability": "Browse the web",
     "prevents": "Policy answers with no source inside the organisation."},
    {"capability": "Calculate figures",
     "prevents": "Arithmetic errors in a finance answer. Enforced after the fact: every number "
                 "in an answer is checked against the tool results."},
]


def truncate(payload: Any) -> Any:
    """Tool-result middleware: cap rows and size before the model sees them."""
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        rows = payload["rows"]
        if len(rows) > MAX_ROWS:
            payload = {**payload, "rows": rows[:MAX_ROWS], "rows_omitted": len(rows) - MAX_ROWS,
                       "note": f"showing {MAX_ROWS} of {len(rows)} rows; filter or aggregate for the rest"}
    if isinstance(payload, list) and len(payload) > MAX_ROWS:
        payload = {"items": payload[:MAX_ROWS], "count": len(payload),
                   "note": f"showing {MAX_ROWS} of {len(payload)} items"}
    text = json.dumps(payload, default=str)
    if len(text) > MAX_CHARS:
        return {"truncated": True, "preview": text[:MAX_CHARS],
                "note": "result too large; narrow the query"}
    return payload


class Toolbox:
    def __init__(self, prism: str, pulse: str, sentry: str, codex: str, timeout: float = 60) -> None:
        self.http = httpx.Client(timeout=timeout)
        self.urls = {"prism": prism, "pulse": pulse, "sentry": sentry, "codex": codex}
        self.tools = {t.name: t for t in self._build()}

    def close(self) -> None:
        self.http.close()

    def _get(self, svc: str, path: str, **params: Any) -> Any:
        r = self.http.get(self.urls[svc] + path, params={k: v for k, v in params.items() if v is not None})
        return self._result(r)

    def _post(self, svc: str, path: str, body: dict[str, Any]) -> Any:
        return self._result(self.http.post(self.urls[svc] + path, json=body))

    @staticmethod
    def _result(r: httpx.Response) -> Any:
        body = r.json()
        if r.status_code >= 400:
            # Errors go back to the model as data so it can correct itself --
            # e.g. "unknown field; known fields: ...".
            return {"error": body.get("detail", body)}
        return body

    def run(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"no tool named {name!r}; available: {sorted(self.tools)}"}
        return truncate(tool.call(arguments))

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    # ------------------------------------------------------------------

    def _build(self) -> list[Tool]:
        return [
            Tool(
                "list_lenses", "data",
                "List the data views (lenses) available for querying, with what each contains. "
                "Call this first when you need data and do not know which lens holds it.",
                {"type": "object", "properties": {}},
                lambda a: [{k: l[k] for k in ("name", "label", "description", "tags")}
                           for l in self._get("prism", "/prism/v1/lenses")],
            ),
            Tool(
                "describe_lens", "data",
                "Get the fields of one lens: names, types, labels, which can be filtered and "
                "grouped. Call before query_lens -- only declared fields can be queried.",
                {"type": "object", "properties": {"lens": {"type": "string"}}, "required": ["lens"]},
                lambda a: self._get("prism", f"/prism/v1/lenses/{a['lens']}"),
            ),
            Tool(
                "query_lens", "data",
                "Query a lens for live figures: volumes, revenue, leakage, rates, by site, class, "
                "channel or day. Describe the query; the platform writes the SQL. Use aggregates "
                "(sum, count, avg, min, max) for totals -- never add numbers up yourself. "
                "Do NOT use for policy or process questions.",
                {
                    "type": "object",
                    "properties": {
                        "lens": {"type": "string"},
                        "fields": {"type": "array", "items": {"type": "object", "properties": {
                            "field": {"type": "string"}}, "required": ["field"]}},
                        "aggregates": {"type": "array", "items": {"type": "object", "properties": {
                            "function": {"type": "string", "enum": ["sum", "count", "count_distinct", "avg", "min", "max"]},
                            "field": {"type": "string"}, "alias": {"type": "string"}},
                            "required": ["function", "alias"]}},
                        "filters": {"type": "array", "items": {"type": "object", "properties": {
                            "field": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "between", "is_null", "is_not_null"]},
                            "value": {}}, "required": ["field", "op"]}},
                        "group_by": {"type": "array", "items": {"type": "string"}},
                        "order_by": {"type": "array", "items": {"type": "object", "properties": {
                            "field": {"type": "string"}, "direction": {"type": "string", "enum": ["asc", "desc"]}},
                            "required": ["field"]}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["lens"],
                },
                self._query_lens,
            ),
            Tool(
                "get_kpis", "data",
                "Latest value and status (ok / warning / critical) of every KPI: leakage rate, "
                "event volume, collected revenue, mis-rating rate, unbilled rate.",
                {"type": "object", "properties": {}},
                lambda a: self._get("pulse", "/pulse/v1/kpis"),
            ),
            Tool(
                "get_kpi_series", "data",
                "Daily values of one KPI, with flagged anomalies (and their plain-language reason) "
                "and a 7-day forecast. Use for trends and 'was this unusual' questions.",
                {"type": "object", "properties": {
                    "kpi_id": {"type": "string"},
                    "start": {"type": "string", "description": "YYYY-MM-DD"},
                    "end": {"type": "string", "description": "YYYY-MM-DD"}},
                 "required": ["kpi_id"]},
                lambda a: self._kpi_series(a),
            ),
            Tool(
                "get_alerts", "data",
                "Alerts raised by the control rules, each with a one-sentence explanation. "
                "status: open (default), suppressed (systemic events), closed, or all.",
                {"type": "object", "properties": {
                    "status": {"type": "string", "enum": ["open", "suppressed", "closed", "all"]},
                    "since": {"type": "string", "description": "YYYY-MM-DD"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]}}},
                lambda a: self._alerts(a),
            ),
            Tool(
                "search_knowledge", "knowledge",
                "Search the organisation's policies, procedures and control library. Use when the "
                "question is HOW a process works, WHY a control exists, WHAT the policy says or WHO "
                "is responsible. Do NOT use for live figures -- use the data tools. Returns passages "
                "with a citation; quote the citation in your answer. If it returns nothing, say the "
                "documents do not cover it.",
                {"type": "object", "properties": {
                    "query": {"type": "string"},
                    "collection": {"type": "string", "enum": ["policies", "controls"]}},
                 "required": ["query"]},
                lambda a: self._post("codex", "/codex/v1/search",
                                     {"query": a["query"], "collection": a.get("collection"), "top_k": 4}),
            ),
        ]

    def _query_lens(self, a: dict[str, Any]) -> Any:
        body = {k: a[k] for k in ("fields", "aggregates", "filters", "group_by", "order_by") if a.get(k)}
        body["limit"] = min(int(a.get("limit") or 50), 200)
        result = self._post("prism", f"/prism/v1/lenses/{a['lens']}/query", body)
        if isinstance(result, dict) and "rows" in result:
            return {"lens": a["lens"], "row_count": result["row_count"], "rows": result["rows"]}
        return result

    def _kpi_series(self, a: dict[str, Any]) -> Any:
        data = self._get("pulse", f"/pulse/v1/kpis/{a['kpi_id']}/series", start=a.get("start"), end=a.get("end"))
        if isinstance(data, dict) and "series" in data:
            data["rows"] = data.pop("series")
            data["anomalies"] = [r for r in data["rows"] if r.get("reason")]
        return data

    def _alerts(self, a: dict[str, Any]) -> Any:
        rows = self._get("sentry", "/sentry/v1/alerts", status=a.get("status", "open"),
                         since=a.get("since"), severity=a.get("severity"))
        if isinstance(rows, list):
            keep = ("period", "probe_name", "rule_name", "subject_id", "severity", "status", "summary")
            return {"count": len(rows), "rows": [{k: r.get(k) for k in keep} for r in rows]}
        return rows
