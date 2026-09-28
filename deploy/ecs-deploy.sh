#!/bin/sh
set -eu

image="${1:?usage: ecs-deploy.sh <image>}"
deploy_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose_file="$deploy_dir/docker-compose.ecs.yml"
env_file="${JUYA_ENV_FILE:-/etc/juya/admin-api.env}"

test -r "$env_file"
if [ -n "${ACR_USERNAME:-}" ] && [ -n "${ACR_PASSWORD:-}" ]; then
  registry="${image%%/*}"
  printf '%s' "$ACR_PASSWORD" | docker login --username "$ACR_USERNAME" --password-stdin "$registry"
fi
docker pull "$image"

previous_container="$(JUYA_ADMIN_API_IMAGE="$image" JUYA_ENV_FILE="$env_file" \
  docker compose -f "$compose_file" ps -q admin-api 2>/dev/null || true)"
previous_image=""
if [ -n "$previous_container" ]; then
  previous_image="$(docker inspect --format '{{.Config.Image}}' "$previous_container")"
fi

# 迁移使用同一不可变镜像，但作为独立的一次性进程运行。迁移成功后才更新长期进程。
docker run --rm \
  --env-file "$env_file" \
  -e JUYA_PROCESS_ROLE=migrate \
  "$image"

JUYA_ADMIN_API_IMAGE="$image" JUYA_ENV_FILE="$env_file" \
  docker compose -f "$compose_file" up -d --remove-orphans

attempt=0
until curl --fail --silent --show-error http://127.0.0.1:"${JUYA_ADMIN_API_PORT:-8000}"/health/ready >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker compose -f "$compose_file" ps
    if [ -n "$previous_image" ]; then
      JUYA_ADMIN_API_IMAGE="$previous_image" JUYA_ENV_FILE="$env_file" \
        docker compose -f "$compose_file" up -d --remove-orphans
    fi
    exit 1
  fi
  sleep 2
done

docker compose -f "$compose_file" ps
