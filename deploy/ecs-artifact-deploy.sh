#!/bin/sh
set -eu
umask 077

# 参数: test 分支和已解压的云效制品目录
[ "$#" = 2 ] || { echo 'usage: ecs-artifact-deploy.sh <branch> <bundle-dir>' >&2; exit 64; }
[ "$1" = test ] || { echo 'only test may deploy' >&2; exit 64; }
bundle=$(CDPATH= cd -- "$2" && pwd)
cd "$bundle"
test -s SHA256SUMS
sha256sum --strict --check SHA256SUMS >/dev/null
for file in image.tar.gz image-ref.txt image-id.txt deploy/docker-compose.ecs-2gb.yml deploy/docker-compose.ecs-test.yml; do
  test -s "$file"
  awk -v name="$file" '$2 == name || $2 == "*" name { found=1 } END { exit !found }' SHA256SUMS
done
image=$(cat image-ref.txt)
sha=${image#juya-admin-api-test:}
[ "$image" = "juya-admin-api-test:$sha" ] || exit 64
case "$sha" in ''|*[!a-f0-9]*) exit 64;; esac
[ "${#sha}" = 40 ] || exit 64

root=/opt/juya/juya-admin-api
if [ -n "${JUYA_DEPLOY_TEST_ROOT:-}" ]; then
  [ "${JUYA_DEPLOY_TEST_MODE:-}" = 1 ] || { echo 'root override requires test mode' >&2; exit 64; }
  root=$JUYA_DEPLOY_TEST_ROOT
fi
case "$root" in /*) ;; *) exit 64;; esac
env_file=${JUYA_COMPOSE_ENV_FILE:-/etc/juya/compose.env}
test -r "$env_file"
test -d "$root/deploy"
exec 9>"$root/.artifact-deploy.lock"
flock -n 9 || { echo 'another deployment is running' >&2; exit 75; }

docker load --input "$bundle/image.tar.gz" >/dev/null
actual_id=$(docker image inspect --format '{{.Id}}' "$image")
[ "$actual_id" = "$(cat "$bundle/image-id.txt")" ] || { echo 'image identity mismatch' >&2; exit 65; }

previous_dir="$root/deploy"
previous_override=0
if [ -L "$root/current-release" ]; then
  previous_dir="$(readlink -f "$root/current-release")/deploy"
  previous_override=1
fi

# 参数: Compose 子命令,所有应用保持原项目和持久卷
previous_compose() {
  if [ "$previous_override" = 1 ]; then
    JUYA_ADMIN_API_IMAGE="$previous_image" docker compose --env-file "$env_file" -p juya-admin-small \
      -f "$previous_dir/docker-compose.ecs-2gb.yml" -f "$previous_dir/docker-compose.ecs-test.yml" "$@"
  else
    JUYA_ADMIN_API_IMAGE="$previous_image" docker compose --env-file "$env_file" -p juya-admin-small \
      -f "$previous_dir/docker-compose.ecs-2gb.yml" "$@"
  fi
}

previous_image="$image"
previous_container=$(previous_compose ps -q admin-api)
test -n "$previous_container" || { echo 'existing deployment required' >&2; exit 69; }
previous_image=$(docker inspect --format '{{.Config.Image}}' "$previous_container")
release="$root/releases/$sha"
mkdir -p "$root/releases" "$root/backups"
if [ -d "$release" ]; then
  cmp "$bundle/SHA256SUMS" "$release/SHA256SUMS" || { echo 'release already exists with different content' >&2; exit 65; }
else
  mkdir "$release"
  cp -R "$bundle/deploy" "$release/deploy"
  cp "$bundle/image-ref.txt" "$bundle/image-id.txt" "$bundle/SHA256SUMS" "$release/"
fi
compose() {
  JUYA_ADMIN_API_IMAGE="$image" docker compose --env-file "$env_file" -p juya-admin-small \
    -f "$release/deploy/docker-compose.ecs-2gb.yml" -f "$release/deploy/docker-compose.ecs-test.yml" "$@"
}
compose config --quiet

stopped=0
success=0
finish() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$success" != 1 ] && [ "$stopped" = 1 ]; then
    echo 'deployment failed; restoring previous application (database not downgraded)' >&2
    previous_compose up -d --no-deps admin-api admin-worker admin-beat || true
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' HUP INT TERM

# 使用数据库容器已有密码进行备份,不在命令参数或日志输出凭据
backup="$root/backups/$(date -u +%Y%m%dT%H%M%SZ)-$sha.sql"
previous_compose exec -T mysql sh -ec \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --routines --triggers --no-tablespaces juya' > "$backup"
test -s "$backup"
cp "$env_file" "$backup.compose.env"

stopped=1
previous_compose stop admin-beat
worker_container=$(previous_compose ps -q admin-worker)
previous_compose stop -t 180 admin-worker
if [ -n "$worker_container" ]; then
  [ "$(docker inspect --format '{{.State.ExitCode}}' "$worker_container")" != 137 ] || {
    echo 'worker exceeded graceful shutdown budget; deployment aborted' >&2; exit 75;
  }
fi
previous_compose stop admin-api
compose run --rm --no-deps migrate
compose up -d --no-deps admin-api admin-worker admin-beat
curl --fail --silent --show-error --max-time 5 --retry 30 --retry-delay 2 --retry-all-errors \
  "http://127.0.0.1:8000/health/ready" >/dev/null
# 后台进程没有 API 就绪探针,发布成功前单独核验实例数及重启状态
for pass in 1 2; do
  for service in admin-api admin-worker admin-beat; do
    container=$(compose ps -q "$service")
    test -n "$container" || { echo "$service is missing" >&2; exit 1; }
    state=$(docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$container")
    [ "$state" = 'running 0' ] || { echo "$service did not start cleanly" >&2; exit 1; }
  done
  if [ "$pass" = 1 ]; then sleep 2; fi
done
compose ps
ln -s "$release" "$root/.current-release.next"
mv -Tf "$root/.current-release.next" "$root/current-release"
success=1
printf 'deployed %s\n' "$image"
