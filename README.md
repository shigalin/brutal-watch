# brutal-watch

在 Linux Docker 节点宿主机上，自动为客户端公网 IP 配置 **TCP Brutal v2**。

- 默认每分钟采集一次，每个 IP 共享 **100 Mbps**。
- 再次发现 IP 就续期；最后出现 **30 分钟**后清理。
- `on / off / status / check`，支持开机自启和状态容量上限。
- 使用 HyNetworks 官方原版模块与标准 DKMS，不维护自定义内核补丁。

## 一键安装

适用于 **Debian/Ubuntu、Linux >=5.10、x86_64/aarch64、systemd，以及已经运行的 Docker host 网络节点**。以 root 执行，端口替换成自己的节点端口。

缺少 `docker` 命令时，只有已安装的 `docker.io` 不包含客户端、Docker 服务或 `docker.socket` 已激活，且软件源提供 `docker-cli` 候选版本，安装器才会自动补装客户端，再继续检查现有节点。这适用于 Debian 13 的拆包情况；若 `docker.io` 本身包含客户端（如 Debian 12、Ubuntu 24.04），会提示检查 PATH 或修复原软件包。不会自动安装或替换 Docker 服务端或部署节点。

下面的命令会先准备依赖；需要编译模块时，会先取得匹配 headers 并检查编译工具链，再备份并调整目标节点 Compose 的 `GODEBUG`、重建该容器。普通 TCP 验证通过后才安装并加载模块、开启加速及开机自启。**重建容器会短暂断开连接。**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh) \
  --container xboard-node --ports 2053 --configure-node --enable
```

若节点已经配置为普通 TCP，可以省略 `--configure-node`。只安装、不调整节点或启用加速：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh) \
  --container xboard-node --ports 2053
```

安装前未通过普通 TCP 检查会停止，不会硬开加速。默认将旧 v2 模块升级到项目固定的最新官方稳定版（当前 **v2.0.1**），版本相同则跳过重编译；只有显式传入 `--skip-module` 才保留现有 v2 模块。目标版本随本项目更新，不在安装时动态追踪上游未核验的发布。

安装器先只读校验有效配置（首次安装使用本次参数，已有配置不覆盖）。未传 `--configure-node` 时，再验证现有节点的普通 TCP 监听；随后准备模块所需的 headers、DKMS 和编译工具链，以上完成后才关闭已有加速。配置写入及显式授权的节点重建仍在关闭、清理并取得运行锁后执行。

也可从仓库安装：

```bash
git clone https://github.com/shigalin/brutal-watch.git
cd brutal-watch
bash install.sh --container xboard-node --ports 2053 --configure-node --enable
```

安装其他分支或固定提交时，设置 `BRUTAL_WATCH_REF`，并从同一 ref 下载入口脚本。已有配置不会被安装参数覆盖；重复安装会自动执行 `brutal-watch off` 并确认规则清理完成，保留 `/etc/brutal-watch/config.json` 和状态文件及其正常清理流程。

## 使用

以下命令均在**节点所在的 Linux 宿主机**上以 root 执行，不是在客户端电脑或节点容器内执行。

需要停用并改用其他加速时，直接使用[一键卸载](#一键卸载)，`off` 仅关闭加速，不删除程序或模块。

### 确认容器和端口

先查看容器名称，再检查网络模式：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}'
docker inspect -f '{{.HostConfig.NetworkMode}}' xboard-node
```

网络模式应为 `host`。安装参数 `--ports` 填节点实际监听的 TCP 端口；多个端口必须都属于这个容器的主进程，不能把 SSH、Nginx 等其他进程的端口填进来。

例如，目标容器为 `xboard-node`，监听 443 和 8443，首次安装时设置每个 IP 80 Mbps、保留 20 分钟：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh) \
  --container xboard-node --ports 443,8443 \
  --rate-mbps 80 --ttl-seconds 1200 --max-ips 1024 \
  --configure-node --enable
```

### 开启、关闭和开机自启

```bash
brutal-watch on      # 开启，每分钟续期，并启用开机自启
brutal-watch off     # 停止新增，取消自启，清理自己的规则及路由
brutal-watch status  # 查看 IP、剩余有效期、速率和实际规则/路由状态
brutal-watch check   # 只读检查
```

`on` 会立即采集一次，之后每分钟运行。不需要额外执行 `systemctl enable`；`off` 会同时取消自启，但不会卸载内核模块或强制断开已有连接。

