# Argus architecture

Argus is a revenue-assurance platform: usage data in, KPIs and leakage alerts out, and an AI
analyst that explains them. This page describes how the pieces fit and **why** each boundary is
where it is. Everything here is built and running in v1.

## The shape

```text
                 browser
                    │
                  deck  (dashboard; holds no credentials)
         ┌──────────┼──────────────┬──────────────┐
         ▼          ▼              ▼              ▼
       pulse      sentry        analyst ───────► codex
      (KPIs)     (rules)      (AI agent)      (knowledge)
         │          │           │   │              │
         └── via ───┴── via ────┘   │              │
              prism (query layer)   │              │
                    │               │              │
        ┌───────────┴─────────┐     │              │
        ▼                     ▼     ▼              ▼
   ClickHouse            Postgres                pgvector
   analytics       pulse · sentry · analyst      codex
   (+ Valkey cache)
```

| Service | Owns | Reads | Writes |
|---|---|---|---|
| **prism** | The lens catalogue and the only SQL generator | ClickHouse, Postgres (read) | nothing |
| **pulse** | KPI values, breakdowns, anomalies, forecasts | Prism | `pulse` database |
| **sentry** | Rules, probes, alerts | Prism | `sentry` database |
| **codex** | Documents, chunks, embeddings | its own store | `codex` database |
| **analyst** | Conversations and the tool-call audit | Prism, Pulse, Sentry, Codex (HTTP) | `analyst` database |
| **deck** | Page definitions | Pulse, Sentry, Analyst (HTTP) | nothing |

## Rule 1 — everything queries through Prism

Pulse, Sentry and the Analyst never hold analytics credentials. They send Prism a *description*
of a query — a lens, fields, filters, aggregates — and Prism compiles it.

Why it matters:

- **One place generates SQL**, so there is one injection surface. `argus_core.sqlbuilder` resolves
  every identifier through the lens's declared field list and binds every value as a parameter.
  A field a lens does not declare cannot be selected, filtered, grouped or sorted.
- **One cache and one audit point** for every read.
- **The dashboard and the chat cannot disagree**: both read the same lens definitions.

## Rule 2 — data and knowledge are separate tool families

The Analyst answers "how much leaked" from data tools and "what does the policy say" from the
knowledge tool. Each tool's description says when to use it *and when not to*. A model that is
not told the difference will answer a policy question from a stale number.

## Rule 3 — the model never calculates

Every figure an executive reads is computed in SQL (Prism) or deterministic Python (Pulse,
Sentry). The Analyst only reads finished numbers. After it answers, `analyst/verify.py` extracts
every figure from the answer and looks for it in the tool results and the question; anything else
is reported as unverified and shown to the reader.

## Rule 4 — constraints live in the architecture, not the prompt

The Analyst's tools are all reads. What it is **denied** is declared in code
(`analyst/tools.py → DENIED`), each with the failure it prevents:

| Denied | Prevents |
|---|---|
| Run the rules or recalculate KPIs | An answer that changes the numbers it is explaining |
| Open, close or edit an alert | The model grading its own work |
| Write SQL or reach a database | Invented columns, bypassed whitelists, two answers to one question |
| Change config | A prompt-injected instruction moving a threshold |
| Browse the web | Policy answers with no source inside the organisation |
| Calculate figures | Arithmetic errors in a finance answer (checked after the fact) |

Also bounded in code: a maximum number of tool rounds, result truncation before the model sees
a tool's output, a note instead of the same data when the model repeats a call, and a final
no-tools round that answers from what was gathered when the budget runs out.

## Detection in two layers

- **Pulse** watches network-wide KPIs with a direction-aware anomaly test against each point's
  own trailing history. Good at "is the whole business moving".
- **Sentry** judges every site or customer class separately with fixed rules. A fault at one
  site is diluted into noise at network level — in the demo, network KPIs catch one of the four
  planted faults and the per-subject rules catch all four.

Sentry also has a **minimum-history rule** (no verdict on a baseline of one or two days) and
**systemic suppression**: if most subjects breach on the same day, the breaches are kept as
`suppressed` with their reason — a holiday or an upstream outage is one event, not eight faults.
Alerts **close themselves** when a re-run no longer finds the breach.

## Knowledge: hybrid retrieval, measured

Codex chunks Markdown on its headings (so every hit cites *document › section*), embeds locally
with ONNX (no key, no network), and fuses pgvector similarity with Postgres full-text rank by a
per-collection weight. The weights and thresholds were set by measurement — see the retrieval
evaluation in the README.

## Configuration over code

Lenses, KPIs, rules, knowledge collections and dashboard pages are YAML under `config/`. Every
service validates its config at startup and refuses to start on an error, reporting every
problem at once. Config is baked into each image at build time: the image is the deployable unit.

## Verification

| Check | What it proves | Command |
|---|---|---|
| Unit tests | The arithmetic, the SQL compiler, chunking, fusion, the number check | `pytest tests` |
| Acceptance gate | Every planted fault caught, the negative control suppressed, nothing unexplained | `tools accept` |
| Retrieval evaluation | Which retriever finds what, on our own questions | `tools retrieval-eval` |
| Analyst evaluation | Routing, citations, verified figures, and the three traps | `tools analyst-eval` |
