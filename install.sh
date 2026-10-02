#!/usr/bin/env bash
# Install the official TCP Brutal module and this independent IP rule manager.
set -Eeuo pipefail
PROJECT_REPO=shigalin/brutal-watch
PROJECT_REF=${BRUTAL_WATCH_REF:-main}
UPSTREAM_COMMIT=d2397ff8bca04a29fd2de01cf7d2d4b825224de8
UPSTREAM_SHA256=53da436ed1c42094bc8eb73b4d2920ec8ba061990974dad5765cb93cb5148f1b
MODULE_VERSION=2.0.1
DKMS_VERSION=2.0.1-bwd2397ff
MODULE_NAME=tcp-brutal
MODULE_SOURCE=/usr/src/$MODULE_NAME-$DKMS_VERSION
DKMS_OVERRIDE=/etc/dkms/$MODULE_NAME-$DKMS_VERSION.conf
LIBEXEC=/usr/local/libexec/brutal-watch
WATCH_COMMAND=/usr/local/sbin/brutal-watch
CONFIGURE_NODE=0
ENABLE=0
SKIP_MODULE=0
UNINSTALL=0
KEEP_MODULE=0
UNLOAD_MODULE=0
INSTALL_REQUESTED=0
COMPILER=auto
TMP_WORK=
CONFIG_ARGS=()
ARCHIVE_ARGS=()
PACKAGE_INDEX_UPDATED=0

usage() {
  cat <<'EOF'
用法：bash install.sh [选项]
      bash install.sh --uninstall [--keep-module | --unload-module]
  --uninstall          关闭并清理加速，删除工具、配置、状态及本项目安装的 DKMS 模块
  --keep-module        卸载时保留内核模块、brutalctl 和 DKMS 构建入口
  --unload-module      允许移除当前已加载的 brutal 模块（包括外部安装的模块），必要时中断目标节点连接
  --container NAME     目标容器（首次安装默认 xboard-node）
  --ports 2053,443      节点监听端口（首次安装默认 2053）
  --rate-mbps 100       每个 IP 的目标速率
  --ttl-seconds 1800    最后出现后的保留时间
  --max-ips 1024        最大状态条目数
  --compiler POLICY    auto（默认）/ native / docker
  --archive-wait-minutes N  预授权本次归档查找最多等待 N 分钟（1–30），不再交互确认
  --configure-node     允许备份并调整现有 Compose 的 GODEBUG，重建一次节点容器
  --enable             安装、验证完成后开启加速及开机自启
  --skip-module        使用已安装的模块，不安装 DKMS 或编译模块
  --help               查看帮助
默认安装或升级到项目固定的最新官方稳定版，不调整节点、不启用加速。
重复安装仅更新显式传入的配置参数，其余保留；配置变化时先备份原文件。
重复安装会自动 off 并确认规则清理完成；内存中旧模块不会强制卸载，升级后可能需要手动重启。
只支持 Debian/Ubuntu、systemd、x86_64/aarch64、现有 Docker host 网络节点。
卸载不受上述安装环境限制，但需要 root、Linux 和 systemd；--unload-module 允许在模块占用时停止并恢复目标容器。
EOF
}
die() { echo "错误：$*" >&2; exit 1; }
cleanup() {
  local result=$?
  trap - EXIT
  [[ -z "$TMP_WORK" ]] || rm -rf -- "$TMP_WORK"
  exit "$result"
}

