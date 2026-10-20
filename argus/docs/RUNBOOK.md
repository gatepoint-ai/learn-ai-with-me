# Argus runbook

How to start, check, reset and troubleshoot the local stack. All commands run from `argus/`.

## Start

```bash
cp .env.example .env          # then set OPENAI_API_KEY (a free Groq key) -- only the Analyst needs it
docker compose up -d --build
docker compose run --rm tools bootstrap
```

`bootstrap` applies migrations, seeds 30 days of synthetic data with planted faults, ingests the
knowledge base, computes KPIs, runs the rules, and ends with the acceptance gate. Expect
`GATE: PASS`.

Open the dashboard at <http://localhost:3000>. Service APIs listen on `127.0.0.1` only:
Prism 8000, Pulse 8001, Sentry 8002, Analyst 8003, Codex 8004.

## Tasks

| Command | Does |
|---|---|
| `docker compose run --rm tools migrate` | Apply new migrations only |
| `docker compose run --rm tools seed` | Re-seed the synthetic data (dates move to today) |
| `docker compose run --rm tools compute` | Ingest knowledge, recompute KPIs, run rules |
| `docker compose run --rm tools accept` | The acceptance gate |
| `docker compose run --rm tools retrieval-eval` | Measure keyword vs vector vs hybrid search |
| `docker compose run --rm tools analyst-eval` | Ten golden questions for the Analyst (uses the LLM) |

## Change config

Config is baked into images, so a change is a rebuild — seconds, thanks to layer caching:

```bash
docker compose up -d --build <service>      # e.g. sentry after editing config/rules/
docker compose run --rm --build tools compute
```

Every service refuses to start on invalid config and logs every problem at once:
`docker compose logs <service>`.

## Reset

```bash
docker compose down            # stop; data is kept in volumes
docker compose --profile tools down -v   # stop and delete all data -- next bootstrap starts from zero
```

## Troubleshooting

**A service keeps restarting.** `docker compose logs <service>` — almost always a config error,
listed in full in the first log lines.

**The Analyst answers "no_llm_key".** `OPENAI_API_KEY` is empty in `.env`. Set it and run
`docker compose up -d analyst`.

**The Analyst is slow, or returns `llm_failed` 429.** Free LLM tiers limit tokens per minute
(Groq's free tier: 8,000). The client waits as long as the provider asks and retries; a burst of
questions can still exceed it. The Analyst evaluation pauses between questions for this reason
(`ANALYST_EVAL_PAUSE`, default 20 seconds).

**The model is no longer offered.** Providers retire models. List what your key can use:
`curl -H "Authorization: Bearer $KEY" https://api.groq.com/openai/v1/models`, then change
`OPENAI_MODEL` in `.env` and restart the Analyst.

**Config files are missing inside a container.** On some synced drives (Google Drive's virtual
drive, for one), Docker bind mounts appear empty. Argus does not bind-mount config for this
reason — it is copied into each image at build time. If you add a bind mount, check it with
`docker compose exec <service> ls /config`.

**A ClickHouse lens fails with "aggregate function found inside another aggregate".**
ClickHouse resolves a bare column name to a SELECT alias before the table column. Qualify source
columns with a table alias (`a.amount_expected`) inside expressions — see `config/lenses/usage_trends.yaml`.

**Search returns nothing for a sensible question.** Vector-only matches below the collection's
`similarity_threshold` are dropped on purpose, so off-topic questions get no passages. Check the
scores with `curl -X POST localhost:8004/codex/v1/search -d '{"query":"...","collection":"policies"}'`.
