# AISIMA voice service — Render (Docker, free tier).
#
# Build context is this repo root: pyproject.toml, uv.lock, app/,
# scripts/, plus this Dockerfile. No .env is ever copied or baked
# into the image; all keys arrive as Render environment variables
# (see render.yaml — values are set in the Render dashboard, never
# in this repo).
#
# Render sets $PORT and routes public traffic to it; the container
# must listen on 0.0.0.0:$PORT. Render free tier sleeps after ~15 min
# idle; first open after sleep takes a while to wake.
# No audio is recorded by this service, ever.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app

# curl is only for in-container debugging / health checks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Dependencies first (cached layer): no project install yet, so the
# app/ sources are not needed at this step.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application sources. The service runs from /app, so the project
# itself is not installed into site-packages.
COPY app/ ./app/
COPY scripts/ ./scripts/

EXPOSE 7860

# HOST/PORT come from the environment; Render sets PORT.
# 0.0.0.0 is required — 127.0.0.1 would be unreachable from outside.
CMD ["/bin/sh", "-c", ".venv/bin/python -m uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-7860}"]
