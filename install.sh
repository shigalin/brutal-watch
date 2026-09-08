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

usage() {
  cat <<'EOF'
用法：bash install.sh [选项]
  --container NAME     目标容器（首次安装默认 xboard-node）
  --ports 2053,443      节点监听端口（首次安装默认 2053）
  --rate-mbps 100       每个 IP 的目标速率
  --ttl-seconds 1800    最后出现后的保留时间
  --max-ips 1024        最大状态条目数
  --compiler POLICY    auto（默认）/ native / docker
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
  command -v docker >/dev/null || die '请先部署需要加速的 Docker 节点；本安装器不会安装或替换节点'
  docker info >/dev/null 2>&1 || die 'Docker 未运行'
  if systemctl is-active --quiet brutal-watch.timer || systemctl is-active --quiet brutal-watch.service; then
    die 'brutal-watch 仍在运行，请先 off；更新不会擅自停止节点或覆盖运行中的工具'
  fi
}

install_packages() {
  local wanted=("$@")
  local missing=() pkg
  for pkg in "${wanted[@]}"; do
    dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null | grep -qx installed || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    apt-get update
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y --no-install-recommends "${missing[@]}"
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

install_module() {
  local kernel source headers override config
  kernel=$(uname -r)
  if [[ -e /proc/net/tcp_brutal/rules ]]; then
    echo '复用已加载的 v2 模块；保留原有安装方式和升级策略'
    return
  fi
  if [[ "$SKIP_MODULE" == 1 ]]; then
    modprobe brutal || die '无法加载现有 brutal 模块'
    [[ -e /proc/net/tcp_brutal/rules ]] || die '当前模块没有 v2 规则接口'
    return
  fi
  if modinfo -F version brutal 2>/dev/null | grep -q '^2\.'; then
    modprobe brutal
    [[ -e /proc/net/tcp_brutal/rules ]] || die '加载后缺少 v2 规则接口'
    echo '复用磁盘上已有的 v2 模块；未覆盖现有模块'
    return
  fi
  if modinfo brutal >/dev/null 2>&1; then
    die '发现旧版或未知 brutal 模块，请先按原安装方式迁移；不会覆盖或强制卸载'
  fi
  install_packages dkms gcc make libc6-dev
  headers="/lib/modules/$kernel/build"
  if [[ ! -f "$headers/Makefile" ]]; then
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y --no-install-recommends "linux-headers-$kernel" \
      || die "无法取得 $kernel 的 headers；定制内核需要其提供方的匹配 headers，不会替换内核"
  fi
  [[ -f "$headers/Makefile" ]] || die '内核 headers 不完整'
  source "$SOURCE_DIR/scripts/module-build.sh"
  config=$(kernel_config "$headers") || die '内核 headers 不完整'
  if grep -q '^CONFIG_CC_IS_CLANG=y' "$config"; then
    [[ "$COMPILER" != docker ]] || die 'Clang 内核请使用 --compiler auto 或 native；Docker 编译目前仅支持 GCC'
    install_packages clang lld llvm
  fi
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
  install_dependencies
  project_source
  python3 "$SOURCE_DIR/scripts/setup.py" idle
  python3 "$SOURCE_DIR/scripts/setup.py" config ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"}
  install_tool
  if [[ "$CONFIGURE_NODE" == 1 ]]; then
    python3 "$SOURCE_DIR/scripts/setup.py" configure-node
  else
    python3 "$SOURCE_DIR/scripts/setup.py" verify-node || die '工具已安装；节点未通过普通 TCP 检查。可显式使用 --configure-node 或按文档手动调整'
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