parse_args() {
  while (($#)); do
    case "$1" in
      --help|-h) usage; exit 0 ;;
      --uninstall) UNINSTALL=1; shift ;;
      --keep-module) KEEP_MODULE=1; shift ;;
      --unload-module) UNLOAD_MODULE=1; shift ;;
      --configure-node) INSTALL_REQUESTED=1; CONFIGURE_NODE=1; shift ;;
      --enable) INSTALL_REQUESTED=1; ENABLE=1; shift ;;
      --skip-module) INSTALL_REQUESTED=1; SKIP_MODULE=1; shift ;;
      --compiler)
        INSTALL_REQUESTED=1
        (($# >= 2)) || die '--compiler 缺少参数'
        COMPILER=$2; shift 2 ;;
      --archive-wait-minutes)
        INSTALL_REQUESTED=1
        (($# >= 2)) || die '--archive-wait-minutes 缺少参数'
        [[ "$2" =~ ^([1-9]|[12][0-9]|30)$ ]] || die '--archive-wait-minutes 必须是 1–30 的整数'
        ARCHIVE_ARGS=(--archive-wait-minutes "$2"); shift 2 ;;
      --container|--ports|--rate-mbps|--ttl-seconds|--max-ips)
        INSTALL_REQUESTED=1
        (($# >= 2)) || die "$1 缺少参数"
        CONFIG_ARGS+=("$1" "$2"); shift 2 ;;
      *) die "未知选项 $1" ;;
    esac
  done
  [[ "$COMPILER" == auto || "$COMPILER" == native || "$COMPILER" == docker ]] || die 'compiler 必须是 auto/native/docker'
  ((UNINSTALL == 0 || INSTALL_REQUESTED == 0)) || die '--uninstall 不能与安装参数混用'
  ((KEEP_MODULE == 0 || UNINSTALL == 1)) || die '--keep-module 只能与 --uninstall 一起使用'
  ((UNLOAD_MODULE == 0 || UNINSTALL == 1)) || die '--unload-module 只能与 --uninstall 一起使用'
  ((KEEP_MODULE == 0 || UNLOAD_MODULE == 0)) || die '--keep-module 不能与 --unload-module 一起使用'
}

check_platform() {
  [[ $(uname -s) == Linux && $(id -u) == 0 ]] || die '请在 Linux 宿主机上以 root 运行；不会在 macOS 上安装任何东西'
  case $(uname -m) in x86_64|aarch64) ;; *) die '自动安装仅支持 x86_64/aarch64' ;; esac
  local release major minor
  release=$(uname -r); major=${release%%.*}; minor=${release#*.}; minor=${minor%%.*}
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || die '无法解析内核版本'
  ((major > 5 || (major == 5 && minor >= 10))) || die 'TCP Brutal v2 需要 Linux >=5.10'
  [[ -f /etc/os-release ]] || die '无法识别发行版'
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ ${ID:-} == debian || ${ID:-} == ubuntu ]] || die '当前一键依赖安装仅支持 Debian/Ubuntu'
  [[ -d /run/systemd/system ]] || die '需要宿主机 systemd；不要在普通 Docker 容器内运行安装器'
}

check_docker() {
  if ! command -v docker >/dev/null; then
    if package_installed docker-cli; then
      die 'docker-cli 已安装，但找不到 docker 命令；请检查 PATH 或修复 docker-cli 软件包'
    fi
    package_installed docker.io \
      || die '未找到 Docker 客户端，也未检测到已安装的 docker.io 服务端；请检查现有 Docker 安装，本安装器不会安装或替换服务端或节点'
    local files
    files=$(dpkg-query -L docker.io) || die '无法读取 docker.io 文件清单，请检查软件包状态'
    if grep -Eq '^/(usr/)?bin/docker$' <<< "$files"; then
      die '当前 docker.io 已包含客户端，但找不到 docker 命令；请检查 PATH 或修复原软件包，不会自动重装服务端'
    fi
    systemctl is-active --quiet docker || systemctl is-active --quiet docker.socket \
      || die 'Docker 未运行，docker.socket 也未激活；请先检查已有 Docker 服务'
    update_package_index
    LC_ALL=C apt-cache policy docker-cli 2>/dev/null \
      | awk '$1 == "Candidate:" && $2 != "(none)" {found=1} END {exit !found}' \
      || die '当前软件源没有可安装的 docker-cli，请检查客户端包来源；不会安装或替换 Docker 服务端'
    echo '检测到已有 docker.io 服务端但缺少 Docker 客户端，补装 docker-cli'
    install_packages docker-cli
    command -v docker >/dev/null || die '补装后仍找不到 Docker 客户端，请检查 docker-cli 包和 PATH'
  fi
  docker info >/dev/null 2>&1 || die '无法连接 Docker 服务，请检查服务状态和 Docker 客户端连接配置'
}

