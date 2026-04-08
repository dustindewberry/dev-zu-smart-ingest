# syntax=docker/dockerfile:1.6
#
# zubot-ingestion — multi-stage build
# CAP-002: containerize the FastAPI + Celery service
#
# Stage 1 (builder): install Python dependencies into an isolated venv at /opt/venv.
# Stage 2 (runtime): copy the venv and source into a slim image, run as uid 1000.

# ----------------------------------------------------------------------
# Stage 1: builder
# ----------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Build-time system packages required to compile native Python wheels
# (psycopg/asyncpg, pymupdf, lxml, pillow, etc.).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
        libjpeg-dev \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create the isolated virtualenv that we will copy into the runtime image.
RUN python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --upgrade pip setuptools wheel

WORKDIR /build

# Install dependencies first so this layer is cached when only source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ----------------------------------------------------------------------
# Stage 2: runtime
# ----------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    ZUBOT_HOST=0.0.0.0 \
    ZUBOT_PORT=4243

# Runtime-only system packages: curl is needed by the HEALTHCHECK; the
# remaining libs are runtime shared objects required by pymupdf / lxml /
# pillow / asyncpg without their -dev counterparts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        libpq5 \
        libxml2 \
        libxslt1.1 \
        libjpeg62-turbo \
        zlib1g \
        libffi8 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user (uid 1000) and the directories the service writes to.
RUN groupadd --system --gid 1000 zubot \
    && useradd --system --uid 1000 --gid 1000 --home /app --shell /sbin/nologin zubot \
    && mkdir -p /app /var/log/zubot-ingestion \
    && chown -R zubot:zubot /app /var/log/zubot-ingestion /opt

# Copy the prebuilt virtualenv from the builder stage.
COPY --from=builder --chown=zubot:zubot /opt/venv /opt/venv

WORKDIR /app

# Copy application source. Only src/ is needed at runtime; tests, docs, etc.
# are excluded via .dockerignore.
COPY --chown=zubot:zubot src/ /app/src/

USER zubot

EXPOSE 4243

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:4243/health || exit 1

CMD ["uvicorn", "zubot_ingestion.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "4243"]
