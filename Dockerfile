FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Data volume for SQLite (cursors must survive restarts, M5 acceptance).
VOLUME ["/app/data"]

# Graceful shutdown: let in-flight sends finish and cursors persist.
STOPSIGNAL SIGINT
CMD ["hunter"]
