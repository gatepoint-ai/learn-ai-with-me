# Argus

An AI revenue-assurance platform, **built in public**. It ingests usage events, computes KPIs,
detects revenue leakage with configurable rules, and answers natural-language questions grounded
in both live data and a policy knowledge base.

Everything is config-driven: adding a metric, a check, a data view or a dashboard page means adding
a YAML file, not writing code.

> **Status — October 2026: detection runs end to end.** The Docker stack (ClickHouse, Postgres,
> pgvector, Valkey), Prism (query layer), Pulse (KPIs) and Sentry (rules) run on synthetic data
> with planted faults. The acceptance gate passes: every planted fault caught, the negative control
> suppressed, zero unexplained alerts. Codex (the knowledge base) answers with hybrid search,
> measured on its own questions. 162 unit tests. The Analyst and the dashboard are the roadmap below. This README says which parts run today and which are still the plan.

I'm [Maicon Abreu](https://www.linkedin.com/in/mbdabreu/) — 20 years in revenue assurance and fraud,
now delivering data and AI programs. Argus is the system I wish I'd had: the controls I've worked
with for two decades, rebuilt around a language model that is allowed to explain and never allowed
to calculate. Each piece is explained in a lesson in this repo —
see the [lesson index](../README.md).

## Run the stack — Docker, no API key

```bash
cd argus
docker compose up -d --build                 # datastores + Prism + Pulse + Sentry
docker compose run --rm tools bootstrap      # migrate, seed, compute KPIs, run rules, acceptance gate

curl localhost:8001/pulse/v1/kpis            # KPI values and status
curl localhost:8002/sentry/v1/alerts         # open alerts, each explained in one sentence
```

The bootstrap ends with the acceptance gate:

```text
  PASS  feed_outage       2026-10-01  expect alert      PR-FEED-COMPLETENESS on SITE-03: open
  PASS  volume_spike      2026-10-08  expect alert      PR-VOLUME-SPIKE on SITE-06: open
  PASS  misrating         2026-10-11  expect alert      PR-RATING-ACCURACY on ENTERPRISE: open
  ...
  PASS  systemic_dip      2026-10-04  expect suppressed PR-FEED-COMPLETENESS: 8 breaches, 8 suppressed, 0 open
  No unexplained open alerts.
  10/10 checks passed (pass rate 1.00), 0 unexplained alerts
  GATE: PASS
```

Ask Prism directly — the query is a description, never SQL:

```bash
curl -X POST localhost:8000/prism/v1/lenses/site_volume_baseline/query   -H 'content-type: application/json'   -d '{"fields":[{"field":"usage_date"},{"field":"site_id"},{"field":"volume_change_ratio"}],
       "filters":[{"field":"volume_change_ratio","op":"lt","value":-0.5}]}'
```

## Try it without Docker — no API key

```bash
cd argus
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e "libs/argus-core[test]" -r services/prism/requirements.txt -r services/pulse/requirements.txt

python -m pytest tests                               # 162 unit tests, no network needed
python datamodel/seed/generate.py --days 30 --dry-run   # synthetic data + planted faults, no database
```

The seeder's dry run prints the four faults it plants. In the full stack, the rule engine has to
find every one of them.

## Why "Argus"

Argus Panoptes was the hundred-eyed watchman who never slept with every eye at once. Revenue
assurance is the same job: watch a large surface continuously and notice the one thing that changed.

## Architecture

Six services over four datastores. The service names follow the watchman theme, and each is named
for what it does.

| Service | Port | Responsibility | Status |
|---|---|---|---|
| **prism** | 8000 | Query layer. Serves **lenses** — named, validated SQL views with a closed field list. | ✅ built, tested |
| **pulse** | 8001 | KPI engine. Computes series, breakdowns, forecasts and anomalies. | ✅ built, tested |
| **sentry** | 8002 | Rule engine. Runs **probes** against lenses and raises **alerts**. | ✅ built, tested |
| **codex** | 8004 | Knowledge base. Chunks, embeds and retrieves policy documents. | ✅ built, measured |
| **analyst** | 8003 | AI agent. Answers questions using lens queries *and* Codex retrieval. | 🔜 next |
| **deck** | 3000 | Dashboard. Renders YAML-defined views and panels. | 🔜 planned |

```text
   browser ──► deck (Next.js)
                 │  server-side route handlers
     ┌───────────┼────────────┬───────────┐
     ▼           ▼            ▼           ▼
   prism       pulse       sentry      analyst ──► codex
     │           │            │           │          │
     ▼           └── via prism ┘           └── tools ─┘
 ClickHouse   ┌─────────────────┐      ┌──────────┐
  analytics   │ Postgres        │      │ pgvector │
   + Valkey   │ pulse / sentry  │      │  codex   │
              │ / analyst       │      └──────────┘
              └─────────────────┘
```

Two rules carry the design:

**1. Everything queries through Prism.** Pulse and Sentry do not hold database credentials for the
analytics store — they ask Prism. One query path means one cache, one audit point, and one place
where SQL is generated.

**2. Data and knowledge are separate tool families.** The Analyst answers "how much leaked last
month" from a lens and "what is our write-off policy" from Codex. Their tool descriptions say so
explicitly, because models that are not told the difference will answer policy questions from stale
numbers.

## What's in the repo today

