#!/usr/bin/env bash
# DKMS invokes this in its build directory. TCP Brutal sources remain unmodified.
set -euo pipefail

kernel_config() {
  local headers=$1 config
  for config in "$headers/include/config/auto.conf" "$headers/.config"; do
    if [[ -f "$config" ]]; then
      printf '%s\n' "$config"
      return
    fi
  done
  echo '找不到内核 auto.conf 或 .config，无法匹配编译器' >&2
  return 1
}

choose_compiler() {
  local headers=$1 policy=$2 version major candidate actual config
  config=$(kernel_config "$headers") || return 1
  if grep -q '^CONFIG_CC_IS_CLANG=y' "$config"; then
    [[ "$policy" != docker ]] || { echo 'Clang 内核请使用 --compiler auto 或 native；Docker 编译目前仅支持 GCC' >&2; return 1; }
    for candidate in clang ld.lld llvm-objcopy; do
      command -v "$candidate" >/dev/null || { echo "Clang 内核缺少 ${candidate}，请安装 clang、lld、llvm 工具链" >&2; return 1; }
    done
    printf 'native:llvm\n'
    return
  fi
  version=$(sed -n 's/^CONFIG_GCC_VERSION=\([0-9]*\)$/\1/p' "$config")
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
  if [[ "$selected" == native:llvm ]]; then
    exec make -j2 KERNEL_DIR="$headers" LLVM=1 all
  fi
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
    make -j1 KERNEL_DIR="$headers" PWD="$build_dir" CC=gcc all
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then build_module "$@"; fi
