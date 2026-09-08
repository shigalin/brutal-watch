# brutal-watch

在 Linux Docker 节点宿主机上，自动为客户端公网 IP 配置 **TCP Brutal v2**。

- 默认每分钟采集一次，每个 IP 共享 **100 Mbps**。
- 再次发现 IP 就续期；最后出现 **30 分钟**后清理。
- `on / off / status / check`，支持开机自启和状态容量上限。
- 使用 HyNetworks 官方原版模块与标准 DKMS，不维护自定义内核补丁。

## 一键安装

适用于 **Debian/Ubuntu、Linux >=5.10、x86_64/aarch64、systemd，以及已经运行的 Docker host 网络节点**。以 root 执行，端口替换成自己的节点端口。

下面的命令会安装依赖和官方模块，备份并调整目标节点 Compose 的 `GODEBUG`，重建该容器一次，然后开启加速及开机自启。**重建容器会短暂断开连接。**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh) \
  --container xboard-node --ports 2053 --configure-node --enable
```

若节点已经配置为普通 TCP，可以省略 `--configure-node`。只安装、不调整节点或启用加速：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh) \
  --container xboard-node --ports 2053
```

安装前未通过普通 TCP 检查会停止，不会硬开加速。使用现有 v2 模块时可加 `--skip-module`。

也可从仓库安装：

```bash
git clone https://github.com/shigalin/brutal-watch.git
cd brutal-watch
bash install.sh --container xboard-node --ports 2053 --configure-node --enable
```

安装其他分支或固定提交时，设置 `BRUTAL_WATCH_REF`，并从同一 ref 下载入口脚本。已有配置不会被安装参数覆盖；更新前执行 `brutal-watch off`，保留 `/etc/brutal-watch/config.json` 和 `/var/lib/brutal-watch/state.json`。

## 使用

以下命令均在**节点所在的 Linux 宿主机**上以 root 执行，不是在客户端电脑或节点容器内执行。

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
| 提示协议为 262 / MPTCP | 先 `off` 并确认清理完成，再使用安装器的 `--configure-node`；显式开启 `tcp_multi_path` 的配置需先手动处理 |
| 提示模块未加载 | 已安装匹配模块时可执行 `modprobe brutal`，然后 `brutal-watch check`；缺模块则重新运行安装器 |
| 提示规则或路由冲突 | 不要删除其他程序的规则；核对错误中的 IP 及现有配置，处理冲突后再检查 |
| 关闭后 `shutdown_pending: true`、连接数为 `null` | 表示存量连接是否完全退出尚不确定，不等于定时器仍在添加规则；检查 `enabled`、定时器和 `entries` |
| 提示内核 Oops 或无法安全检查 socket | 停止开启操作，先恢复主机或处理权限；不要反复重试、删除故障标记或强卸载模块 |

### 更新本工具

先 `brutal-watch off`，确认规则清理完成，再重新运行安装入口：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/shigalin/brutal-watch/main/install.sh)
brutal-watch check
brutal-watch on
```

更新会保留已有配置和状态，不自动拉取节点新镜像。已经验证为普通 TCP 的节点无需再次传 `--configure-node`。更新节点容器本身时，也应先关闭加速，并保留 `GODEBUG=multipathtcp=0`，重新验证后再开启。

完整安装参数见 `bash install.sh --help`；内核模块的升级与重编译边界见下文。

## 已纳入的兼容性处理

| 问题 | 处理方式 |
| --- | --- |
| 宿主机 GCC 10、内核由 GCC 13 编译 | 读取内核配置，优先用匹配 GCC；没有则用官方 GCC 容器编译，不取消内核安全编译选项 |
| Clang 构建的内核 | `auto` / `native` 安装本机 clang、lld、llvm，沿用上游 `LLVM=1` 构建；`docker` 目前仅支持 GCC |
| 定制内核缺 headers | 查找当前内核对应包；取得失败则停止，不擅自换内核或重启服务器 |
| MPTCP 与 Brutal 冲突 | 使用 Go 官方 `GODEBUG=multipathtcp=0`；开启前直接读取真实监听 socket 的 `SO_PROTOCOL`，要求普通 TCP（6） |
| `tcp_multi_path=false` 仍使用 MPTCP | 不依赖布尔配置猜测；若程序显式开启 MPTCP、环境变量不起作用，拒绝开启并回滚自动调整 |
| `ss` 卡在内核诊断 | 采集改用 `/proc`，状态不调用 `ss`；外部命令超时后不无限等待无法退出的子进程 |
| 已经发生内核 Oops | 检测内核故障标志后拒绝重新开启，不因配置已改好就误认为内核已经恢复 |
| VPS 的 `/32` 地址、网关需要 `onlink` | 保留原网关及 `onlink`；新增精确路由，不覆盖已有精确路由或默认路由 |
| 更换内核 | 新安装交由 DKMS 重建；需要匹配 headers 及编译器，容器编译还要求 Docker 可用 |
| 重装、失败、异常退出 | 保留配置/状态；安装互斥锁；规则归属检查；故障记录保留重试；Compose 有备份和恢复流程 |

安装器仅修改明确授权的目标容器环境变量，保留其他 `GODEBUG` 选项，并验证使用相同镜像。高级 YAML anchor 或动态 `GODEBUG` 插值无法可靠保留时，会要求手动调整。

**复用此前手工安装的 v2 模块不会自动将其转换为 DKMS 安装**，原来的内核升级维护方式仍适用。

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

## 开发验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -p 'test_*.py' -v
bash -n install.sh scripts/module-build.sh
```

单元测试使用模拟主机操作，不安装内核模块或修改本机网络。隔离内核测试位于 `diagnostics/`，不要把普通 Docker 容器当成独立内核环境。

## 上游

- [HyNetworks/tcp-brutal](https://github.com/HyNetworks/tcp-brutal)：官方模块、规则接口和 `brutalctl`，遵循其 GPL-3.0 许可。
- [Go 官方 GODEBUG](https://go.dev/doc/godebug#go-124)：MPTCP 默认行为及关闭方法。
- [DKMS](https://github.com/dell/dkms)：使用标准 per-module 构建配置，不修改模块源码。

本项目不是上述项目的官方发行版。