开启后，让客户端重新建立节点连接再观察。**开启前已经建立的长连接不会立即切换到 Brutal**，仅续期也不会改变这条旧连接的拥塞算法。

### 查看是否正常工作

```bash
brutal-watch status
```

输出为 JSON，重点看以下字段：

| 字段 | 含义与正常状态 |
| --- | --- |
| `enabled` | `true` 表示工具处于开启状态 |
| `timer.is-enabled` | `enabled` 表示已设置开机自启 |
| `timer.is-active` | `active` 表示定时器正在运行 |
| `module_loaded` | `true` 表示模块已加载，不代表加速一定已开启 |
| `last_scan` | 最近一次成功采集的 Unix 时间戳，应随定时任务更新 |
| `entries` | 当前维护的 IP 记录；无人连接时可以为空 |
| `entries.<IP>.rate_mbps` | 该 IP 的配置速率，默认 100 Mbps |
| `entries.<IP>.remaining_seconds` | 距离规则到期的剩余秒数 |
| `entries.<IP>.rule_verified` / `route_verified` | 都为 `true` 表示当前规则和路由核对通过 |
| `entries.<IP>.members` | 该规则组中的连接数；大于 0 表示已有连接加入组 |
| `error` / `entries.<IP>.error` | 正常时为空字符串，有内容时先处理错误 |
| `blocked_reason` | 非空表示存在阻止开启的故障，不能直接删标记绕过 |

如果同时安装了官方 `brutalctl`，还可以观察连接数和累计发送量：

```bash
brutalctl list
```

`MEMBERS` 表示连接数，`SENT(MB)` 增长表示组内有发送流量。**本工具使用协议号 234 管理路由，而 `brutalctl` 的 `ROUTE` 列只识别其自身的 233 路由，因此可能显示 `no`；请以 `brutal-watch status` 中的 `route_verified` 为准。**

规则配置成功、存在流量和实际网速提升是不同的验证结果，不能只凭 `rate_mbps: 100` 就认为实际速度达到了 100 Mbps。

### 理解 30 分钟续期

假设 10:00 采集到某个 IP，它会被保留到 10:30；10:12 再次采集到，就延长到 10:42。此后没有再出现，才会在到期后的清理轮次移除。

