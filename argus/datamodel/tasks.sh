#!/bin/sh
# Entry point for `docker compose run --rm tools <task>`.
set -e
cd /app
case "${1:-bootstrap}" in
  migrate)   python datamodel/migrate.py up ;;
  status)    python datamodel/migrate.py status ;;
  seed)      python datamodel/seed/generate.py --days "${SEED_DAYS:-30}" ;;
  compute)   python datamodel/compute.py ;;
  accept)    python datamodel/acceptance.py ;;
  retrieval-eval) python datamodel/retrieval_eval.py ;;
  analyst-eval)   python datamodel/analyst_eval.py ;;
  bootstrap) python datamodel/migrate.py up
             python datamodel/seed/generate.py --days "${SEED_DAYS:-30}"
             python datamodel/compute.py
             python datamodel/acceptance.py ;;
  *)         exec "$@" ;;
esac