| Path | What it is |
|---|---|
| `libs/argus-core/` | Shared core: settings, datastore clients, config schemas, and the SQL compiler with the field whitelist |
| `services/prism/` | The query layer — FastAPI service, Dockerfile |
| `services/pulse/` | The KPI engine — pure, tested arithmetic in `engine.py`; reads only through Prism |
| `services/sentry/` | The rule engine — per-subject probes, systemic suppression, self-closing alerts |
| `config/rules/` | The controls: feed completeness, volume spike, rating accuracy, write-off drift |
| `datamodel/acceptance.py` | The gate: every planted fault caught, the negative control suppressed |
| `services/codex/` | The knowledge base — heading-aware chunking, local embeddings, hybrid search |
| `config/codex/` | Collections, five fictional policy documents, and the retrieval evaluation questions |
| `config/kpis/` | Five KPI definitions: leakage, volume, collected revenue, mis-rating, unbilled |
| `docker-compose.yaml` | The local stack; `tools` runs migrations, seeding and computation |
| `config/lenses/` | Six lens definitions (usage trends, rating quality, collection mix, alerts, …) |
| `datamodel/migrations/` | Schemas for ClickHouse (analytics), Postgres (pulse, sentry, analyst) and pgvector (codex) |
| `datamodel/migrate.py` | Migration runner for all three engines |
| `datamodel/seed/` | Synthetic-data generator and the fault manifest |
| `tests/unit/` | 162 tests: SQL builder and injection payloads, migration splitter, seeder, Prism API, KPI and rule engines, chunking and fusion |

## Concepts

| Term | What it is |
|---|---|
| **lens** | A named SQL view plus a closed list of queryable fields. `config/lenses/*.yaml` |
| **probe** | One threshold test over a lens. The executable unit of a rule. |
| **rule** | A group of related probes with an owner and a process. |
| **alert** | A probe breach, attributed to a subject (a site, a customer class). |
| **subject** | Whatever an alert is about — the entity the probe groups by. |
| **KPI** | A measure over a lens at a time grain, with thresholds. |
| **collection** | A retrievable document corpus. |
| **view / panel** | A dashboard page and the widgets on it. |

## The field whitelist

A lens declares its fields. `argus_core.sqlbuilder` resolves **every** identifier through that list
before it reaches SQL, and binds every literal as a parameter. A field the lens does not declare
cannot be selected, filtered, grouped or sorted — there is no path from a request string to a SQL
identifier.

This is the platform's whole injection surface, so it is tested first and hard: 14
identifier-shaped attack payloads, hostile filter values proven bound not inlined, and both dialects
covered.

## Synthetic data has faults on purpose

The seeder plants four documented faults **and one negative control**, and writes a manifest
describing them:

| Fault | Shape | Probe expected to catch it |
|---|---|---|
| `feed_outage` | one site loses ~85% of a day's volume | `PR-FEED-COMPLETENESS` |
| `volume_spike` | one site doubles for a day | `PR-VOLUME-SPIKE` |
| `misrating` | one customer class mis-rated for 3 days | `PR-RATING-ACCURACY` |
| `collection_shift` | one site's write-off share jumps for 4 days | `PR-COLLECTION-DRIFT` |
| `systemic_dip` | **every** site loses ~70% on one day, like a public holiday | must be **suppressed**, not alerted |

The manifest is the contract between the seeder and Sentry: the acceptance gate
([`datamodel/acceptance.py`](datamodel/acceptance.py)) reads it and checks every fault on every
day. A seeder that produced clean data would prove only that the stack starts — "no alerts" and
"detector broken" would look identical. The negative control proves the opposite failure is caught
too: alerting on everything is not a pass.

The gate fails closed. Zero cases is a failure, not a vacuous pass; every fault date is checked,
not a sample; and an open alert the manifest cannot explain fails the run.

## Hybrid search, measured on our own documents

Codex searches five fictional policy documents (write-offs, collections, rating changes,
escalation, the control library) two ways and blends the scores. Before choosing a weighting,
we measured — 16 questions, half naming an exact ID, half in plain English:

| Retriever | Exact-ID questions | Plain-English questions | All |
|---|---|---|---|
| Keyword only | **8/8** | 5/8 | 13/16 |
| Vector only | 5/8 | **7/8** | 12/16 |
| **Hybrid** | **8/8** | **7/8** | **15/16** |

Each retriever fails on a different kind of question, and the blend fixes both. Embeddings put
`CTL-REV-003` and `CTL-REV-004` almost on top of each other; keyword search finds the exact token.
A cross-encoder reranker added nothing on this corpus, so it is off by default. Off-topic
questions return no passages at all, so the Analyst can say "I don't know". Run it yourself:
`docker compose run --rm tools retrieval-eval`.

## LLM providers (planned for the Analyst)

The design uses two provider implementations to reach about ten backends, because nearly every
free LLM host speaks the OpenAI chat-completions wire format including tool calling. Switching
backend is a `base_url` change, not a code change.

Default will be **Groq** — free, no credit card, fast enough for a real agent loop. **Claude** for the
best tool-use quality. Ollama for fully offline use with no key. `.env.example` already carries the
settings.

Embeddings are deliberately **independent** of the chat provider: retrieval runs locally on ONNX via
`fastembed` — no key, no network, and no multi-gigabyte torch layer in the image.

## Roadmap

1. ✅ **Pulse** — KPI series and anomalies, reading through Prism.
2. ✅ **Docker stack** — `docker compose` with a `tools` container for migrate / seed / compute.
3. ✅ **Sentry** — probes and alerts; the acceptance gate that checks every planted fault is caught.
4. ✅ **Codex** — document chunking, local embeddings, hybrid retrieval, measured.
5. **Analyst** — the AI agent, with data tools and knowledge tools kept apart.
6. **Deck** — the dashboard.
7. **Docs** — architecture, data model, config reference, runbook and decision records.

## License

Code under the [MIT License](../LICENSE). All data in this folder is synthetic.

---

Published by **Gatepoint AI** · Follow the build on [LinkedIn](https://www.linkedin.com/in/mbdabreu/).
