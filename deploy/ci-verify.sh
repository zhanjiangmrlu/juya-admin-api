#!/bin/sh
set -eu
umask 077
[ "${CI_COMMIT_REF_NAME:-}" = test ] || { echo 'only test may publish' >&2; exit 64; }
[ "$(uname -s)" = Linux ] || { echo 'Linux build container required' >&2; exit 64; }

# 原生进程不继承业务连接、凭据和真实外部测试开关;只提取变量名,不输出其值。
for variable in $(env | sed -n 's/^\(JUYA_[A-Za-z0-9_]*\|OSS_[A-Za-z0-9_]*\)=.*/\1/p'); do
  unset "$variable"
done
export JUYA_RUN_LIVE_OSS_TESTS=false JUYA_RUN_LIVE_OSS_BROWSER_TESTS=false

# 公共集群拒绝 Docker network create;测试服务直接运行在任务容器内。
ci_root=$(mktemp -d "${TMPDIR:-/tmp}/juya-ci-native.XXXXXX")
mysql_pid=
redis_pid=
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" != 0 ]; then
    for log in "$ci_root/mysql.log" "$ci_root/redis.log"; do
      if [ -f "$log" ]; then tail -n 30 "$log" >&2; fi
    done
  fi
  for pid in "$mysql_pid" "$redis_pid"; do
    if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  done
  # 只删除 mktemp 为本次任务生成的目录,并先退出自有服务进程。
  rm -rf "$ci_root"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
mkdir -p "$ci_root/tools" "$ci_root/mysql-data" "$ci_root/redis-data"
export PATH="$ci_root/tools/bin:$PATH"
# 仅缓存工具和包;虚拟环境、测试数据及日志仍归本次临时目录所有。
ci_cache="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/juya-ci-native"
export UV_CACHE_DIR="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/uv"
export UV_PYTHON_INSTALL_DIR="$ci_cache/python"
mkdir -p "$ci_cache"

if ! command -v mysqld >/dev/null || ! command -v redis-server >/dev/null; then
  echo 'Preparing native MySQL/Redis build dependencies'
  if command -v dnf >/dev/null; then
    sh "$(dirname "$0")/ci-install-rpm.sh" gcc make tar gzip xz libaio numactl-libs ncurses-libs libtirpc
  elif command -v apt-get >/dev/null; then
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then
      sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
    fi
    apt-get -o Acquire::Retries=3 update
    aio_package=libaio1
    if ! apt-cache show libaio1 >/dev/null 2>&1; then aio_package=libaio1t64; fi
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends gcc make tar gzip xz-utils "$aio_package" libnuma1 libncurses6 libtirpc3 ca-certificates curl
    if [ "$aio_package" = libaio1t64 ]; then
      mkdir -p "$ci_root/tools/lib"
      ln -s /usr/lib/x86_64-linux-gnu/libaio.so.1t64 "$ci_root/tools/lib/libaio.so.1"
      export LD_LIBRARY_PATH="$ci_root/tools/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
  else
    echo 'dnf or apt-get required to prepare native test services' >&2; exit 69
  fi
fi
if ! command -v uv >/dev/null; then
  echo 'Installing uv 0.11.16'
  curl --fail --location --retry 3 --output "$ci_root/uv-install.sh" https://astral.sh/uv/0.11.16/install.sh
  UV_INSTALL_DIR="$ci_root/tools/bin" UV_NO_MODIFY_PATH=1 sh "$ci_root/uv-install.sh"
fi
if ! command -v mysqld >/dev/null; then
  echo 'Downloading native MySQL 8.4.6'
  curl --fail --location --retry 3 --output "$ci_root/mysql.tar.xz" \
    https://cdn.mysql.com/archives/mysql-8.4/mysql-8.4.6-linux-glibc2.28-x86_64-minimal.tar.xz
  mkdir "$ci_root/tools/mysql"
  tar xJf "$ci_root/mysql.tar.xz" -C "$ci_root/tools/mysql" --strip-components=1
  export PATH="$ci_root/tools/mysql/bin:$PATH"
fi
if ! command -v redis-server >/dev/null; then
  echo 'Building native Redis 7.4.2'
  curl --fail --location --retry 3 --output "$ci_root/redis.tar.gz" https://download.redis.io/releases/redis-7.4.2.tar.gz
  tar xzf "$ci_root/redis.tar.gz" -C "$ci_root/tools"
  make -C "$ci_root/tools/redis-7.4.2" -j2 MALLOC=libc OPTIMIZATION=-O2 BUILD_TLS=no
  export PATH="$ci_root/tools/redis-7.4.2/src:$PATH"
fi
mysqld --version | grep -E '8\.4\.' >/dev/null
redis-server --version | grep -E 'v=7\.4\.' >/dev/null

# Native CI must exercise actual decoding/slicing even on non-Debian runners.
# Source/version/hash verified from the upstream HTTPS release on 2026-10-11.
phase_started=$(date +%s)
if ! ffmpeg -version 2>/dev/null | grep -E '^ffmpeg version 7\.1\.5([ -]|$)' >/dev/null \
   || ! ffprobe -version 2>/dev/null | grep -E '^ffprobe version 7\.1\.5([ -]|$)' >/dev/null; then
  if command -v dnf >/dev/null; then
    sh "$(dirname "$0")/ci-install-rpm.sh" gcc make tar xz
  elif command -v apt-get >/dev/null; then
    apt-get -o Acquire::Retries=3 update
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends gcc make tar xz-utils
  else
    echo 'dnf or apt-get required for pinned FFmpeg build' >&2; exit 69
  fi
  sh "$(dirname "$0")/ci-prepare-ffmpeg.sh" "$ci_root/tools" "$ci_cache"
