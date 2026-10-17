#!/bin/sh
set -eu
umask 077

# 参数:本次任务工具目录、可由流水线保存的缓存根目录。仅复用媒体工具,不缓存数据库。
tools=$1
cache_root=$2
key=$(
  {
    sha256sum "$0" | cut -d ' ' -f 1
    uname -m
    if [ -r /etc/os-release ]; then cat /etc/os-release; else uname -s; fi
    getconf GNU_LIBC_VERSION 2>/dev/null || true
  } | sha256sum | cut -d ' ' -f 1
)
entry="$cache_root/ffmpeg-$key"
mkdir -p "$entry" "$tools/bin"
# 同一缓存目录中只允许一个任务发布完整安装结果。
exec 9>"$entry/lock"
flock 9

valid_install() {
  [ -f "$entry/install/SHA256SUMS" ] \
    && (cd "$entry/install" \
        && expected=$(sha256sum bin/ffmpeg bin/ffprobe) \
        && [ "$expected" = "$(cat SHA256SUMS)" ]) \
    && "$entry/install/bin/ffmpeg" -version | grep -E '^ffmpeg version 7\.1\.5([ -]|$)' >/dev/null \
    && "$entry/install/bin/ffprobe" -version | grep -E '^ffprobe version 7\.1\.5([ -]|$)' >/dev/null
}

if valid_install; then
  echo 'Reusing verified FFmpeg 7.1.5 cache'
else
  build=$(mktemp -d "$entry/build.XXXXXX")
  # 只清理本脚本创建的编译临时目录;完整工具和已校验源码保留在缓存中。
  trap 'rm -rf "$build"' EXIT
  trap 'exit 130' HUP INT TERM
  archive="$entry/ffmpeg-7.1.5.tar.xz"
  source_hash=de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f
  verify_source() {
    printf '%s  %s\n' "$source_hash" "$1" | sha256sum --strict --check - >/dev/null 2>&1
  }
  if ! verify_source "$archive"; then
    echo 'Downloading FFmpeg 7.1.5 source'
    curl --fail --location --retry 3 --connect-timeout 30 --max-time 900 \
      --output "$build/source.tar.xz" https://ffmpeg.org/releases/ffmpeg-7.1.5.tar.xz
    verify_source "$build/source.tar.xz"
    mv "$build/source.tar.xz" "$archive"
  fi
  tar xJf "$archive" -C "$build"
  echo 'Compiling FFmpeg 7.1.5 (cold cache)'
  (
    cd "$build/ffmpeg-7.1.5"
    ./configure --prefix="$build/install" --disable-autodetect --disable-x86asm --disable-doc
    make -j2
    make install
  )
  # 静态 FFmpeg 库链接进两个程序,仅缓存运行需要的可执行文件。
  mkdir "$build/ready"
  mv "$build/install/bin" "$build/ready/bin"
  (cd "$build/ready" && sha256sum bin/ffmpeg bin/ffprobe > SHA256SUMS)
  if [ -d "$entry/install" ]; then mv "$entry/install" "$build/obsolete"; fi
  mv "$build/ready" "$entry/install"
  valid_install
fi
ln -sf "$entry/install/bin/ffmpeg" "$tools/bin/ffmpeg"
ln -sf "$entry/install/bin/ffprobe" "$tools/bin/ffprobe"
