# TCP Brutal IP 自动续期工具

管理程序独立部署在 Linux 宿主机，不修改节点代码、镜像或系统默认拥塞算法。不连接面板、不保存 SSH 密码。安装器可安装官方模块；只有传入 `--configure-node` 才允许调整 Compose 并重建目标节点。

默认配置：容器 `xboard-node`，TCP 端口 `2053`，每个公网 IP 共享 **100 Mbps**，每分钟从 `/proc/PID/fd` 与 `/proc/PID/net/tcp{,6}` 采集，最后一次采集到该 IP 后保留 **1800 秒**。同一 IP 更新原记录，不追加历史。

**MPTCP 兼容性要求：** `6.7.9-bbrplus` 上的 MPTCP 监听器配合原版 IP 规则曾触发 `mptcp_stream_accept` 内核异常。目标服务器现已采用 [Go 官方配置](https://go.dev/doc/godebug#go-124) `GODEBUG=multipathtcp=0`，重建后实测 2053 监听 socket 为普通 TCP（协议号 6），继续使用官方原版模块，没有修改模块源码。不要移除这个环境变量或显式开启 `tcp_multi_path`；其他机器也需要先验证普通 TCP 接入。仅有模块编译、加载成功不代表接入兼容。

脚本已停止使用 `ss` 采集或显示状态，并修复外部命令超时后无限等待的问题。事故状态中的 `blocked_reason` 会阻止 `on`；必须在兼容性条件实际修复并验证后才能清除，不能直接删标记绕过问题。

## 使用前必须了解

- **只对规则添加之后新建的 TCP 连接生效。** 首次采集发现的连接已经建立；即使它持续下载并续期，也不会因此自动切换算法。
- **`off` 和到期清理不强断连接。** 已使用 Brutal 的连接继续使用它直到关闭；关闭后可能显示存量连接等待退出。
- IP 规则不限账号、进程或端口。同一公网 IP 的其他 TCP 服务、同 NAT 用户也可能共享速率。100 Mbps 是发送方向的目标值，不是吞吐保证、严格计费限额或每账号限速。
- TCP 表中的 `ESTABLISHED` 表示连接存在，不证明代理认证通过，也不表示正在传输数据。空闲长连接会持续续期；一分钟采样可能漏掉短连接。
- 不要与该节点原来的 `multiplex.brutal` / 客户端 `smux.brutal-opts` 同时启用：默认锁定规则可能使旧接口收到 `EPERM`、协商失败。socket 协议检查不等于已关闭应用层 Brutal 选项，必须事先确认。
- 首版仅支持 **Docker host 网络、容器主进程直接持有全部配置端口、普通 main 表单路径路由**。bridge、策略路由、多路径、带特殊 metrics 的路由会被拒绝，不会改造部署来适配。

## 文件

| 文件 | 用途 |
| --- | --- |
| `brutal_watch.py` | 主程序，Python 3.7+，仅标准库 |
| `config.example.json` | 默认配置 |
| `brutal-watch.service` / `.timer` | 单次执行及每分钟调度 |
| `install.sh` | 一键安装、依赖/模块处理，可选节点配置和开启 |
| `scripts/setup.py` | 配置保留、Compose 编辑/回滚和监听验证 |
| `scripts/module-build.sh` | 标准 DKMS 构建入口，自动匹配 GCC |
| `test_brutal_watch.py` | 模拟主机操作的回归测试，不操作网络 |

## 安装和命令

安装命令和平台要求见 [README](README.md)。默认安装会自动补齐缺少的依赖、验证普通 TCP 监听、安装或复用官方 v2 模块；不自动改变节点配置或开启加速。`--configure-node` 和 `--enable` 分别明确授权这两项操作。

DKMS 包名是 `tcp-brutal`，实际内核模块名是 `brutal`。新安装的源码及 DKMS 构建配置保存在 `/usr/src/tcp-brutal-2.0.0-bw377d2a0`、`/etc/dkms/tcp-brutal-2.0.0-bw377d2a0.conf`，构建入口为 `/usr/local/libexec/brutal-watch/module-build`。复用既有手工模块时不会自动转换其安装方式。

```text
brutal-watch check   # 只读检查模块接口、实际 TCP 协议、容器、端口、策略路由及状态文件
brutal-watch on      # 必要时加载已安装的模块，立即采集，启用每分钟任务和开机自启
brutal-watch off     # 取消开机自启、停止新增、删除自己的规则与路由
brutal-watch status  # 输出 JSON：开关、自启、IP/到期时间、规则/路由校验、错误和存量连接
```

`tick` 是 systemd 的内部入口。时间戳采用 Unix 秒；`remaining_seconds` 直接表示剩余有效期。模块加载时 `host_brutal_connections` 返回 `null`（未知），不再使用可能阻塞的 socket 诊断接口；`shutdown_pending` 保守提示存量连接未确认退出，不能因规则为空声称全部恢复。

路由状态以 `brutal-watch status` 的 `route_verified` 为准。官方 `brutalctl list` 的 `ROUTE` 列只识别自身协议号 233 的路由，因此会把本工具创建的 234 路由显示为 `no`；这不代表本工具路由缺失。它的 `MEMBERS` 和 `SENT(MB)` 仍可用于观察实际使用。

如 `off` 的清理失败，记录不会丢弃；本次开机期间保留定时重试，但不再添加新 IP，也不启用下次开机自启。清理完成后停止定时器。如果 systemd 控制命令失败，返回非零并显示错误，不能视为成功关闭。

内核中的规则不跨重启保留；状态文件会保留。原来为开启状态时，定时器启动后先加载模块，等待节点可被完整采集，再恢复未过期记录；已过期记录不恢复。重启本身不延长到期时间，再次采集到的 IP 才续期。恢复依赖系统时间正确。

## 状态、清理和容量

- `/var/lib/brutal-watch/state.json`：当前记录，默认最多 1024 条，可在配置中调整 `max_ips`（上限 10000）。达到上限时保留已有记录并报告，暂不添加新 IP。
- 文件每轮覆盖更新，使用一个固定临时文件、`fsync` 和原子替换。正常情况下没有历史 IP 文件；异常退出至多留下一个临时文件，下次写入复用。
- `/run/brutal-watch.lock`：所有变更操作共享一把进程锁，防止 `off` 与定时任务互相覆盖。
- 采集失败或节点未就绪时，不续期、不按“空列表”删除；保持记录并报告错误。
- 清理失败记录保留并重试，占用同一个容量上限。状态文件损坏时拒绝重建，不会因为丢失归属而全局清理。
- 每轮最多处理 32 个清理项和 32 个安装/核对项，大批量积压会分轮完成，不能承诺任意规模下均在一分钟内全部生效。每个 IP 的 last_seen 仍按每轮成功采样更新。
- 每分钟仅输出条目数、有效规则数及错误数等摘要，交给 systemd journal；不每分钟打印完整 IP 表。journal 的磁盘轮转和总量限制遵循宿主机已有设置，不更改全局日志配置。

## 路由及异常恢复

本工具直接使用 v2 的 `/proc/net/tcp_brutal/rules` 接口，按 IP 写规则；路由单独管理。没有调用 `brutalctl add` 的自动路由功能，也从不调用全局 `flush`。

路由仅新增 `/32` 或 `/128` 主机路由，使用协议号 **234**、metric **42700**，与 `brutalctl` 的 233 区分。保留原有网关、网卡、`onlink` 和首选源地址；不覆盖默认路由。发现同 IP 已有精确路由、重叠 Brutal 规则或无法无损处理的路由属性时，跳过该 IP 并报告。

状态文件记录规则 ID、参数、路由创建意图及属性。添加顺序是“持久化意图 → 写规则并保存其 ID → 新增路由”；删除顺序是“删除自己的路由 → 删除自己的规则 → 删除状态”。`ip route add` 而非 `replace` 防止覆盖竞争创建的路由。

外部改动导致 ID、速率、路由归属不匹配时，拒绝接管或删除。极小的“内核已添加规则，但尚未持久化 ID”崩溃窗口会留下需人工核查的记录；不能凭参数恰好相同就声称规则属于自己。不要让其他程序同时管理相同 IP 的 Brutal 规则，也不要直接删除状态文件。

修改 `rate_mbps` 前先 `off` 并确认规则清理完成，再修改并 `on`。既有 TCP 连接仍可能保留旧速率直到关闭，这不是实时切换所有连接的接口。

更新工具前先 `off`；安装器在 timer/service 仍活动、运行锁被占用或状态仍有待清理条目时拒绝覆盖程序。迁移到此独立项目不改变 `/etc/brutal-watch`、`/var/lib/brutal-watch` 或服务名称，现有配置/状态可继续使用。

安装器不会自动清除未知故障标记。原 MPTCP 标记只有在模块接口可用、所有目标监听 socket 实际确认为普通 TCP 后才会解除；内核异常仍需先人工恢复主机，不能靠删除标记处理。

## 本地验证

```bash
.venv/bin/python -B -m unittest discover -p 'test_*.py' -v
bash -n install.sh scripts/module-build.sh
```

测试覆盖续期、到期、失联采集、跨重启恢复、容量限制、路由冲突、`onlink`、关闭重试、部分失败和存量连接状态。测试替换主机操作，不需要 root，不加载内核模块，不建立真实加速连接。通过测试不等于生产内核兼容或吞吐已验证。

## 上游依据

- [TCP Brutal v2 README（核对版本 377d2a0）](https://github.com/HyNetworks/tcp-brutal/blob/377d2a0e9324ef585ff90ea91779baf276cf6a50/README.md)
- [规则接口与删除生命周期](https://github.com/HyNetworks/tcp-brutal/blob/377d2a0e9324ef585ff90ea91779baf276cf6a50/brutal_rules.c)
- [brutalctl 的路由管理实现](https://github.com/HyNetworks/tcp-brutal/blob/377d2a0e9324ef585ff90ea91779baf276cf6a50/tools/brutalctl.c)
- [iproute2 路由输出实现](https://github.com/iproute2/iproute2/blob/main/ip/iproute.c)
