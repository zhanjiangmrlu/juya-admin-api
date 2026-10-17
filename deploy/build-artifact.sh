#!/bin/sh
set -eu
[ "$#" = 3 ] || { echo 'usage: build-artifact.sh <branch> <commit-sha> <output>' >&2; exit 64; }
[ "$1" = test ] || { echo 'only test may build test artifacts' >&2; exit 64; }
sha=$2
case "$sha" in ''|*[!a-f0-9]*) exit 64;; esac
[ "${#sha}" = 40 ] || exit 64
output=$3
[ ! -e "$output" ] || { echo 'output must not already exist' >&2; exit 64; }
image="juya-admin-api-test:$sha"
# 云效代理可能拒绝版本查询;镜像构建、导出和校验仍须成功。
command -v docker >/dev/null
command -v python3 >/dev/null
timed() {
  stage=$1
  shift
  started=$(date +%s)
  printf '[artifact] %s start\n' "$stage"
  "$@"
  printf '[artifact] %s elapsed=%ss\n' "$stage" "$(( $(date +%s) - started ))"
}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -n "${JUYA_DOCKER_CACHE_DIR:-}" ]; then
  work=$(mktemp -d "${TMPDIR:-/tmp}/juya-artifact.XXXXXX")
  # 清理仅涉及本次创建的两个固定文件,不递归删除调用方目录。
  trap 'rm -f "$work/image.tar" "$work/build-metrics.json"; rmdir "$work"' EXIT
  python3 "$script_dir/cached-build.py" "$image" "$work/image.tar" "$JUYA_DOCKER_CACHE_DIR"
  mkdir -p "$output"
  mv "$work/image.tar" "$output/image.tar"
  mv "$work/build-metrics.json" "$output/build-metrics.json"
else
  printf '[artifact] external cache disabled; set JUYA_DOCKER_CACHE_DIR to enable\n'
  timed docker-build docker build --progress=plain --platform linux/amd64 --tag "$image" .
  mkdir -p "$output"
  timed docker-save docker save --output "$output/image.tar" "$image"
fi
python3 "$script_dir/image-identity.py" "$output/image.tar" "$image" > "$output/image-id.txt"
if command -v pigz >/dev/null; then
  timed image-compress pigz -n "$output/image.tar"
else
  timed image-compress gzip -n "$output/image.tar"
fi
printf '%s\n' "$image" > "$output/image-ref.txt"
cp -R deploy "$output/deploy"
(
  cd "$output"
  find deploy -type f -exec sha256sum {} \; > SHA256SUMS
  sha256sum image.tar.gz image-ref.txt image-id.txt >> SHA256SUMS
  if [ -f build-metrics.json ]; then sha256sum build-metrics.json >> SHA256SUMS; fi
)
