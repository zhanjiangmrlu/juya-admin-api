# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JUYA_PROCESS_ROLE=admin-api

RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt/lists \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 juya \
    && useradd --system --uid 10001 --gid juya --home-dir /app --shell /usr/sbin/nologin juya
WORKDIR /app
COPY --from=builder --chown=juya:juya /app/.venv /app/.venv
COPY --chown=juya:juya alembic.ini ./
COPY --chown=juya:juya migrations ./migrations
COPY --chown=juya:juya scripts/entrypoint.sh ./scripts/entrypoint.sh
COPY --chown=juya:juya scripts/healthcheck.py ./scripts/healthcheck.py
RUN chmod 0555 /app/scripts/entrypoint.sh

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "/app/scripts/healthcheck.py"]
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