- 仍有已建立连接时，每轮都会续期；空闲长连接也会续期。
- IP 暂时没出现，不会立即删除，而是保留到最后一次出现后的有效期结束。
- 采集失败不等同于所有用户离线，不会据此批量删除。
- 删除规则只影响后续新连接，已有组成员仍可继续使用原速率直到关闭。
- 大量 IP 或清理错误可能导致分轮处理，详见 [容量与清理机制](OPERATIONS.md#状态清理和容量)。

### 修改速率、有效期、端口或排除 IP

先关闭并查看状态：

```bash
brutal-watch off
brutal-watch status
```

确认 `enabled` 为 `false`、`entries` 为 `{}`，且没有清理错误后，再备份配置：

```bash
cp -p /etc/brutal-watch/config.json "/etc/brutal-watch/config.json.bak.$(date +%Y%m%d-%H%M%S)"
```

用文本编辑器修改 `/etc/brutal-watch/config.json`。默认内容如下，请保留所有字段，不要直接覆盖已有的自定义配置：

```json
{
  "container": "xboard-node",
  "ports": [2053],
  "rate_mbps": 100,
  "ttl_seconds": 1800,
  "max_ips": 1024,
  "exclude_cidrs": []
}
```

| 配置项 | 设置方法 |
| --- | --- |
| `container` | Docker 容器名 |
| `ports` | TCP 监听端口列表，例如 `[443, 8443]` |
| `rate_mbps` | 每个 IP 共享的目标 Mbps，例如 50；不是 MB/s |
| `ttl_seconds` | 最后出现后的保留秒数，例如 1800 为 30 分钟、3600 为 1 小时 |
| `max_ips` | 最多保存的 IP 条目数，默认 1024；达到上限会报告并暂停添加新 IP |
| `exclude_cidrs` | 不加入加速的地址或网段，例如 `["203.0.113.10/32", "2001:db8::10/128"]`；示例地址需换成实际地址 |

修改后先检查，再开启：

```bash
brutal-watch check
brutal-watch on
```

安装时的参数只用于首次生成配置。重复安装并传入不同参数，不会覆盖现有配置；后续调整按上述步骤操作。这里的 `ports` 只指定采集哪些端口，不会修改节点本身的监听端口。单纯修改这些配置不需要重建节点容器，但新连接才能完整采用调整后的规则。

### 查看日志与排查

最近 30 条日志、持续观察日志和查看定时器：

```bash
journalctl -u brutal-watch.service -n 30 --no-pager
journalctl -u brutal-watch.service -f
systemctl status brutal-watch.timer --no-pager
```

`brutal-watch.service` 是一次性任务，每轮结束后显示 `inactive` 是正常的；应结合定时器状态、日志以及 `last_scan` 是否更新判断。持续查看日志时，按 `Ctrl+C` 只退出日志查看，不会关闭加速。

| 现象 | 处理方法 |
| --- | --- |
| 提示端口未就绪或不是目标进程监听 | 核对容器名、`host` 网络模式和节点端口，等待节点完成启动 |
| 提示协议为 262 / MPTCP | 使用安装器的 `--configure-node`，安装器会先自动关闭加速并清理；显式开启 `tcp_multi_path` 的配置需先手动处理 |
| 提示模块未加载 | 已安装匹配模块时可执行 `modprobe brutal`，然后 `brutal-watch check`；缺模块则重新运行安装器 |
| 提示规则或路由冲突 | 不要删除其他程序的规则；核对错误中的 IP 及现有配置，处理冲突后再检查 |
| 关闭后 `shutdown_pending: true`、连接数为 `null` | 表示存量连接是否完全退出尚不确定，不等于定时器仍在添加规则；检查 `enabled`、定时器和 `entries` |
| 提示内核 Oops 或无法安全检查 socket | 停止开启操作，先恢复主机或处理权限；不要反复重试、删除故障标记或强卸载模块 |

### 更新工具和内核模块

直接重新运行安装入口，不需要先手动停止：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh)
```

安装器完成配置、节点和模块状态预检，并准备好源码及编译依赖后，会调用固定路径 `/usr/local/sbin/brutal-watch off`；记录较多时自动分批继续清理，直到本工具规则与路由清理完成，再等待定时器和任务停止、取得运行锁并更新文件。不会停止节点容器或强断存量连接。残留条目含旧错误、导致 `off` 返回 1 时，只要后续批次的剩余数量持续下降，就继续清理；顶层错误（如 timer 操作失败）、异常退出、数量不下降或状态无法确认时中断，中止时会输出本轮清理报告，不覆盖工具和模块，保留原有清理重试机制。

若磁盘已是目标版本、内存仍为旧版，预检会在 `off` 前提示待重启并退出，保持当前加速状态。需要构建时，已有源码归属、外部 DKMS 覆盖配置检查及上游归档下载、SHA256 校验、解包均在 `off` 前完成；新源码暂存于临时目录，正式写入、编译和 DKMS 安装仍在关闭后执行，写入前再次检查归属。

重复安装默认更新工具，并将此前安装的旧 v2 模块升级至当前目标版本；包括本项目的 `2.0.0-bw644db52`、官方 DKMS 或手工安装的 v2.0.0。需要升级时使用本项目的 DKMS 构建入口，不强制覆盖 DKMS 拒绝替换的模块，不自动删除旧源码或旧 DKMS 注册。已有比目标更新的版本、v1 或无法识别的版本会停止，避免降级或错误迁移。安装成功后默认保持关闭；希望检查通过后自动开启，可在安装命令后加 `--enable`。

**`off` 不会卸载旧模块。** 新版安装到磁盘后，如果 `/sys/module/brutal/version` 仍为旧版，安装器会以非零状态退出并明确提示“升级尚未生效”，即使传入 `--enable` 也不会开启加速。此时请安排维护窗口手动重启主机（会中断连接），重启后重新运行上面的安装命令验证；不会重复编译已经安装的目标版本。安装器不会自动重启、强卸载模块或断开现有连接。这里的版本一致性限制属于安装器；独立的 `brutal-watch check/on` 只检查规则接口等运行条件，不校验磁盘与加载版本，不能把它们通过视为模块升级已生效。

确认安装器成功结束后，再执行：

```bash
modinfo -F version brutal       # 当前内核在磁盘上的模块版本
cat /sys/module/brutal/version # 当前实际加载版本；本次升级两者应均为 2.0.1
brutal-watch check && brutal-watch on
```

仅更新工具而保留旧模块时，加 `--skip-module`；但磁盘和已加载版本不一致时仍会停止，不能用它绕过待重启状态。编译或 DKMS 安装失败时保持加速关闭，修复错误后重跑安装器；若磁盘仍解析到旧版本，核查 `dkms status` 和 `modinfo -n brutal`，不要强制覆盖或手动删除未知模块。

更新会保留已有配置和状态，不自动拉取节点新镜像。已经验证为普通 TCP 的节点无需再次传 `--configure-node`。更新节点容器本身时，也应先关闭加速，并保留 `GODEBUG=multipathtcp=0`，重新验证后再开启。旧 DKMS 记录可能仍用于其他已安装内核，本安装器只升级当前运行内核；其他内核需在切换后检查并重新运行安装器。

完整安装参数见 `bash install.sh --help`；内核模块的升级与重编译边界见下文。

## 已纳入的兼容性处理

| 问题 | 处理方式 |
| --- | --- |
| 宿主机 GCC 10、内核由 GCC 13 编译 | 读取内核配置，优先用匹配 GCC；没有则用官方 GCC 容器编译，不取消内核安全编译选项 |
| Clang 构建的内核 | `auto` / `native` 安装本机 clang、lld、llvm，沿用上游 `LLVM=1` 构建；`docker` 目前仅支持 GCC |
| XanMod 等带 BBRv3 补丁的内核 | 使用官方 v2.0.1 对 `tso_segs` / `min_tso_segs` 的自动检测，不维护本地内核补丁 |
| Debian 官方旧内核的 headers 已退出当前源 | 先使用当前 APT 源；没有候选包时，按已安装内核包的精确版本查找 Debian Snapshot。通过临时隔离的 APT 索引和系统 `debian-archive-keyring` 验证 Release/Packages，再用已认证包记录中的 SHA256 校验下载文件，核对包名称、版本、架构和源码来源并模拟安装；需要删除或替换已有包、安装内核或相关系统服务时停止 |
| 定制内核或无法确认来源的内核缺 headers | 在调整节点前停止，提示准备提供方的匹配 headers；不会猜测版本、替换内核或重启服务器 |
| `latin-1` 等输出编码不能打印中文 | 命令行工具及安装辅助程序使用 UTF-8 输出，不修改宿主机 locale |
| Compose 调整失败 | 报告失败阶段和安全的错误摘要，区分容器未重建、回滚成功、文件恢复失败和容器回滚失败；不输出完整配置或命令捕获内容 |
| MPTCP 与 Brutal 冲突 | 使用 Go 官方 `GODEBUG=multipathtcp=0`；开启前直接读取真实监听 socket 的 `SO_PROTOCOL`，要求普通 TCP（6） |
| `tcp_multi_path=false` 仍使用 MPTCP | 不依赖布尔配置猜测；若程序显式开启 MPTCP、环境变量不起作用，拒绝开启并回滚自动调整 |
| `ss` 卡在内核诊断 | 采集改用 `/proc`，状态不调用 `ss`；外部命令超时后不无限等待无法退出的子进程 |
| 已经发生内核 Oops | 检测内核故障标志后拒绝重新开启，不因配置已改好就误认为内核已经恢复 |
| VPS 的 `/32` 地址、网关需要 `onlink` | 保留原网关及 `onlink`；新增精确路由，不覆盖已有精确路由或默认路由 |
| 更换内核 | 新安装交由 DKMS 重建；需要匹配 headers 及编译器，容器编译还要求 Docker 可用 |
| 重装、失败、异常退出 | 保留配置/状态；安装互斥锁；规则归属检查；故障记录保留重试；Compose 有备份和恢复流程 |

安装器仅修改明确授权的目标容器环境变量，保留其他 `GODEBUG` 选项，并验证使用相同镜像。高级 YAML anchor 或动态 `GODEBUG` 插值无法可靠保留时，会要求手动调整。

归档回退仅适用于 Debian 的 `amd64`/`arm64` 官方内核，不修改系统 APT 软件源及常规索引；临时源、索引和下载文件位于本次安装的临时目录。历史快照仅放宽该临时源的 `Valid-Until` 时效检查，保留签名与强校验要求；Snapshot API 的文件标识只用于定位文件，不能替代来源认证。签名验证失败、密钥环缺失、归档依赖无法唯一匹配或安装计划需要改变已有包时停止。APT 安装进度及错误会实时显示，软件源 URL 会被隐藏以避免暴露认证信息。

归档包的模拟安装和正式安装共用本次临时版本约束：保留已安装软件包的精确版本，防止 APT 顺带升级 `linux-libc-dev` 等依赖。原有 APT 偏好配置继续用于其他版本选择；不写入系统 pin/hold，也不将已有自动安装依赖改成手动安装。若保留现有版本无法满足依赖，输出 APT 冲突详情并停止。

历史 headers 的归档查找和下载默认最多自动等待 2 分钟。超时后先停止当前请求，再通过终端询问是否继续；只有明确输入 `y` 或 `yes` 并回车才继续，60 秒未完成回答、回车、拒绝、取消或无可用终端均停止。确认后重试当前请求，本轮累计等待最多 30 分钟，每个索引最多 15 分钟；等待用户回答的时间不计入限额。每 30 秒输出等待进度。候选分支的详细错误先缓存，全部失败、拒绝继续或总时限耗尽时才汇总，成功恢复时不混入此前分支的 APT 报错。这些限额不会在正式包安装中途终止 dpkg。

自动化部署可显式传入 `--archive-wait-minutes N`（1–30 的整数），预先授权本次归档查找、下载的总等待分钟数。例如 `--archive-wait-minutes 30` 允许无终端执行最多等待 30 分钟；到达所选上限直接停止，不再询问。这是仅对本次运行生效的参数，不写入节点或 watcher 配置。不传此参数时仍保留上述两分钟确认规则。

已有匹配 headers 继续复用；模块默认升级到项目目标版本，同版本或显式 `--skip-module` 才复用。Ubuntu 继续使用现有软件源，缺少精确 headers 时停止。不会改装最新内核来绕过失败。

**同版本或 `--skip-module` 复用此前手工安装的 v2 模块，不会将其转换为 DKMS 安装**；默认升级旧版本则通过 DKMS 安装新版。已有外部升级任务仍需由管理员协调，避免多个安装器同时管理同一模块。

自动编译支持 GCC 和 Clang 构建的内核。Clang 使用发行版提供的 LLVM 工具链；若定制内核要求其他工具链版本，仍需自行准备兼容版本。DKMS 后续重建也需要对应工具链可用。受限环境若禁止 root 通过 `pidfd_getfd` 检查目标进程 socket，会拒绝开启，不降低协议验证要求。

## 使用边界

- 新增规则只影响之后新建的 TCP 连接；不会自动接管首次采集到的旧连接。
- 删除规则或 `off` 不强断现有连接，已有 Brutal 连接要等自然关闭。状态中的未知连接数用 `null` 表示，不伪装成 0。
- 速率按 IP 共享，不按账号或连接分配；同 NAT 用户以及发往该 IP 的其他 TCP 服务可能共享。
- 100 Mbps 是配置目标，不保证实际吞吐，也不是套餐限速或计费机制。
- 不与原有 `multiplex.brutal` / `smux.brutal-opts` 叠加使用；不设置系统默认拥塞算法。
- 不要在加速开启期间将节点改回 MPTCP。定时检查发现不安全监听会撤下本工具规则，但无法消除外部重建到下一次采集之间的窗口。
- 本工具不会自动修复已发生的内核崩溃、强卸载模块、强杀节点或重启服务器。

详细说明：[运维与状态语义](OPERATIONS.md) · [隔离验证记录](diagnostics/README.md)。

## 一键卸载

在 Linux 宿主机上以 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh) --uninstall --unload-module
```

