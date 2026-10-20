# Build context is the REPO ROOT, not this directory -- the shared library at
# libs/argus-core has to be copyable. See docker-compose.yaml.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is here for the container healthcheck, not for the application.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# The shared library is copied and installed before the service code so that
# editing a handler does not invalidate the dependency layer.
COPY libs/argus-core /app/libs/argus-core
RUN pip install /app/libs/argus-core

COPY services/deck/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY services/deck/deck /app/deck

# Config is part of the image: what was tested is exactly what runs.
COPY config /config

# Unprivileged. The Deck holds no credentials at all: every number on the page
# comes from a service API.
RUN useradd --create-home --uid 10001 argus \
 && chown -R argus:argus /app
USER argus

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "deck.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
