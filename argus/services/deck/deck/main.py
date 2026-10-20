"""Deck -- the dashboard.

Renders the pages defined in `config/deck/views/` from the service APIs:
KPIs from Pulse, alerts from Sentry, answers from the Analyst. It holds no
database credentials and no LLM key. Because it reads the same APIs the
Analyst reads, the dashboard and the chat cannot disagree about a number.
"""

from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from argus_core.config import ConfigError
from argus_core.logging import configure_logging, get_logger
from argus_core.settings import ConfigPaths, LogSettings, ServiceUrls

from .views import Panel, View, load_views

log = get_logger("deck")


class DeckUrls(ServiceUrls):
    analyst_url: str = "http://analyst:8000"


class State:
    views: list[View] = []
    urls: DeckUrls
    http: httpx.Client


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = LogSettings(service_name="deck")
    configure_logging(settings.service_name, settings.log_level, settings.log_json)
    try:
        state.views = load_views(f"{ConfigPaths().config_dir}/deck/views")
    except ConfigError as exc:
        log.error("config_invalid", problems=exc.problems)
        raise
    state.urls = DeckUrls()
    state.http = httpx.Client(timeout=300)
    log.info("deck_ready", views=[v.path for v in state.views])
    yield
    state.http.close()


app = FastAPI(title="Argus Deck", version="0.1.0", lifespan=lifespan)


def _get(url: str) -> Any:
    try:
        r = state.http.get(url)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------

e = html.escape


def panel_kpi_tiles(panel: Panel) -> str:
    kpis = _get(f"{state.urls.pulse_url}/pulse/v1/kpis") or []
    if not kpis:
        return _empty("KPIs not computed yet -- run the bootstrap.")
    tiles = "".join(
        f'<div class="tile s-{e(k["status"])}"><div class="tile-label">{e(k["name"])}</div>'
        f'<div class="tile-value">{e(k["latest_display"] or "n/a")}</div>'
        f'<div class="tile-meta"><span class="badge s-{e(k["status"])}">{e(k["status"])}</span> '
        f'{e(k["latest_period"] or "")}</div></div>'
        for k in kpis
    )
    return f'<div class="tiles">{tiles}</div>'


def panel_kpi_chart(panel: Panel, index: int) -> str:
    data = _get(f"{state.urls.pulse_url}/pulse/v1/kpis/{panel.kpi}/series")
    if not data or not data.get("series"):
        return _empty(f"No data for {panel.kpi}.")
    series = data["series"]
    forecast = data.get("forecast", [])
    payload = {
        "labels": [r["period"] for r in series] + [f["period"] for f in forecast],
        "actual": [r["value"] for r in series] + [None] * len(forecast),
        "anomaly": [r["value"] if r.get("reason") else None for r in series] + [None] * len(forecast),
        "forecast": [None] * (len(series) - 1) + [series[-1]["value"]] + [f["forecast_value"] for f in forecast],
        "lower": [None] * len(series) + [f["confidence_lower"] for f in forecast],
        "upper": [None] * len(series) + [f["confidence_upper"] for f in forecast],
        "unit": data.get("unit", ""),
    }
    reasons = [r["reason"] for r in series if r.get("reason")]
    notes = "".join(f"<li>{e(x)}</li>" for x in reasons[-3:])
    return (
        f'<canvas id="chart{index}" height="110"></canvas>'
        f'<script>window.ARGUS_CHARTS=(window.ARGUS_CHARTS||[]);'
        f'window.ARGUS_CHARTS.push(["chart{index}",{json.dumps(payload)}]);</script>'
        + (f'<ul class="notes">{notes}</ul>' if notes else
           '<p class="muted">No anomalies flagged in this period.</p>')
    )


def panel_alerts_table(panel: Panel) -> str:
    rows = _get(f"{state.urls.sentry_url}/sentry/v1/alerts?status={panel.status}") or []
    if not rows:
        return _empty("Nothing here.")
    body = "".join(
        f'<tr><td>{e(str(r["period"]))}</td><td><span class="badge sev-{e(r["severity"])}">'
        f'{e(r["severity"])}</span></td><td>{e(r.get("control_id") or "")}</td>'
        f'<td>{e(r["subject_id"])}</td><td>{e(r["summary"])}</td><td>{e(r.get("owner") or "")}</td></tr>'
        for r in rows[: panel.limit]
    )
    more = (f'<p class="muted">{len(rows) - panel.limit} more on the Alerts page.</p>'
            if len(rows) > panel.limit else "")
    return ('<table><thead><tr><th>Day</th><th>Severity</th><th>Control</th><th>Subject</th>'
            f'<th>What happened</th><th>Owner</th></tr></thead><tbody>{body}</tbody></table>{more}')


