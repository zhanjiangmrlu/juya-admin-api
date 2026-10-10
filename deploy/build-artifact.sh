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
docker version >/dev/null
docker build --platform linux/amd64 --tag "$image" .
mkdir -p "$output"
docker save --output "$output/image.tar" "$image"
gzip "$output/image.tar"
printf '%s\n' "$image" > "$output/image-ref.txt"
docker image inspect --format '{{.Id}}' "$image" > "$output/image-id.txt"
cp -R deploy "$output/deploy"
(
  cd "$output"
  find deploy -type f -exec sha256sum {} \; > SHA256SUMS
  sha256sum image.tar.gz image-ref.txt image-id.txt >> SHA256SUMS
)