fi
ffmpeg -version | head -n 1
ffprobe -version | head -n 1
# Prove binaries can generate, probe and completely decode audio before starting services.
ffmpeg -nostdin -v error -f lavfi -i sine=frequency=440:duration=0.1 \
  -c:a pcm_s16le "$ci_root/ffmpeg-smoke.wav"
ffprobe -v error -show_entries format=duration -of default=nw=1 "$ci_root/ffmpeg-smoke.wav"
ffmpeg -nostdin -v error -xerror -i "$ci_root/ffmpeg-smoke.wav" -f null -
echo "FFmpeg preparation elapsed_seconds=$(($(date +%s)-phase_started))"

export UV_PROJECT_ENVIRONMENT="$ci_root/venv" UV_LINK_MODE=copy
# uv.lock 记录 PyPI 原始 URL,仅设置 UV_INDEX_URL 不能可靠重定向已锁定文件。
# 导出原锁定版本及哈希后从镜像安装;本地项目单独安装,不重解析业务依赖。
echo 'Installing locked Python dependencies from Aliyun mirror'
phase_started=$(date +%s)
uv export --locked --no-emit-project --format requirements-txt --output-file "$ci_root/requirements.txt"
uv venv --python 3.13 "$UV_PROJECT_ENVIRONMENT"
uv pip sync --python "$UV_PROJECT_ENVIRONMENT/bin/python" --require-hashes \
  --index-url https://mirrors.aliyun.com/pypi/simple/ "$ci_root/requirements.txt"
uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" --no-deps --editable . \
  --index-url https://mirrors.aliyun.com/pypi/simple/
echo "Python preparation elapsed_seconds=$(($(date +%s)-phase_started))"
# 避免误用同一容器里已存在的服务;占用端口时在创建数据库前退出。
uv run --no-sync python -c 'import socket; sockets = [socket.socket(), socket.socket()]; [s.bind(("127.0.0.1", p)) for s, p in zip(sockets, (13306, 16379))]'
operations_database="juya_v13_ops_$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
mysql_base=$(dirname "$(dirname "$(command -v mysqld)")")
mysqld --no-defaults --initialize-insecure --user="$(id -un)" \
  --basedir="$mysql_base" --datadir="$ci_root/mysql-data" > "$ci_root/mysql.log" 2>&1
mysqld --no-defaults --user="$(id -un)" --basedir="$mysql_base" \
  --datadir="$ci_root/mysql-data" --socket="$ci_root/mysql.sock" \
  --pid-file="$ci_root/mysql.pid" --bind-address=127.0.0.1 --port=13306 \
  --mysqlx=0 --innodb-buffer-pool-size=128M >> "$ci_root/mysql.log" 2>&1 &
mysql_pid=$!
redis-server --bind 127.0.0.1 --port 16379 --protected-mode yes \
  --save '' --appendonly no --dir "$ci_root/redis-data" > "$ci_root/redis.log" 2>&1 &
redis_pid=$!
count=0
until mysql --no-defaults --socket="$ci_root/mysql.sock" -uroot -e 'SELECT 1' >/dev/null 2>&1; do
  count=$((count + 1)); [ "$count" -lt 60 ] || exit 1
  kill -0 "$mysql_pid" 2>/dev/null || exit 1
  sleep 2
done
sleep 1
kill -0 "$redis_pid" 2>/dev/null || exit 1
redis-cli -h 127.0.0.1 -p 16379 ping | grep -x PONG >/dev/null
mysql --no-defaults --socket="$ci_root/mysql.sock" -uroot -e \
  "ALTER USER 'root'@'localhost' IDENTIFIED BY 'isolated-ci-only'; CREATE DATABASE juya_ci; CREATE DATABASE $operations_database" >/dev/null

# 只使用本任务的环回服务和新建空库,不读取或连接 ECS 数据库。
export JUYA_TEST_DATABASE_URL=mysql+pymysql://root:isolated-ci-only@127.0.0.1:13306/juya_ci
export JUYA_MIGRATION_DATABASE_URL="$JUYA_TEST_DATABASE_URL"
export JUYA_TEST_REDIS_URL=redis://127.0.0.1:16379/0
export JUYA_V13_ISOLATED_DATABASE="$operations_database"
uv run --no-sync alembic upgrade head
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src
uv run --no-sync pytest --ignore=tests/integration/test_operations_v13.py --cov=juya_admin_api --cov-report=term-missing --cov-fail-under=80
export JUYA_TEST_DATABASE_URL="mysql+pymysql://root:isolated-ci-only@127.0.0.1:13306/$operations_database"
export JUYA_MIGRATION_DATABASE_URL="$JUYA_TEST_DATABASE_URL"
uv run --no-sync pytest tests/integration/test_operations_v13.py --cov=juya_admin_api --cov-append --cov-report=term-missing --cov-fail-under=80