package_installed() {
  dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null | grep -qx installed
}

update_package_index() {
  if [[ "$PACKAGE_INDEX_UPDATED" == 0 ]]; then
    apt-get update || die '软件源索引更新失败，请检查上面的 apt 错误后重试'
    PACKAGE_INDEX_UPDATED=1
  fi
}

install_packages() {
  local wanted=("$@")
  local missing=() pkg
  for pkg in "${wanted[@]}"; do
    package_installed "$pkg" || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    update_package_index
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y --no-install-recommends "${missing[@]}" \
      || die "${missing[*]} 安装失败，请检查上面的 apt 错误后重试"
  fi
}

install_dependencies() {
  install_packages python3 python3-yaml curl ca-certificates iproute2 kmod util-linux
  python3 -c 'import sys; assert sys.version_info >= (3,7), "需要 Python >=3.7"'
}

extract_archive() {
  python3 - "$1" "$2" <<'PY'
import pathlib,sys,tarfile
root=pathlib.Path(sys.argv[2]).resolve()
with tarfile.open(sys.argv[1],'r:gz') as archive:
    for member in archive.getmembers():
        parts=pathlib.PurePosixPath(member.name).parts
        if len(parts)<2: continue
        if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
            raise SystemExit('归档含不支持的链接或特殊文件')
        relative=pathlib.PurePosixPath(*parts[1:])
        if relative.is_absolute() or '..' in relative.parts: raise SystemExit('归档路径非法')
        destination=root/relative
        if member.isdir(): destination.mkdir(parents=True,exist_ok=True)
        else:
            destination.parent.mkdir(parents=True,exist_ok=True)
            with archive.extractfile(member) as src, destination.open('wb') as dst:
                dst.write(src.read())
            destination.chmod(member.mode & 0o755)
PY
}