def panel_ask(panel: Panel) -> str:
    chips = "".join(f'<button class="chip" type="button">{e(x)}</button>' for x in panel.examples)
    return f"""
<form id="ask-form" autocomplete="off">
  <input id="ask-input" name="q" placeholder="Ask about leakage, alerts or policy..." required minlength="2">
  <button type="submit">Ask</button>
</form>
<div class="chips">{chips}</div>
<div id="ask-log"></div>"""


def _empty(text: str) -> str:
    return f'<p class="muted">{e(text)}</p>'


def render_panel(panel: Panel, index: int) -> str:
    if panel.type == "kpi_tiles":
        inner = panel_kpi_tiles(panel)
    elif panel.type == "kpi_chart":
        inner = panel_kpi_chart(panel, index)
    elif panel.type == "alerts_table":
        inner = panel_alerts_table(panel)
    else:
        inner = panel_ask(panel)
    wide = " wide" if panel.type in ("kpi_tiles", "alerts_table", "ask") else ""
    return f'<section class="panel{wide}"><h2>{e(panel.title)}</h2>{inner}</section>'


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------


def page(view: View) -> str:
    nav = "".join(
        f'<a href="{e(v.path)}" class="{"active" if v.id == view.id else ""}">{e(v.title)}</a>'
        for v in state.views
    )
    panels = "".join(render_panel(p, i) for i, p in enumerate(view.panels))
    return PAGE.format(title=e(view.title), nav=nav, description=e(view.description), panels=panels)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "views": len(state.views)}


