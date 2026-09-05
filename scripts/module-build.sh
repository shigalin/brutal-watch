#!/usr/bin/env bash
# DKMS invokes this in its build directory. TCP Brutal sources remain unmodified.
set -euo pipefail

choose_compiler() {
  local headers=$1 policy=$2 version major candidate actual
  [[ -f "$headers/.config" ]] || { echo '找不到内核 .config，无法匹配编译器' >&2; return 1; }
  if grep -q '^CONFIG_CC_IS_CLANG=y' "$headers/.config"; then
    echo '当前自动安装仅支持 GCC 构建的内核；不会猜测 Clang 工具链或修改内核编译参数' >&2
    return 1
  fi
  version=$(sed -n 's/^CONFIG_GCC_VERSION=\([0-9]*\)$/\1/p' "$headers/.config")
  [[ "$version" =~ ^[0-9]+$ && "$version" -ge 50000 ]] || { echo '无法确定内核 GCC 版本' >&2; return 1; }
  major=$((version / 10000))
  if [[ "$policy" != docker ]]; then
    for candidate in "gcc-$major" gcc; do
      if command -v "$candidate" >/dev/null; then
        actual=$("$candidate" -dumpversion)
        if [[ ${actual%%.*} == "$major" ]]; then
          printf 'native:%s\n' "$candidate"
          return
        fi
      fi
    done
  fi
  [[ "$policy" != native ]] || { echo "内核需要 GCC ${major}，本机没有匹配版本" >&2; return 1; }
  printf 'docker:%s\n' "$major"
}

build_module() {
  local policy=$1 headers=$2 selected compiler major image build_dir
  [[ "$policy" == auto || "$policy" == native || "$policy" == docker ]] || return 2
  headers=$(readlink -f "$headers")
  selected=$(choose_compiler "$headers" "$policy")
  if [[ "$selected" == native:* ]]; then
    compiler=${selected#native:}
    exec make -j2 KERNEL_DIR="$headers" CC="$compiler" all
  fi
  major=${selected#docker:}
  image="gcc:$major-bookworm"
  if [[ "$major" == 13 ]]; then
    image='gcc@sha256:3617a214e52a25bde5375dc9503b5e67f01b6c7322a30137e2790aa8e6db5d1f'
  fi
  docker info >/dev/null 2>&1 || { echo '容器编译需要运行中的 Docker；未更换宿主机编译器' >&2; return 1; }
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
  build_dir=$(pwd -P)
  echo "使用官方 $image 编译；不加载容器或宿主机内核模块"
  exec docker run --rm --network=none --cpus=1 --memory=512m --memory-swap=768m \
    --pids-limit=128 --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp:rw,nosuid,size=64m -v /usr/src:/usr/src:ro -v /lib/modules:/lib/modules:ro \
    -v "$build_dir:$build_dir:rw" -w "$build_dir" "$image" \
    make -j1 KERNEL_DIR="$headers" CC=gcc all
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then build_module "$@"; fi
