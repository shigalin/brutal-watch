# 官方原版模块的隔离验证

此目录只包含测试工具，不含自定义内核补丁。宿主机不加载测试模块；Docker 中的 QEMU TCG 启动独立的 `6.7.9-bbrplus` 内核，客户机内部使用 veth 与 network namespace 模拟客户端和服务端。

## 已核对的官方依据

- [TCP Brutal 官方仓库](https://github.com/HyNetworks/tcp-brutal)，核对提交 `377d2a0e9324ef585ff90ea91779baf276cf6a50`，模块版本 `2.0.0`。
- [Go 官方 GODEBUG 文档](https://go.dev/doc/godebug#go-124)：Go 1.24 开始默认启用监听器 MPTCP；`GODEBUG=multipathtcp=0` 关闭默认 MPTCP。
- [Go ListenConfig 文档](https://pkg.go.dev/net#ListenConfig.SetMultipathTCP)：显式 `SetMultipathTCP` 优先于默认值和 GODEBUG。当前目标节点未发现显式开启配置，仍须在实际修改后核验监听协议。

## 结果

- 官方原版模块 + MPTCP 监听器接收普通 TCP：在独立客户机中复现 `mptcp_stream_accept+0xe4` 崩溃，位置与服务器事故一致。
- 同一官方原版模块 + 普通 TCP 监听器：IPv4/IPv6 合计 22 次连接、数据校验、`TCP_INFO`、`ss`、100 Mbps 到 50 Mbps 的规则修改及删除后的新连接恢复全部通过。
- 这是上述内核和接入方式的验证，不能推导为任意内核或 MPTCP 路径已经修复，也不是公网提速测试。
- 曾用于研究的自定义模块补丁已放弃，没有安装到生产宿主机。

## 运行方式

需要 Linux Docker 测试主机，以及对应内核镜像、headers 和模块文件。以下命令只构建工具镜像、运行独立虚拟机，不加载宿主机模块。

```bash
docker build -t brutal-watch-vm:377d2a0 diagnostics
bash diagnostics/run-vm.sh /path/to/official/brutal.ko original crash
bash diagnostics/run-vm.sh /path/to/official/brutal.ko official-tcp pass tcp-only
```

输出中原版 MPTCP 用例应出现 `EXPECTED_MPTCP_CRASH_REPRODUCED`；普通 TCP 用例应出现 `VM_ALL_PASS`。测试目录保存串口日志和临时 initramfs。QEMU 的独立内核限制为 192 MiB、1 vCPU，外层容器限制为 384 MiB、0.5 CPU，无外部网络。

## 已获授权的生产调整

在 `/root/xboard-node/docker-compose.yml` 的 `xboard-node` 服务现有 `environment` 下增加：

```yaml
GODEBUG: multipathtcp=0
```

用户已授权这项最小运行配置调整。容器用同一镜像重建后，通过 pidfd 复制监听 FD 并读取 `SO_PROTOCOL`，确认 2053 监听器的协议号为 6（普通 TCP），不是 262（MPTCP）。随后恢复官方原版模块与加速任务。原 Compose 已备份；没有修改节点代码、镜像、端口或业务参数。
