# One-off tasks: migrations and seeding. Build context is the argus/ root.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY libs/argus-core /app/libs/argus-core
RUN pip install /app/libs/argus-core

COPY datamodel /app/datamodel

# Evaluation questions and other config the tasks read.
COPY config /config
COPY datamodel/tasks.sh /usr/local/bin/argus-task
RUN chmod +x /usr/local/bin/argus-task

ENTRYPOINT ["argus-task"]
CMD ["bootstrap"]
