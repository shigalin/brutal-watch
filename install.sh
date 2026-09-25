#!/usr/bin/env bash
# Install the official TCP Brutal module and this independent IP rule manager.
set -Eeuo pipefail
PROJECT_REPO=shigalin/brutal-watch
PROJECT_REF=${BRUTAL_WATCH_REF:-main}
UPSTREAM_COMMIT=644db5226173dba741fe2b593082702fa7b16108
UPSTREAM_SHA256=8e37baa6ac7844c618005e90f6a204fb858865f25988a62de0d9a21cdfa08be6
DKMS_VERSION=2.0.0-bw644db52
MODULE_NAME=tcp-brutal
LIBEXEC=/usr/local/libexec/brutal-watch
CONFIGURE_NODE=0
ENABLE=0
SKIP_MODULE=0
COMPILER=auto
TMP_WORK=
CONFIG_ARGS=()
ARCHIVE_ARGS=()
PACKAGE_INDEX_UPDATED=0

usage() {
  cat <<'EOF'
用法：bash install.sh [选项]
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
默认只安装并检查，不调整节点、不启用加速。运行中的旧版本须先 off。
只支持 Debian/Ubuntu、systemd、x86_64/aarch64、现有 Docker host 网络节点。
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
      --configure-node) CONFIGURE_NODE=1; shift ;;
      --enable) ENABLE=1; shift ;;
      --skip-module) SKIP_MODULE=1; shift ;;
      --compiler)
        (($# >= 2)) || die '--compiler 缺少参数'
        COMPILER=$2; shift 2 ;;
      --archive-wait-minutes)
        (($# >= 2)) || die '--archive-wait-minutes 缺少参数'
        [[ "$2" =~ ^([1-9]|[12][0-9]|30)$ ]] || die '--archive-wait-minutes 必须是 1–30 的整数'
        ARCHIVE_ARGS=(--archive-wait-minutes "$2"); shift 2 ;;
      --container|--ports|--rate-mbps|--ttl-seconds|--max-ips)
        (($# >= 2)) || die "$1 缺少参数"
        CONFIG_ARGS+=("$1" "$2"); shift 2 ;;
      *) die "未知选项 $1" ;;
    esac
  done
  [[ "$COMPILER" == auto || "$COMPILER" == native || "$COMPILER" == docker ]] || die 'compiler 必须是 auto/native/docker'
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
  if systemctl is-active --quiet brutal-watch.timer || systemctl is-active --quiet brutal-watch.service; then
    die 'brutal-watch 仍在运行，请先 off；更新不会擅自停止节点或覆盖运行中的工具'
  fi
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

module_mode() {
  if [[ -e /proc/net/tcp_brutal/rules ]]; then
    echo loaded
  elif [[ "$SKIP_MODULE" == 1 ]]; then
    echo existing
  elif modinfo -F version brutal 2>/dev/null | grep -q '^2\.'; then
    echo existing
  elif modinfo brutal >/dev/null 2>&1; then
    die '发现旧版或未知 brutal 模块，请先按原安装方式迁移；不会覆盖或强制卸载'
  else
    echo build
  fi
}

prepare_module() {
  local kernel headers config selected image mode
  mode=$(module_mode) || return 1
  [[ "$mode" == build ]] || return 0
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
    echo '复用已加载的 v2 模块；保留原有安装方式和升级策略'
    return
  fi
  if [[ "$mode" == existing ]]; then
    modprobe brutal || die '无法加载现有 brutal 模块'
    [[ -e /proc/net/tcp_brutal/rules ]] || die '当前模块没有 v2 规则接口'
    echo '复用磁盘上已有的 v2 模块；未覆盖现有模块'
    return
  fi
  headers="/lib/modules/$kernel/build"
  [[ -f "$headers/Makefile" ]] || die '内核 headers 不完整'
  source="/usr/src/$MODULE_NAME-$DKMS_VERSION"
  if [[ ! -d "$source" ]]; then
    curl -fL --connect-timeout 15 --max-time 120 --retry 2 \
      "https://codeload.github.com/HyNetworks/tcp-brutal/tar.gz/$UPSTREAM_COMMIT" -o "$TMP_WORK/tcp-brutal.tar.gz"
    printf '%s  %s\n' "$UPSTREAM_SHA256" "$TMP_WORK/tcp-brutal.tar.gz" | sha256sum -c -
    mkdir "$TMP_WORK/module"
    extract_archive "$TMP_WORK/tcp-brutal.tar.gz" "$TMP_WORK/module"
    (cd "$TMP_WORK/module" && PACKAGE_VERSION="$DKMS_VERSION" bash scripts/mkdkmsconf.sh > dkms.conf)
    printf '%s\n' "$UPSTREAM_COMMIT" > "$TMP_WORK/module/.brutal-watch-upstream"
    mv "$TMP_WORK/module" "$source"
  fi
  [[ $(cat "$source/.brutal-watch-upstream" 2>/dev/null) == "$UPSTREAM_COMMIT" ]] || die '源码目录已有未知内容，拒绝覆盖'
  install -d -m 755 "$LIBEXEC"
  install -m 755 "$SOURCE_DIR/scripts/module-build.sh" "$LIBEXEC/module-build"
  override="/etc/dkms/$MODULE_NAME-$DKMS_VERSION.conf"
  if [[ -e "$override" ]] && ! grep -q '^# Managed by brutal-watch$' "$override"; then
    die '发现外部 DKMS 覆盖配置，拒绝覆盖'
  fi
  cat > "$override" <<EOF
# Managed by brutal-watch
MAKE[0]="$LIBEXEC/module-build $COMPILER \${kernel_source_dir}"
EOF
  dkms status -m "$MODULE_NAME" -v "$DKMS_VERSION" | grep -q . || dkms add -m "$MODULE_NAME" -v "$DKMS_VERSION"
  dkms build -m "$MODULE_NAME" -v "$DKMS_VERSION" -k "$kernel" -j 2
  dkms install -m "$MODULE_NAME" -v "$DKMS_VERSION" -k "$kernel"
  make -C "$source/tools"
  install -m 755 "$source/tools/brutalctl" /usr/local/bin/brutalctl
  depmod -a "$kernel"
  modprobe brutal
  [[ -e /proc/net/tcp_brutal/rules ]] || die '模块加载后未提供 v2 规则接口，请检查模块签名和内核日志'
}

install_tool() {
  install -d -m 700 /etc/brutal-watch /var/lib/brutal-watch
  install -d -m 755 "$LIBEXEC"
  install -m 755 "$SOURCE_DIR/scripts/module-build.sh" "$LIBEXEC/module-build"
  install -m 755 "$SOURCE_DIR/brutal_watch.py" /usr/local/sbin/brutal-watch
  install -m 644 "$SOURCE_DIR/brutal-watch.service" /etc/systemd/system/brutal-watch.service
  install -m 644 "$SOURCE_DIR/brutal-watch.timer" /etc/systemd/system/brutal-watch.timer
  install -m 600 "$SOURCE_DIR/OPERATIONS.md" /etc/brutal-watch/OPERATIONS.md
  systemctl daemon-reload
  systemd-analyze verify /etc/systemd/system/brutal-watch.service /etc/systemd/system/brutal-watch.timer
}

acquire_locks() {
  command -v flock >/dev/null || die '缺少 flock（util-linux）'
  exec 9>/run/brutal-watch-install.lock
  flock -n 9 || die '已有安装任务正在运行'
  exec 8>/run/brutal-watch.lock
  flock -n 8 || die 'brutal-watch 正在处理任务，请稍后安装'
}

release_runtime_lock() { flock -u 8; exec 8>&-; }

main() {
  parse_args "$@"
  check_platform
  acquire_locks
  trap cleanup EXIT
  umask 077
  TMP_WORK=$(mktemp -d /tmp/brutal-watch-install.XXXXXXXX)
  check_docker
  install_dependencies
  project_source
  python3 "$SOURCE_DIR/scripts/setup.py" idle
  python3 "$SOURCE_DIR/scripts/setup.py" config ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"}
  if [[ "$CONFIGURE_NODE" == 1 ]]; then
    prepare_module
    install_tool
    python3 "$SOURCE_DIR/scripts/setup.py" configure-node
  else
    python3 "$SOURCE_DIR/scripts/setup.py" verify-node || die '节点未通过普通 TCP 检查；尚未准备模块或更新工具。可显式使用 --configure-node 或按文档手动调整'
    prepare_module
    install_tool
  fi
  install_module
  python3 "$SOURCE_DIR/scripts/setup.py" resolve-mptcp-block
  release_runtime_lock
  brutal-watch check
  if [[ "$ENABLE" == 1 ]]; then
    brutal-watch on
  else
    echo '安装和检查完成，加速保持关闭。执行 brutal-watch on 开启。'
  fi
}
if [[ -z ${BASH_SOURCE[0]:-} || ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
