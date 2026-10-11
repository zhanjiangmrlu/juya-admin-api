#!/bin/sh
set -eu

# 功能:为云效 Alibaba Cloud Linux 3 容器安装缺少的基础依赖,避免刷新无关仓库
# 参数:经过校验的 RPM 包名列表;不升级已有包,安装失败继续作为流水线失败
missing=
for package do
  case "$package" in ''|-*|*[!a-zA-Z0-9._+-]*) echo 'invalid RPM package name' >&2; exit 64;; esac
  if ! rpm -q "$package" >/dev/null 2>&1; then missing="$missing $package"; fi
done
if [ -z "$missing" ]; then
  echo 'Required RPM dependencies already installed; skipping repository refresh'
  exit 0
fi
echo "Installing missing RPM dependencies:$missing"
# 仅启用已验证包含所需库的基础仓库,保留 RPM GPG 校验;HTTPS 避免依赖 HTTP 源
# missing 只包含上述校验通过的包名,此处有意按包名拆分参数
# shellcheck disable=SC2086
dnf install -y --disablerepo='*' --enablerepo=alinux3-os \
  '--setopt=alinux3-os.baseurl=https://mirrors.aliyun.com/alinux/3/os/$basearch/' \
  --setopt=alinux3-os.gpgkey=https://mirrors.aliyun.com/alinux/3/RPM-GPG-KEY-ALINUX-3 \
  --setopt=alinux3-os.gpgcheck=1 \
  --setopt=install_weak_deps=False --setopt=timeout=30 --setopt=retries=2 $missing