class AskBody(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    conversation_id: str | None = None


@app.post("/api/ask")
def api_ask(body: AskBody) -> JSONResponse:
    """Proxy to the Analyst, so the browser never talks to it directly."""
    try:
        r = state.http.post(f"{state.urls.analyst_url}/analyst/v1/ask", json=body.model_dump(exclude_none=True))
    except httpx.HTTPError as exc:
        raise HTTPException(502, {"message": f"analyst unreachable: {exc}"}) from exc
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/{path:path}", response_class=HTMLResponse)
def view(path: str, request: Request) -> HTMLResponse:
    target = "/" + path.strip("/")
    for v in state.views:
        if v.path.rstrip("/") == target.rstrip("/") or (target == "/" and v.path == "/"):
            return HTMLResponse(page(v))
    raise HTTPException(404, "no such page")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Argus · {title}</title>
<style>
:root {{ --navy:#1F3864; --ink:#1a1a1a; --muted:#5f6b7a; --line:#e3e7ef; --bg:#f6f8fb; --card:#fff;
        --ok:#1E6B35; --okbg:#E8F5EC; --warn:#8a5a00; --warnbg:#fff4dc; --crit:#A3271B; --critbg:#FDECEA; }}
* {{ box-sizing:border-box }}
body {{ margin:0; font-family:"Segoe UI",system-ui,Arial,sans-serif; color:var(--ink); background:var(--bg) }}
header {{ background:var(--navy); color:#fff; padding:14px 24px; display:flex; align-items:center; gap:28px; flex-wrap:wrap }}
header .brand {{ font-weight:700; font-size:20px; letter-spacing:.3px }}
header .brand span {{ font-weight:400; opacity:.75; font-size:14px; margin-left:8px }}
nav a {{ color:#cfd8ea; text-decoration:none; margin-right:18px; font-size:15px }}
nav a.active, nav a:hover {{ color:#fff; border-bottom:2px solid #fff; padding-bottom:3px }}
main {{ max-width:1200px; margin:0 auto; padding:20px 16px 40px }}
.lead {{ color:var(--muted); margin:4px 0 18px }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr)); gap:16px }}
.panel {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; min-width:0 }}
.panel.wide {{ grid-column:1/-1 }}
.panel h2 {{ margin:0 0 12px; font-size:16px; color:var(--navy) }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px }}
.tile {{ border:1px solid var(--line); border-left:5px solid var(--ok); border-radius:10px; padding:10px 12px }}
.tile.s-warning {{ border-left-color:#d4a017 }} .tile.s-critical {{ border-left-color:var(--crit) }}
.tile-label {{ font-size:13px; color:var(--muted) }} .tile-value {{ font-size:24px; font-weight:700; margin:4px 0 }}
.tile-meta {{ font-size:12px; color:var(--muted) }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:20px; font-size:12px; font-weight:600; background:var(--okbg); color:var(--ok) }}
.badge.s-warning,.badge.sev-medium {{ background:var(--warnbg); color:var(--warn) }}
.badge.s-critical,.badge.sev-critical,.badge.sev-high {{ background:var(--critbg); color:var(--crit) }}
.badge.sev-low {{ background:#eef1f6; color:var(--muted) }}
table {{ width:100%; border-collapse:collapse; font-size:14px }}
th {{ text-align:left; color:var(--muted); font-weight:600; border-bottom:1px solid var(--line); padding:6px 8px }}
td {{ border-bottom:1px solid var(--line); padding:7px 8px; vertical-align:top }}
td:first-child, td:nth-child(3) {{ white-space:nowrap }}
.panel {{ overflow-x:auto }}
.notes {{ margin:10px 0 0; padding-left:18px; color:var(--muted); font-size:13px }}
.muted {{ color:var(--muted); font-size:14px }}
form {{ display:flex; gap:8px }} input {{ flex:1; min-width:0; padding:10px 12px; border:1px solid var(--line); border-radius:8px; font-size:15px }}
button {{ background:var(--navy); color:#fff; border:0; border-radius:8px; padding:10px 16px; font-size:15px; cursor:pointer }}
button:disabled {{ opacity:.6; cursor:wait }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 4px }}
.chip {{ background:#eef2f9; color:var(--navy); padding:6px 10px; font-size:13px }}
.turn {{ border-top:1px solid var(--line); margin-top:14px; padding-top:12px }}
.q {{ font-weight:600; margin-bottom:6px }} .a table {{ margin:8px 0 }}
.meta {{ font-size:12px; color:var(--muted); margin-top:8px; display:flex; flex-wrap:wrap; gap:6px 14px }}
.check {{ font-weight:600 }} .check.ok {{ color:var(--ok) }} .check.bad {{ color:var(--crit) }}
footer {{ text-align:center; color:var(--muted); font-size:12px; padding:20px }}
</style></head><body>
<header><div class="brand">Argus<span>revenue assurance, built in public</span></div><nav>{nav}</nav></header>
<main><p class="lead">{description}</p><div class="grid">{panels}</div></main>
<footer>Synthetic demo data · Gatepoint AI · learn-ai-with-me</footer>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script>
(window.ARGUS_CHARTS||[]).forEach(([id, d]) => {{
  const el = document.getElementById(id); if (!el || !window.Chart) return;
  new Chart(el, {{ type:'line', data:{{ labels:d.labels, datasets:[
    {{ label:'Actual', data:d.actual, borderColor:'#1F3864', backgroundColor:'#1F3864', pointRadius:2, tension:.25 }},
    {{ label:'Flagged', data:d.anomaly, borderColor:'#A3271B', backgroundColor:'#A3271B', pointRadius:6, showLine:false }},
    {{ label:'Forecast', data:d.forecast, borderColor:'#8A97B0', borderDash:[6,4], pointRadius:0, tension:.25 }},
    {{ label:'Band', data:d.upper, borderColor:'transparent', backgroundColor:'rgba(138,151,176,.18)', pointRadius:0, fill:'+1' }},
    {{ label:'Band low', data:d.lower, borderColor:'transparent', pointRadius:0, fill:false }} ]}},
    options:{{ responsive:true, plugins:{{ legend:{{ labels:{{ filter:i => i.text !== 'Band low' }} }} }},
      scales:{{ y:{{ title:{{ display:true, text:d.unit }} }} }} }} }});
}});
const form = document.getElementById('ask-form');
if (form) {{
  let conversation = null;
  const input = document.getElementById('ask-input'), log = document.getElementById('ask-log');
  document.querySelectorAll('.chip').forEach(c => c.onclick = () => {{ input.value = c.textContent; form.requestSubmit(); }});
  const esc = s => s.replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
  form.onsubmit = async ev => {{
    ev.preventDefault(); const q = input.value.trim(); if (!q) return;
    const btn = form.querySelector('button'); btn.disabled = true; btn.textContent = 'Thinking...';
    const turn = document.createElement('div'); turn.className = 'turn';
    turn.innerHTML = '<div class="q">' + esc(q) + '</div><div class="a muted">Working through the tools...</div>';
    log.prepend(turn);
    try {{
      const r = await fetch('/api/ask', {{ method:'POST', headers:{{'content-type':'application/json'}},
        body: JSON.stringify({{ question:q, conversation_id:conversation }}) }});
      const d = await r.json();
      if (!r.ok) throw new Error((d.detail && d.detail.message) || 'request failed');
      conversation = d.conversation_id;
      const n = d.numbers || {{ok:true, checked:0, unverified:[]}};
      const check = n.ok ? '<span class="check ok">&#10003; ' + n.checked + ' figures verified against the data</span>'
                         : '<span class="check bad">&#9888; unverified figures: ' + esc(n.unverified.join(', ')) + '</span>';
      const tools = (d.tools_used || []).map(t => t.name).join(' &rarr; ') || 'none';
      const src = (d.sources || []).map(esc).join('; ');
      turn.querySelector('.a').className = 'a';
      turn.querySelector('.a').innerHTML = DOMPurify.sanitize(marked.parse(d.answer || ''));
      turn.insertAdjacentHTML('beforeend', '<div class="meta">' + check + '<span>tools: ' + tools + '</span>' +
        (src ? '<span>sources: ' + src + '</span>' : '') + '<span>' + esc(d.model) + '</span></div>');
    }} catch (err) {{ turn.querySelector('.a').textContent = 'Error: ' + err.message; }}
    btn.disabled = false; btn.textContent = 'Ask'; input.value = '';
  }};
}}
</script></body></html>"""
