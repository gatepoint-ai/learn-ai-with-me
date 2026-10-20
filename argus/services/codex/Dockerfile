# Build context is the argus/ root -- see docker-compose.yaml.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FASTEMBED_CACHE_PATH=/models

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY libs/argus-core /app/libs/argus-core
RUN pip install /app/libs/argus-core

COPY services/codex/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Model weights are downloaded once, at build time, and baked into the image.
# At runtime Codex needs no network and no key to embed or rerank.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ARG RERANK_MODEL=Xenova/ms-marco-MiniLM-L-6-v2
RUN python -c "from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; TextEmbedding('${EMBEDDING_MODEL}'); TextCrossEncoder('${RERANK_MODEL}')"

COPY services/codex/codex /app/codex

# Config -- including the documents -- is part of the image.
COPY config /config

RUN useradd --create-home --uid 10001 argus \
 && chown -R argus:argus /app /models
USER argus

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "codex.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