已有仓库也可直接执行 `bash install.sh --uninstall --unload-module`。`--unload-module` 明确允许移除当前内存中的 `brutal` 模块，**包括官方或手工安装的模块**；它不会扩大磁盘文件的删除范围。卸载与安装使用同一互斥锁；不要求匹配内核 headers 或编译工具链，不安装依赖。远程入口仍需下载项目文件。

若存在上次卸载留下的节点恢复记录，先恢复对应容器，再进行卸载预检。随后关闭加速、取消开机自启，分批清理本工具的规则和路由，确认清理完成后删除：

- `brutal-watch` 命令、systemd timer/service、`/etc/brutal-watch` 配置与 `/var/lib/brutal-watch` 状态。需要保留配置时请提前备份。
- 本项目安装并有归属标记的所有 TCP Brutal DKMS 版本（包括旧版本）、对应源码和 DKMS 配置。
- 不再被其他 DKMS 配置引用的构建入口，以及与本项目源码目录内已编译文件一致的 `brutalctl`。

**上述命令允许中断连接。** 会先尝试卸载内存中的 `brutal` 模块；若未成功，则检查配置中的目标节点容器，仍在运行时先停止，在 60 秒重试窗口内等待未关闭完的连接释放模块。没有卸载恢复记录且原本已停止的容器同样等待重试，但保持停止状态。重试超时会显示 `rmmod` 的实际错误，不直接认定为其他进程占用。仍有其他 TCP Brutal 规则、清理失败、状态损坏或文件归属不明时会报错停止；不会使用 `rmmod -f` 或自动重启宿主机。

