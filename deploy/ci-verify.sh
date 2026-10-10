#!/bin/sh
set -eu
[ "${CI_COMMIT_REF_NAME:-}" = test ] || { echo 'only test may publish' >&2; exit 64; }
docker version >/dev/null
job="juya-ci-$(date +%s)-$$"
cleanup() {
  docker rm -f "$job-python" "$job-mysql" "$job-redis" >/dev/null 2>&1 || true
  docker network rm "$job" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$job" >/dev/null
docker run -d --name "$job-mysql" --network "$job" --network-alias mysql \
  -e MYSQL_ROOT_PASSWORD=isolated-ci-only -e MYSQL_DATABASE=juya_ci \
  mysql:8.4 >/dev/null
docker run -d --name "$job-redis" --network "$job" --network-alias redis redis:7.4-alpine >/dev/null
count=0
until docker exec "$job-mysql" mysqladmin ping -h127.0.0.1 -uroot -pisolated-ci-only --silent >/dev/null 2>&1; do
  count=$((count + 1)); [ "$count" -lt 60 ] || exit 1; sleep 2
done
operations_database="juya_v13_ops_$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
docker exec "$job-mysql" mysql -uroot -pisolated-ci-only -e "CREATE DATABASE $operations_database" >/dev/null
docker run --rm --name "$job-python" --network "$job" -v "$PWD:/work" -w /work \
  -e UV_LINK_MODE=copy -e UV_PROJECT_ENVIRONMENT=/tmp/juya-ci-venv \
  -e JUYA_TEST_DATABASE_URL=mysql+pymysql://root:isolated-ci-only@mysql:3306/juya_ci \
  -e JUYA_MIGRATION_DATABASE_URL=mysql+pymysql://root:isolated-ci-only@mysql:3306/juya_ci \
  -e JUYA_TEST_REDIS_URL=redis://redis:6379/0 \
  -e JUYA_V13_ISOLATED_DATABASE="$operations_database" \
  ghcr.io/astral-sh/uv:python3.13-bookworm-slim sh -ec '
    uv sync --locked
    uv run alembic upgrade head
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src
    uv run pytest --ignore=tests/integration/test_operations_v13.py --cov=juya_admin_api --cov-report=term-missing
    export JUYA_TEST_DATABASE_URL="mysql+pymysql://root:isolated-ci-only@mysql:3306/$JUYA_V13_ISOLATED_DATABASE"
    export JUYA_MIGRATION_DATABASE_URL="$JUYA_TEST_DATABASE_URL"
    uv run pytest tests/integration/test_operations_v13.py --cov=juya_admin_api --cov-append --cov-report=term-missing
  '
