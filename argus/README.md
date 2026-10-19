# Argus

An AI revenue-assurance platform, **built in public**. It ingests usage events, computes KPIs,
detects revenue leakage with configurable rules, and answers natural-language questions grounded
in both live data and a policy knowledge base.

Everything is config-driven: adding a metric, a check, a data view or a dashboard page means adding
a YAML file, not writing code.

> **Status — October 2026: the data layer runs end to end.** The Docker stack (ClickHouse,
> Postgres, pgvector, Valkey), Prism (the query layer) and Pulse (the KPI engine) run on synthetic
> data with planted faults, covered by 129 unit tests. Sentry, Codex, the Analyst and the
> dashboard are the roadmap below. This README says which parts run today and which are still the plan.

I'm [Maicon Abreu](https://www.linkedin.com/in/mbdabreu/) — 20 years in revenue assurance and fraud,
now delivering data and AI programs. Argus is the system I wish I'd had: the controls I've worked
with for two decades, rebuilt around a language model that is allowed to explain and never allowed
to calculate. Each piece is explained in a lesson in this repo —
see the [lesson index](../README.md).

## Run the stack — Docker, no API key

```bash
cd argus
docker compose up -d --build                 # datastores + Prism + Pulse
docker compose run --rm tools bootstrap      # migrate, seed 30 days with 4 planted faults, compute KPIs

curl localhost:8001/pulse/v1/kpis            # KPI values and status
curl localhost:8001/pulse/v1/anomalies       # what the KPI engine flagged, in plain sentences
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

python -m pytest tests                               # 129 unit tests, no network needed
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
| **sentry** | 8002 | Rule engine. Runs **probes** against lenses and raises **alerts**. | 🔜 next |
| **codex** | 8004 | Knowledge base. Chunks, embeds and retrieves policy documents. | 🔜 planned (schema ✅) |
| **analyst** | 8003 | AI agent. Answers questions using lens queries *and* Codex retrieval. | 🔜 planned |
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
| `config/kpis/` | Five KPI definitions: leakage, volume, collected revenue, mis-rating, unbilled |
| `docker-compose.yaml` | The local stack; `tools` runs migrations, seeding and computation |
| `config/lenses/` | Six lens definitions (usage trends, rating quality, collection mix, alerts, …) |
| `datamodel/migrations/` | Schemas for ClickHouse (analytics), Postgres (pulse, sentry, analyst) and pgvector (codex) |
| `datamodel/migrate.py` | Migration runner for all three engines |
| `datamodel/seed/` | Synthetic-data generator and the fault manifest |
| `tests/unit/` | 129 tests: SQL builder and injection payloads, migration splitter, seeder, Prism API, KPI engine |

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

The seeder plants four documented faults and writes `datamodel/seed/fault-manifest.json` describing
them:

| Fault | Shape | Probe expected to catch it |
|---|---|---|
| `feed_outage` | one site loses ~85% of a day's volume | `PR-FEED-COMPLETENESS` |
| `volume_spike` | one site doubles for a day | `PR-VOLUME-SPIKE` |
| `misrating` | one customer class mis-rated for 3 days | `PR-RATING-ACCURACY` |
| `collection_shift` | one site's write-off share jumps for 4 days | `PR-COLLECTION-DRIFT` |

The manifest is the contract between the seeder and Sentry: the acceptance test will read it and
assert an alert exists for every entry. A seeder that produced clean data would prove only that the
stack starts — "no alerts" and "detector broken" would look identical.

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
3. **Sentry** — probes and alerts; the acceptance test that checks every planted fault is caught.
4. **Codex** — document chunking, local embeddings, retrieval.
5. **Analyst** — the AI agent, with data tools and knowledge tools kept apart.
6. **Deck** — the dashboard.
7. **Docs** — architecture, data model, config reference, runbook and decision records.

## License

Code under the [MIT License](../LICENSE). All data in this folder is synthetic.

---

Published by **Gatepoint AI** · Follow the build on [LinkedIn](https://www.linkedin.com/in/mbdabreu/).