停止节点前，先原子保存容器 ID、名称和配置端口到 `/var/lib/brutal-watch/uninstall-node.json`。模块及 DKMS 操作完成或失败、收到 Ctrl+C/SIGTERM 时，都会尝试恢复该 ID 对应的容器；恢复期间暂不响应重复的 Ctrl+C/SIGTERM。启动后在 60 秒检查窗口内，每秒检查一次，要求同一容器主进程连续 3 次持有全部配置 TCP 监听端口，才清除恢复记录。Docker 启动命令最多等待 30 秒，单次状态查询最多 5 秒。该检查证明进程及监听恢复，不代表面板通信或客户端端到端连接已经验证。

恢复失败时保留记录及工具文件并报错；下次执行任一卸载模式都会先重试恢复，即使内核模块已经卸载也不会跳过。容器被删除时不会按同名新容器替代。断电或 SIGKILL 无法当场恢复，需再次执行卸载；不会安装开机恢复任务。旧版卸载没有此记录，无法据此自动恢复旧版遗留的停止状态。

仅执行 `bash install.sh --uninstall` 时，不会凭相同版本号推断已加载模块的来源。若模块仍在内存，会先关闭加速并清理本工具规则，随后返回 2，本次不再继续删除文件，也不停止节点；这不表示此前已删除的文件仍然存在。确认要移除当前模块后，可重跑带 `--unload-module` 的命令。若重跑时模块仍被占用、配置文件已不存在，会提示无法确定目标容器并返回 1；请手动停止占用模块的服务后重试。

