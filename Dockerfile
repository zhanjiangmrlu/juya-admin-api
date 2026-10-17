# 官方 Python 3.13/uv 镜像,固定摘要;基础镜像只从 GHCR 拉取。
FROM ghcr.io/astral-sh/uv:0.11.16-python3.13-trixie-slim@sha256:d57fc364ed714127c162a01b316bfb7b9a3fd09d97c665781a5607acaa35d254 AS python-base

FROM python-base AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# 锁文件中的原始 PyPI URL 不随 index-url 重定向;导出原版本及哈希后安装。
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file /tmp/requirements.txt \
    && uv venv /app/.venv \
    && uv pip sync --python /app/.venv/bin/python --require-hashes \
        --index-url https://mirrors.aliyun.com/pypi/simple/ /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --python /app/.venv/bin/python --no-deps \
        --index-url https://mirrors.aliyun.com/pypi/simple/ . \
    && uv pip check --python /app/.venv/bin/python

FROM python-base AS runtime

# Verified against https://packages.debian.org/trixie/amd64/ffmpeg (2026-10-11).
ARG FFMPEG_DEBIAN_VERSION=7:7.1.5-0+deb13u1

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JUYA_PROCESS_ROLE=admin-api \
    JUYA_IMPORT_STORAGE_PATH=/var/lib/juya/imports

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' > /etc/apt/apt.conf.d/keep-cache \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends "ffmpeg=${FFMPEG_DEBIAN_VERSION}" \
    && test "$(dpkg-query -W -f='${Version}' ffmpeg)" = "$FFMPEG_DEBIAN_VERSION" \
    && ffmpeg -version && ffprobe -version \
    && rm /usr/local/bin/uv /usr/local/bin/uvx \
    && groupadd --system --gid 10001 juya \
    && useradd --system --uid 10001 --gid juya --home-dir /app --shell /usr/sbin/nologin juya \
    && install -d -m 0700 -o 10001 -g 10001 /var/lib/juya/imports /var/lib/juya/imports/.tmp
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