project_source() {
  local here
  here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/dev/stdin}")" && pwd)
  if [[ -f "$here/brutal_watch.py" && -f "$here/scripts/setup.py" ]]; then
    SOURCE_DIR=$here
    return
  fi
  [[ "$PROJECT_REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die '项目 ref 无效'
  SOURCE_DIR="$TMP_WORK/project"
  mkdir -p "$SOURCE_DIR"
  curl -fL --connect-timeout 15 --max-time 120 --retry 2 \
    "https://api.github.com/repos/$PROJECT_REPO/tarball/$PROJECT_REF" -o "$TMP_WORK/project.tar.gz"
  extract_archive "$TMP_WORK/project.tar.gz" "$SOURCE_DIR"
  [[ -f "$SOURCE_DIR/brutal_watch.py" && -f "$SOURCE_DIR/scripts/module-build.sh" ]] || die '下载的项目文件不完整'
}

loaded_module_version() {
  local version
  if [[ -d /sys/module/brutal ]]; then
    if [[ ! -r /sys/module/brutal/version ]]; then
      echo '已加载 brutal 模块未提供可读的 version 文件，版本未知；请按原安装方式确认并迁移，不会覆盖模块' >&2
      return 1
    fi
    version=$(cat /sys/module/brutal/version) || return 1
    [[ -n "$version" ]] || return 1
    printf '%s\n' "$version"
  fi
}

module_has_rules() { [[ -e /proc/net/tcp_brutal/rules ]]; }

module_mode() {
  local disk loaded version
  disk=$(modinfo -F version brutal 2>/dev/null || true)
  loaded=$(loaded_module_version) || die '无法读取已加载 brutal 模块版本'
  if [[ "$SKIP_MODULE" != 1 ]]; then
    for version in "$disk" "$loaded"; do
      [[ -n "$version" ]] || continue
      [[ "$version" =~ ^2\.[0-9]+\.[0-9]+$ ]] || die "不支持自动迁移模块版本 ${version}，请按原安装方式处理"
      if dpkg --compare-versions "$version" gt "$MODULE_VERSION"; then
        die "已有模块 $version 新于目标 ${MODULE_VERSION}，拒绝降级"
      fi
    done
    if [[ -z "$disk" ]] && modinfo brutal >/dev/null 2>&1; then
      die '磁盘模块版本未知，拒绝覆盖'
    fi
    if [[ "$disk" == "$MODULE_VERSION" ]]; then
      if [[ -n "$loaded" && "$loaded" != "$MODULE_VERSION" ]]; then
        die "磁盘模块为 ${disk}，当前仍加载 ${loaded}；升级尚未生效，请安排手动重启后重跑安装器"
      fi
      echo existing
    else
      echo build
    fi
  elif [[ -n "$loaded" && -n "$disk" && "$loaded" != "$disk" ]]; then
    die "磁盘模块为 ${disk}，当前仍加载 ${loaded}；请安排手动重启后重跑安装器，不会开启加速"
  elif [[ -n "$loaded" && "$loaded" != 2.* ]]; then
    die '已加载旧版或未知 brutal 模块，请先按原安装方式迁移'
  elif module_has_rules; then
    echo loaded
  else
    echo existing
  fi
}

verify_module_version() {
  local disk loaded
  disk=$(modinfo -F version brutal) || die '安装后无法读取磁盘模块版本'
  [[ "$disk" == "$MODULE_VERSION" ]] || die "磁盘仍解析到模块 ${disk}，预期 ${MODULE_VERSION}；请检查 DKMS 和模块路径，不会开启加速"
  loaded=$(loaded_module_version) || die '无法读取已加载 brutal 模块版本'
  if [[ -n "$loaded" && "$loaded" != "$MODULE_VERSION" ]]; then
    die "新版 $MODULE_VERSION 已安装到磁盘，但当前仍加载 ${loaded}；升级尚未生效，加速保持关闭。请安排手动重启后重跑安装器验证，再执行 brutal-watch on；不会强卸载或自动重启"
  fi
  modprobe brutal || die '新版模块无法加载，请检查 DKMS、模块签名和内核日志'
  loaded=$(loaded_module_version) || die '无法读取已加载 brutal 模块版本'
  [[ "$loaded" == "$MODULE_VERSION" ]] || die "加载版本不是 ${MODULE_VERSION}，不会开启加速"
  module_has_rules || die '模块加载后未提供 v2 规则接口，请检查模块签名和内核日志'
}

check_module_paths() {
  if [[ -e "$MODULE_SOURCE" || -L "$MODULE_SOURCE" ]]; then
    [[ -d "$MODULE_SOURCE" && $(cat "$MODULE_SOURCE/.brutal-watch-upstream" 2>/dev/null) == "$UPSTREAM_COMMIT" ]] \
      || die '源码目录已有未知内容，拒绝覆盖'
  fi
  if [[ -e "$DKMS_OVERRIDE" || -L "$DKMS_OVERRIDE" ]]; then
    grep -q '^# Managed by brutal-watch$' "$DKMS_OVERRIDE" \
      || die '发现外部 DKMS 覆盖配置，拒绝覆盖'
  fi
}

prepare_module_source() {
  check_module_paths
  [[ ! -d "$MODULE_SOURCE" ]] || return 0
  curl -fL --connect-timeout 15 --max-time 120 --retry 2 \
    "https://codeload.github.com/HyNetworks/tcp-brutal/tar.gz/$UPSTREAM_COMMIT" -o "$TMP_WORK/tcp-brutal.tar.gz" \
    || die '上游模块源码下载失败；尚未关闭已有加速'
  printf '%s  %s\n' "$UPSTREAM_SHA256" "$TMP_WORK/tcp-brutal.tar.gz" | sha256sum -c - \
    || die '上游模块源码 SHA256 校验失败；尚未关闭已有加速'
  mkdir "$TMP_WORK/module"
  extract_archive "$TMP_WORK/tcp-brutal.tar.gz" "$TMP_WORK/module"
  (cd "$TMP_WORK/module" && PACKAGE_VERSION="$DKMS_VERSION" bash scripts/mkdkmsconf.sh > dkms.conf)
  printf '%s\n' "$UPSTREAM_COMMIT" > "$TMP_WORK/module/.brutal-watch-upstream"
}

prepare_module() {
  local kernel headers config selected image mode
  mode=$(module_mode) || return 1
  [[ "$mode" == build ]] || return 0
  prepare_module_source
  kernel=$(uname -r)
  install_packages dkms gcc make libc6-dev
  headers="/lib/modules/$kernel/build"
  ensure_headers "$kernel" "$headers"
  [[ -f "$headers/Makefile" ]] || die '内核 headers 不完整，请修复当前内核的 headers 包'
  source "$SOURCE_DIR/scripts/module-build.sh"
  config=$(kernel_config "$headers") || die '内核 headers 不完整'
  if grep -q '^CONFIG_CC_IS_CLANG=y' "$config"; then
    [[ "$COMPILER" != docker ]] || die 'Clang 内核请使用 --compiler auto 或 native；Docker 编译目前仅支持 GCC'
    install_packages clang lld llvm
  fi
  selected=$(choose_compiler "$headers" "$COMPILER") || die '编译工具链检查失败；尚未调整节点'
  if [[ "$selected" == docker:* ]]; then
    image=$(compiler_image "${selected#docker:}")
    docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image" \
      || die '无法取得编译器镜像；尚未调整节点'
  fi
}

ensure_headers() {
  local kernel=$1 headers=${2:-/lib/modules/$1/build}
  [[ ! -f "$headers/Makefile" ]] || return 0
  update_package_index
  if LC_ALL=C apt-cache policy "linux-headers-$kernel" 2>/dev/null \
      | awk '$1 == "Candidate:" && $2 != "(none)" {found=1} END {exit !found}'; then
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y --no-install-recommends "linux-headers-$kernel" \
      || die "安装 $kernel 的 headers 失败，请检查上面的 apt 错误；尚未调整节点"
  elif [[ ${ID:-} == debian ]]; then
    python3 "$SOURCE_DIR/scripts/debian_headers.py" "$kernel" "$TMP_WORK/headers" ${ARCHIVE_ARGS[@]+"${ARCHIVE_ARGS[@]}"} \
      || die "无法补齐 $kernel 的精确 headers；尚未调整节点，不会替换内核"
  else
    die "当前软件源没有 $kernel 的精确 headers，请检查软件源或内核提供方；尚未调整节点，不会替换内核"
  fi
}

install_module() {
  local kernel source headers override mode
  mode=$(module_mode) || return 1
  kernel=$(uname -r)
  if [[ "$mode" == loaded ]]; then
    echo '--skip-module：复用已加载的 v2 模块，未升级'
    return
  fi
  if [[ "$mode" == existing ]]; then
    if [[ "$SKIP_MODULE" != 1 ]]; then
      verify_module_version
    else
      modprobe brutal || die '无法加载现有 brutal 模块'
      module_has_rules || die '当前模块没有 v2 规则接口'
    fi
    echo '模块版本和规则接口检查完成；未重新编译现有模块'
    return
  fi
  headers="/lib/modules/$kernel/build"
  [[ -f "$headers/Makefile" ]] || die '内核 headers 不完整'
  source=$MODULE_SOURCE
  override=$DKMS_OVERRIDE
  # Recheck ownership before writing; preparation does not reserve these paths.
  check_module_paths
  if [[ ! -d "$source" ]]; then
    [[ -f "$TMP_WORK/module/dkms.conf" && $(cat "$TMP_WORK/module/.brutal-watch-upstream" 2>/dev/null) == "$UPSTREAM_COMMIT" ]] \
      || die '缺少预检阶段准备的模块源码，请重新运行安装器'
    mv "$TMP_WORK/module" "$source"
  fi
  [[ $(cat "$source/.brutal-watch-upstream" 2>/dev/null) == "$UPSTREAM_COMMIT" ]] || die '源码目录已有未知内容，拒绝覆盖'
  install -d -m 755 "$LIBEXEC"
  install -m 755 "$SOURCE_DIR/scripts/module-build.sh" "$LIBEXEC/module-build"
  cat > "$override" <<EOF
# Managed by brutal-watch
MAKE[0]="$LIBEXEC/module-build $COMPILER \${kernel_source_dir}"
EOF
  dkms status -m "$MODULE_NAME" -v "$DKMS_VERSION" | grep -q . || dkms add -m "$MODULE_NAME" -v "$DKMS_VERSION"
  dkms build -m "$MODULE_NAME" -v "$DKMS_VERSION" -k "$kernel" -j 2
  make -C "$source/tools"
  dkms install -m "$MODULE_NAME" -v "$DKMS_VERSION" -k "$kernel"
  install -m 755 "$source/tools/brutalctl" /usr/local/bin/brutalctl
  depmod -a "$kernel"
  verify_module_version
}

install_tool() {
  install -d -m 700 /etc/brutal-watch /var/lib/brutal-watch
  install -d -m 755 "$LIBEXEC"
  install -m 755 "$SOURCE_DIR/scripts/module-build.sh" "$LIBEXEC/module-build"
  install -m 755 "$SOURCE_DIR/brutal_watch.py" "$WATCH_COMMAND"
  install -m 644 "$SOURCE_DIR/brutal-watch.service" /etc/systemd/system/brutal-watch.service
  install -m 644 "$SOURCE_DIR/brutal-watch.timer" /etc/systemd/system/brutal-watch.timer
  install -m 600 "$SOURCE_DIR/OPERATIONS.md" /etc/brutal-watch/OPERATIONS.md
  systemctl daemon-reload
  systemd-analyze verify /etc/systemd/system/brutal-watch.service /etc/systemd/system/brutal-watch.timer
}

acquire_install_lock() {
  command -v flock >/dev/null || die '缺少 flock（util-linux）'
  exec 9>/run/brutal-watch-install.lock
  flock -n 9 || die '已有安装任务正在运行'
}

stop_existing_watch() {
  local remaining result previous=-1 report="$TMP_WORK/off-status.json"
  if [[ -x "$WATCH_COMMAND" ]]; then
    echo '检测到已有 brutal-watch，自动关闭加速并清理本工具规则与路由'
    while :; do
      result=0
      "$WATCH_COMMAND" off > "$report" || result=$?
      remaining=$(python3 - "$report" "$result" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    state = json.load(stream)
entries = state['entries']
if state['enabled'] is not False or not isinstance(entries, dict) or state['error']:
    raise SystemExit('关闭状态或规则清理异常')
entry_errors = any(e['error'] for e in entries.values())
if int(sys.argv[2]) != 0 and not (int(sys.argv[2]) == 1 and entry_errors):
    raise SystemExit('关闭命令异常退出，无法确认清理结果')
print(len(entries))
PY
      ) || {
        cat "$report" >&2
        die '无法确认关闭状态，保留原工具及清理重试机制，请检查状态后重试'
      }
      [[ "$remaining" =~ ^[0-9]+$ ]] || die '关闭状态中的待清理数量无效'
      ((remaining > 0)) || break
      if ((previous >= 0 && remaining >= previous)); then
        cat "$report" >&2
        die '规则清理没有进展，保留原工具及清理重试机制，请检查状态后重试'
      fi
      echo "仍有 $remaining 条记录待清理，继续自动清理"
      previous=$remaining
    done
    # off requests a nonblocking timer stop. Drain systemd jobs before taking
    # the runtime lock, otherwise a pending tick could wait on our own lock.
    systemctl stop brutal-watch.timer brutal-watch.service \
      || die '无法停止 watcher 定时器或任务，尚未改动工具或模块'
  fi
  if systemctl is-active --quiet brutal-watch.timer || systemctl is-active --quiet brutal-watch.service; then
    die 'watcher 仍在运行，自动停止未完成（请检查已安装命令和 systemd 状态）；尚未改动工具或模块'
  fi
}

acquire_runtime_lock() {
  exec 8>/run/brutal-watch.lock
  flock -n 8 || die '关闭后仍有其他 brutal-watch 命令占用运行锁，请稍后重试'
}

release_runtime_lock() { flock -u 8; exec 8>&-; }

check_uninstall_platform() {
  [[ $(uname -s) == Linux && $(id -u) == 0 && -d /run/systemd/system ]] \
    || die '请在使用 systemd 的 Linux 宿主机上以 root 卸载'
  local dependency
  for dependency in python3 flock systemctl ip; do
    command -v "$dependency" >/dev/null || die "卸载缺少命令：$dependency"
  done
}

uninstall_main() {
  check_uninstall_platform
  acquire_install_lock
  trap cleanup EXIT
  umask 077
  TMP_WORK=$(mktemp -d /tmp/brutal-watch-uninstall.XXXXXXXX)
  project_source
  local options=()
  [[ "$KEEP_MODULE" == 0 ]] || options+=(--keep-module)
  [[ "$UNLOAD_MODULE" == 0 ]] || options+=(--unload-module)
  # Recover a node stopped by an interrupted uninstall before any new preflight can fail.
  acquire_runtime_lock
  python3 "$SOURCE_DIR/scripts/uninstall.py" recover-node
  release_runtime_lock
  python3 "$SOURCE_DIR/scripts/uninstall.py" preflight ${options[@]+"${options[@]}"}
  stop_existing_watch
  acquire_runtime_lock
  python3 "$SOURCE_DIR/scripts/uninstall.py" remove ${options[@]+"${options[@]}"}
  release_runtime_lock
}

main() {
  parse_args "$@"
  if [[ "$UNINSTALL" == 1 ]]; then
    uninstall_main
    return
  fi
  check_platform
  acquire_install_lock
  trap cleanup EXIT
  umask 077
  TMP_WORK=$(mktemp -d /tmp/brutal-watch-install.XXXXXXXX)
  check_docker
  install_dependencies
  project_source
  if [[ "$CONFIGURE_NODE" == 1 ]]; then
    python3 "$SOURCE_DIR/scripts/setup.py" validate-config ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"}
  else
    python3 "$SOURCE_DIR/scripts/setup.py" verify-node ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"} || die '节点未通过普通 TCP 检查；尚未准备模块或更新工具。可显式使用 --configure-node 或按文档手动调整'
  fi
  prepare_module
  stop_existing_watch
  acquire_runtime_lock
  python3 "$SOURCE_DIR/scripts/setup.py" idle || die '关闭后仍有待清理状态，尚未覆盖工具或模块；请处理清理错误后重新安装'
  python3 "$SOURCE_DIR/scripts/setup.py" config ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"}
  install_tool
  if [[ "$CONFIGURE_NODE" == 1 ]]; then
    python3 "$SOURCE_DIR/scripts/setup.py" configure-node
  fi
  install_module
  python3 "$SOURCE_DIR/scripts/setup.py" resolve-mptcp-block
  release_runtime_lock
  "$WATCH_COMMAND" check
  if [[ "$ENABLE" == 1 ]]; then
    "$WATCH_COMMAND" on
  else
    echo '安装和检查完成，加速保持关闭。执行 brutal-watch on 开启。'
  fi
}
if [[ -z ${BASH_SOURCE[0]:-} || ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