模块磁盘核验、容器恢复或工具清理失败时，保留源码中的归属标记、已编译的 `brutalctl` 和项目 DKMS 配置，直到后续步骤成功再删除。即使 DKMS 注册已移除，修复故障后重跑也能继续确认辅助文件归属。

只删除 watcher、保留模块供其他工具使用：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh) --uninstall --keep-module
```

`--keep-module` 同时保留 `brutalctl` 和 DKMS 构建入口，不停止节点容器，不能与 `--unload-module` 混用。`--uninstall` 不能与安装参数混用。

卸载不改动节点 Compose（包括 `GODEBUG=multipathtcp=0`），不删除 Docker、内核 headers、编译器等共享依赖。官方或手工安装、被安装器复用的外部磁盘模块不会冒认成本项目模块删除；DKMS 也可能恢复安装本项目前已有的模块。检测到外部磁盘模块、重新加载的模块或归属不明的 `brutalctl` 等残留时，会明确列出待处理项。

退出码 **0** 表示所选卸载范围完成；**1** 表示失败；**2** 表示仍有待处理项（可能保留整个安装，也可能只剩模块或辅助文件），不能当作完全卸载；**130** 表示 Python 卸载流程收到 Ctrl+C/SIGTERM 后中断（恢复失败则返回 1）。输出会说明已完成的步骤和保留内容，可重复执行同一命令核验或继续清理。

## 开发验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -p 'test_*.py' -v
bash -n install.sh scripts/module-build.sh
```

单元测试使用模拟主机操作，不安装内核模块或修改本机网络。隔离内核测试位于 `diagnostics/`，不要把普通 Docker 容器当成独立内核环境。

真实 APT 的源隔离、空状态文件、签名拒绝和历史索引时效检查另有[一次性 Debian 容器验证](diagnostics/README.md#debian-归档签名索引集成验证)，不由上述单元测试代替。

## 上游

- [HyNetworks/tcp-brutal](https://github.com/HyNetworks/tcp-brutal)：官方模块、规则接口和 `brutalctl`，遵循其 GPL-3.0 许可。
- [Go 官方 GODEBUG](https://go.dev/doc/godebug#go-124)：MPTCP 默认行为及关闭方法。
- [DKMS](https://github.com/dell/dkms)：使用标准 per-module 构建配置，不修改模块源码。

本项目不是上述项目的官方发行版。
