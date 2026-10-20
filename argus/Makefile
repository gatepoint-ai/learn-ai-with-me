# Shortcuts for macOS / Linux. On Windows, run the docker compose commands directly
# (see README.md) -- they are all this file does.

.PHONY: up bootstrap test accept evals logs down reset

up:            ## build and start every service
	docker compose up -d --build

bootstrap:     ## migrate, seed, ingest, compute, run rules, acceptance gate
	docker compose run --rm tools bootstrap

test:          ## unit tests (no Docker needed)
	python -m pytest tests

accept:        ## the acceptance gate on its own
	docker compose run --rm tools accept

evals:         ## retrieval and analyst evaluations (the analyst one uses the LLM)
	docker compose run --rm tools retrieval-eval
	docker compose run --rm tools analyst-eval

logs:          ## follow one service: make logs s=sentry
	docker compose logs -f $(s)

down:          ## stop, keep data
	docker compose down

reset:         ## stop and delete all data
	docker compose --profile tools down -v
