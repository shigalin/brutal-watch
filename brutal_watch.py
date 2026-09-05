#!/usr/bin/env python3
"""Standalone, opt-in TCP Brutal v2 IP rule manager. Python standard library only."""

import argparse
import contextlib
import ctypes
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import socket
import sys
import time


CONFIG = Path('/etc/brutal-watch/config.json')
STATE = Path('/var/lib/brutal-watch/state.json')
LOCK = Path('/run/brutal-watch.lock')
RULES = Path('/proc/net/tcp_brutal/rules')
PROC = Path('/proc')
TIMER = 'brutal-watch.timer'
# Deliberately different from brutalctl's 233. Ownership also requires saved state.
ROUTE_PROTOCOL = 234
ROUTE_METRIC = 42700
GAIN = 20
MAX_ACTIONS = 32


class WatchError(Exception):
    pass


class UnsafeListener(WatchError):
    pass


def short_error(error):
    return str(error).replace('\n', ' ')[:300]


def config_load(path):
    return validate_config(json.loads(path.read_text()))


def validate_config(raw):
    expected = {'container', 'ports', 'rate_mbps', 'ttl_seconds', 'max_ips', 'exclude_cidrs'}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise WatchError('配置字段不完整或含未知字段，请参照 config.example.json')
    if not isinstance(raw['container'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', raw['container']):
        raise WatchError('container 名称无效')
    if not isinstance(raw['ports'], list) or not raw['ports'] or any(type(p) is not int or not 1 <= p <= 65535 for p in raw['ports']):
        raise WatchError('ports 必须为有效 TCP 端口列表')
    for name, low, high in [('rate_mbps', 1, 100000), ('ttl_seconds', 60, 86400), ('max_ips', 1, 10000)]:
        if type(raw[name]) is not int or not low <= raw[name] <= high:
            raise WatchError('{} 超出允许范围 {}..{}'.format(name, low, high))
    if not isinstance(raw['exclude_cidrs'], list):
        raise WatchError('exclude_cidrs 必须是列表')
    for value in raw['exclude_cidrs']:
        ipaddress.ip_network(value)
    return raw


def canonical_ip(value):
    addr = ipaddress.ip_address(value.strip('[]'))
    return str(getattr(addr, 'ipv4_mapped', None) or addr)


def prefix_for(address):
    addr = ipaddress.ip_address(address)
    return '{}/{}'.format(addr, addr.max_prefixlen)


def route_network(route, family):
    dst = route.get('dst', 'default')
    return ipaddress.ip_network(('0.0.0.0/0' if family == 4 else '::/0') if dst == 'default' else dst, strict=False)


def parse_rules(text):
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            values = dict(part.split('=', 1) for part in line.split())
            prefix = str(ipaddress.ip_network(values['dst'], strict=True))
            result[prefix] = {key: int(values[key]) for key in ('rate', 'gain', 'lock', 'id', 'members', 'sent')}
        except (ValueError, KeyError) as exc:
            raise WatchError('无法识别 TCP Brutal v2 规则格式') from exc
    return result


def parse_peers(text, pid, ports, excludes):
    peers = set()
    owner = re.compile(r'\bpid={},'.format(pid))
    networks = [ipaddress.ip_network(x) for x in excludes]
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != 'ESTAB' or not owner.search(line):
            continue
        try:
            if int(fields[3].rsplit(':', 1)[1]) not in ports:
                continue
            addr = ipaddress.ip_address(canonical_ip(fields[4].rsplit(':', 1)[0]))
        except (IndexError, ValueError) as exc:
            raise WatchError('ss 连接格式无法识别，停止本轮清理') from exc
        if addr.is_global and not any(addr.version == net.version and addr in net for net in networks):
            peers.add(str(addr))
    return peers


def proc_peers(tables, inodes, ports, excludes):
    peers, listeners = set(), set()
    networks = [ipaddress.ip_network(value) for value in excludes]
    for text in tables:
        for line in text.splitlines()[1:]:
            fields = line.split()
            try:
                if fields[9] not in inodes:
                    continue
                port = int(fields[1].rsplit(':', 1)[1], 16)
                if fields[3] == '0A':
                    listeners.add(port)
                if fields[3] != '01' or port not in ports:
                    continue
                raw = fields[2].split(':')[0]
                packed = b''.join(int(raw[i:i + 8], 16).to_bytes(4, sys.byteorder)
                                  for i in range(0, len(raw), 8))
                addr = ipaddress.ip_address(canonical_ip(str(ipaddress.ip_address(packed))))
                if addr.is_global and not any(addr.version == net.version and addr in net for net in networks):
                    peers.add(str(addr))
            except (IndexError, ValueError) as exc:
                raise WatchError('/proc TCP 表格式异常，停止本轮清理') from exc
    if not set(ports).issubset(listeners):
        raise WatchError('配置端口未全部由容器主进程监听，拒绝采集')
    return peers


def simple_policy(rules):
    expected = {0: ('local', '255'), 32766: ('main', '254'), 32767: ('default', '253')}
    if not rules:
        return False
    for rule in rules:
        priority = rule.get('priority')
        if priority not in expected or str(rule.get('table')) not in expected[priority]:
            return False
        if rule.get('src', 'all') != 'all' or set(rule) - {'priority', 'src', 'table', 'protocol'}:
            return False
    return any(rule.get('priority') == 32766 for rule in rules)


def plan_route(address, routes):
    """Support ordinary single-next-hop main-table routing; reject lossy copies."""
    addr = ipaddress.ip_address(address)
    candidates = [r for r in routes if addr in route_network(r, addr.version)]
    if not candidates:
        raise WatchError('没有通往客户端的主路由')
    length = max(route_network(r, addr.version).prefixlen for r in candidates)
    candidates = [r for r in candidates if route_network(r, addr.version).prefixlen == length]
    if length == addr.max_prefixlen:
        raise WatchError('已有该 IP 的精确路由，拒绝覆盖')
    metric = min(int(r.get('metric', 0)) for r in candidates)
    candidates = [r for r in candidates if int(r.get('metric', 0)) == metric]
    if len(candidates) != 1:
        raise WatchError('存在等价多条路由，需人工处理')
    base = candidates[0]
    allowed = {'dst', 'gateway', 'dev', 'protocol', 'scope', 'prefsrc', 'metric', 'flags', 'pref', 'expires', 'table', 'type'}
    if set(base) - allowed or base.get('type', 'unicast') != 'unicast' or not base.get('dev'):
        raise WatchError('路由包含特殊属性，拒绝简化复制')
    if set(base.get('flags', [])) - {'onlink'}:
        raise WatchError('路由状态不支持')
    scope = str(base.get('scope', 'global'))
    scope = {'0': 'global', '253': 'link'}.get(scope, scope)
    if scope not in ('global', 'link'):
        raise WatchError('不支持的路由 scope')
    return {'dst': prefix_for(address), 'dev': base['dev'], 'gateway': base.get('gateway'),
            'onlink': 'onlink' in base.get('flags', []), 'prefsrc': base.get('prefsrc'),
            'scope': scope}


def route_matches(route, plan):
    metrics = route.get('metrics', [])
    if isinstance(metrics, dict):
        metrics = [metrics]
    cc = next((m.get('congestion') for m in metrics if 'congestion' in m), None)
    family = ipaddress.ip_network(plan['dst']).version
    return (str(route_network(route, family)) == plan['dst']
            and str(route.get('protocol')) == str(ROUTE_PROTOCOL)
            and route.get('metric') == ROUTE_METRIC and cc == 'brutal'
            and route.get('dev') == plan['dev'] and route.get('gateway') == plan['gateway']
            and route.get('prefsrc') == plan['prefsrc']
            and ('onlink' in route.get('flags', [])) == plan['onlink'])


def route_command(action, plan):
    family = ipaddress.ip_network(plan['dst']).version
    argv = ['ip', '-{}'.format(family), 'route', action, plan['dst'], 'table', 'main',
            'proto', str(ROUTE_PROTOCOL), 'metric', str(ROUTE_METRIC)]
    if plan['gateway']:
        argv += ['via', plan['gateway']]
    argv += ['dev', plan['dev']]
    if plan['onlink']:
        argv += ['onlink']
    if plan['prefsrc']:
        argv += ['src', plan['prefsrc']]
    if action == 'add':
        argv += ['scope', plan['scope'], 'congctl', 'lock', 'brutal']
    return argv


class Store:
    def __init__(self, path):
        self.path = path

    def load(self, boot):
        if not self.path.exists():
            return {'version': 1, 'boot_id': boot, 'enabled': False, 'entries': {}, 'last_scan': None, 'error': ''}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or not {'version', 'boot_id', 'enabled', 'entries', 'last_scan', 'error'} <= data.keys():
                raise ValueError('missing state fields')
            if data['version'] != 1 or type(data['enabled']) is not bool or not isinstance(data['entries'], dict):
                raise ValueError('state schema')
            if not isinstance(data['boot_id'], str) or not isinstance(data['error'], str):
                raise ValueError('state metadata')
            if len(data['entries']) > 10000:
                raise ValueError('state exceeds hard cap')
            for address, entry in data['entries'].items():
                if canonical_ip(address) != address:
                    raise ValueError('noncanonical IP')
                for key in ('last_seen', 'expires_at', 'rate'):
                    if type(entry[key]) not in (int, float) or not math.isfinite(entry[key]) or entry[key] < 0:
                        raise ValueError('bad timestamp or rate')
                if entry['expires_at'] < entry['last_seen'] or type(entry['rate']) is not int or entry['rate'] <= 0:
                    raise ValueError('invalid deadline or rate')
                if entry['phase'] not in ('new', 'rule_pending', 'route_pending', 'active', 'cleanup') or not isinstance(entry['error'], str):
                    raise ValueError('invalid phase or error')
                if entry.get('rule_id') is not None and (type(entry['rule_id']) is not int or entry['rule_id'] < 1):
                    raise ValueError('invalid rule ID')
                if type(entry.get('route_intent', False)) is not bool:
                    raise ValueError('invalid route intent')
                if entry.get('plan'):
                    plan = entry['plan']
                    if set(plan) != {'dst', 'dev', 'gateway', 'onlink', 'prefsrc', 'scope'} or plan['dst'] != prefix_for(address):
                        raise ValueError('invalid route plan')
                    if not isinstance(plan['dev'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', plan['dev']):
                        raise ValueError('invalid device')
                    if plan['scope'] not in ('global', 'link') or type(plan['onlink']) is not bool:
                        raise ValueError('invalid route attributes')
                    for key in ('gateway', 'prefsrc'):
                        if plan[key] is not None:
                            ipaddress.ip_address(plan[key])
            if data['boot_id'] != boot:
                # Kernel state is gone on reboot. Do not renew timestamps or adopt new rules.
                data['boot_id'] = boot
                for entry in data['entries'].values():
                    entry.update(rule_id=None, plan=None, phase='new', route_intent=False, error='')
            return data
        except (ValueError, KeyError, TypeError) as exc:
            raise WatchError('状态文件损坏，拒绝重建或清空；请保留文件人工核查') from exc

    def save(self, data):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # One fixed temporary file under the shared lock: crashes cannot accumulate files.
        name = str(self.path.parent / '.state.tmp')
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class Host:
    def run(self, argv, optional=False, timeout=8, cwd=None):
        # subprocess.run waits without a bound after kill on timeout. A D-state
        # kernel-blocked child may never exit; do not hold our state lock forever.
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, cwd=cwd)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise WatchError('{} 超时；已请求结束子进程 PID {}，未无限等待内核释放'.format(argv[0], process.pid)) from exc
        if process.returncode and not optional:
            raise WatchError('{}: {}'.format(argv[0], short_error(stderr or stdout)))
        return stdout.strip()

    def boot_id(self):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def rules(self):
        return parse_rules(RULES.read_text()) if RULES.exists() else {}

    def module(self, load=False):
        if not RULES.exists() and load:
            self.run(['modprobe', 'brutal'])
        if not RULES.exists():
            raise WatchError('未加载 TCP Brutal v2；请先单独安装模块，本工具不会下载安装')
        self.rules()

    def write_rule(self, command):
        # proc handler expects one complete command per write. Never invokes global flush.
        with RULES.open('w') as stream:
            stream.write(command + '\n')

    def routes(self, family):
        return json.loads(self.run(['ip', '-j', '-N', '-{}'.format(family), 'route', 'show', 'table', 'main']))

    def policies(self, family):
        return json.loads(self.run(['ip', '-j', '-N', '-{}'.format(family), 'rule', 'show']))

    def peers(self, cfg):
        fmt = '{{.State.Pid}} {{.State.Running}} {{.HostConfig.NetworkMode}}'
        info = self.run(['docker', 'inspect', '--format', fmt, cfg['container']]).split()
        if len(info) != 3 or info[1] != 'true' or info[2] != 'host' or not info[0].isdigit() or int(info[0]) < 1:
            raise WatchError('目标容器未运行或不是 host 网络；本轮不作离线清理')
        pid = int(info[0])
        self.validate_plain_tcp(pid, cfg['ports'])
        root = PROC / str(pid)
        inodes = set()
        for fd in (root / 'fd').iterdir():
            try:
                target = os.readlink(str(fd))
            except FileNotFoundError:
                continue  # Socket closed during enumeration.
            match = re.fullmatch(r'socket:\[(\d+)\]', target)
            if match:
                inodes.add(match.group(1))
        tables = [(root / 'net/tcp').read_text()]
        if (root / 'net/tcp6').exists():
            tables.append((root / 'net/tcp6').read_text())
        peers = proc_peers(tables, inodes, cfg['ports'], cfg['exclude_cidrs'])
        # A restart during collection must not become an empty snapshot.
        if self.run(['docker', 'inspect', '--format', fmt, cfg['container']]).split() != info:
            raise WatchError('采集时容器发生变化，跳过本轮')
        return peers

    def validate_plain_tcp(self, pid, ports):
        """Read SO_PROTOCOL from the real listener, without SOCK_DIAG or config guesses."""
        tainted = PROC / 'sys/kernel/tainted'
        if tainted.exists() and int(tainted.read_text().strip()) & (1 << 7):
            raise UnsafeListener('当前内核发生过 Oops；需要先维护恢复主机，不能仅靠修改配置重新开启')
        lib = ctypes.CDLL(None, use_errno=True)
        pidfd = lib.syscall(434, pid, 0)  # pidfd_open: x86_64 / aarch64 Linux >= 5.10
        if pidfd < 0:
            if ctypes.get_errno() == 3:
                raise WatchError('节点在检查时退出，跳过本轮')
            raise UnsafeListener('无法打开节点 pidfd，不能安全确认 TCP 协议')
        found = set()
        try:
            for entry in (PROC / str(pid) / 'fd').iterdir():
                try:
                    if not os.readlink(str(entry)).startswith('socket:'):
                        continue
                except FileNotFoundError:
                    continue
                fd = lib.syscall(438, pidfd, int(entry.name), 0)  # pidfd_getfd
                if fd < 0:
                    error = ctypes.get_errno()
                    if error in (1, 13):
                        raise UnsafeListener('缺少检查节点 socket 的权限（pidfd_getfd）；拒绝猜测协议')
                    continue
                try:
                    with socket.socket(fileno=fd) as conn:
                        if conn.family not in (socket.AF_INET, socket.AF_INET6):
                            continue
                        if not conn.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                            continue
                        port = conn.getsockname()[1]
                        if port in ports:
                            protocol = conn.getsockopt(socket.SOL_SOCKET, 38)  # SO_PROTOCOL
                            if protocol != socket.IPPROTO_TCP:
                                raise UnsafeListener('端口 {} 的协议为 {}，必须先设置 GODEBUG=multipathtcp=0 并重建节点'.format(port, protocol))
                            found.add(port)
                except OSError as exc:
                    raise UnsafeListener('无法读取节点监听 socket 的协议') from exc
            if found != set(ports):
                raise WatchError('目标监听端口尚未全部就绪')
        finally:
            os.close(pidfd)

    def timer(self, action):
        if action in ('start', 'stop'):
            self.run(['systemctl', '--no-block', action, TIMER])
        else:
            self.run(['systemctl', action, TIMER])

    def timer_status(self):
        return {key: self.run(['systemctl', key, TIMER], optional=True)
                for key in ('is-enabled', 'is-active')}

    def brutal_connections(self):
        # /proc TCP does not expose congestion algorithms. Never use SOCK_DIAG
        # here: one damaged socket can block all ss calls in the namespace.
        return None if RULES.exists() else 0


class Manager:
    def __init__(self, cfg, store, host, now=None):
        self.cfg, self.store, self.host = cfg, store, host
        self.now = time.time() if now is None else now
        self.data = store.load(host.boot_id())

    def save(self):
        self.store.save(self.data)

    def exact_routes(self, address):
        addr = ipaddress.ip_address(address)
        return [r for r in self.host.routes(addr.version) if str(route_network(r, addr.version)) == prefix_for(address)]

    def own_rule(self, entry, rule):
        return (entry.get('rule_id') is not None and entry['rule_id'] == rule['id']
                and rule['rate'] == entry['rate'] and rule['gain'] == GAIN and rule['lock'] == 1)

    def remove(self, address, entry):
        if entry['phase'] == 'new' and entry.get('rule_id') is None and entry.get('plan') is None:
            # A rejected foreign configuration was only observed, never owned.
            del self.data['entries'][address]
            self.save()
            return
        # Remove selection route first so new sockets do not run brutal without a group.
        entry['phase'] = 'cleanup'
        self.save()
        routes = self.exact_routes(address)
        if entry.get('plan') and entry.get('route_intent'):
            owned = [r for r in routes if route_matches(r, entry['plan'])]
            tagged = [r for r in routes if str(r.get('protocol')) == str(ROUTE_PROTOCOL)]
            if tagged and len(owned) != len(tagged):
                raise WatchError('本工具路由属性被外部修改，拒绝删除')
            if owned:
                self.host.run(route_command('del', entry['plan']))
            if any(route_matches(r, entry['plan']) for r in self.exact_routes(address)):
                raise WatchError('路由删除未生效')
        rule = self.host.rules().get(prefix_for(address))
        if rule:
            if not self.own_rule(entry, rule):
                raise WatchError('规则身份或参数不匹配，保留记录供人工核查')
            self.host.write_rule('del ' + prefix_for(address))
            if prefix_for(address) in self.host.rules():
                raise WatchError('规则删除未生效')
        del self.data['entries'][address]
        self.save()

    def install(self, address, entry):
        family = ipaddress.ip_address(address).version
        if not simple_policy(self.host.policies(family)):
            raise WatchError('存在策略路由，拒绝自动添加')
        prefix = prefix_for(address)
        rules = self.host.rules()
        for other, rule in rules.items():
            network = ipaddress.ip_network(other)
            if network.version == family and network.overlaps(ipaddress.ip_network(prefix)):
                if other != prefix or not self.own_rule(entry, rule):
                    raise WatchError('与已有 Brutal 规则冲突，拒绝接管')
        routes = self.exact_routes(address)
        if routes:
            if not entry.get('plan') or not entry.get('route_intent') or len(routes) != 1 or not route_matches(routes[0], entry['plan']):
                raise WatchError('已有精确路由或路由被修改，拒绝覆盖')
            if prefix not in rules:
                # Missing group while a selection route remains: remove our route first.
                self.host.run(route_command('del', entry['plan']))
                routes = []
        base_routes = [r for r in self.host.routes(family) if str(route_network(r, family)) != prefix]
        plan = plan_route(address, base_routes)
        if routes and plan != entry['plan']:
            self.host.run(route_command('del', entry['plan']))
            routes = []
        if not routes:
            entry['plan'] = plan
        if prefix not in rules:
            entry.update(phase='rule_pending', rule_id=None)
            self.save()  # Write-ahead intent. Unknown rule IDs after a crash are not adopted.
            self.host.write_rule('add {} rate={} gain={} lock'.format(prefix, entry['rate'], GAIN))
            rule = self.host.rules().get(prefix)
            if not rule or (rule['rate'], rule['gain'], rule['lock']) != (entry['rate'], GAIN, 1):
                raise WatchError('规则写入后校验失败')
            entry['rule_id'] = rule['id']
            entry['phase'] = 'route_pending'
            self.save()
        if not routes:
            entry['phase'] = 'route_pending'
            entry['route_intent'] = True
            self.save()
            # add, never replace. A concurrent foreign route makes this fail safely.
            self.host.run(route_command('add', entry['plan']))
        routes = self.exact_routes(address)
        if len(routes) != 1 or not route_matches(routes[0], entry['plan']):
            raise WatchError('路由写入后校验失败')
        entry.update(phase='active', error='')
        self.save()

    def cleanup(self, addresses):
        # Failed removals must not starve other expired records or fill the cap forever.
        pending = sorted(addresses, key=lambda a: self.data['entries'][a].get('cleanup_at', 0))
        for address in pending[:MAX_ACTIONS]:
            try:
                self.remove(address, self.data['entries'][address])
            except (WatchError, OSError, subprocess.SubprocessError) as exc:
                self.data['entries'][address]['error'] = short_error(exc)
                self.data['entries'][address]['cleanup_at'] = self.now
                self.save()

    def tick(self):
        if not self.data['enabled']:
            self.cleanup(list(self.data['entries']))
            if not self.data['entries']:
                self.host.timer('stop')
            self.save()
            return
        self.host.module(load=True)
        try:
            peers = self.host.peers(self.cfg)
        except UnsafeListener as exc:
            self.data['blocked_reason'] = short_error(exc)
            self.off()  # Withdraw our routes/rules, never leave active selection on an unsafe listener.
            self.data['error'] = short_error(exc)
            self.save()
            return
        except (WatchError, OSError, subprocess.SubprocessError) as exc:
            self.data['error'] = short_error(exc)
            self.save()
            return  # Failed observation never means everyone went offline.
        self.data.update(last_scan=self.now, error='')
        entries = self.data['entries']
        for address in peers & entries.keys():
            entries[address].update(last_seen=self.now, expires_at=self.now + self.cfg['ttl_seconds'])
        self.save()
        expired = [address for address, e in entries.items() if e['expires_at'] <= self.now]
        self.cleanup(expired)
        for address in sorted(peers - entries.keys(), key=lambda a: (ipaddress.ip_address(a).version, int(ipaddress.ip_address(a)))):
            if len(entries) >= self.cfg['max_ips']:
                self.data['error'] = '达到最大 IP 条目数；保留已有记录，暂不添加新 IP'
                break
            entries[address] = {'last_seen': self.now, 'expires_at': self.now + self.cfg['ttl_seconds'],
                                'rate': self.cfg['rate_mbps'] * 1000000 // 8, 'rule_id': None,
                                'plan': None, 'phase': 'new', 'route_intent': False, 'error': ''}
        self.save()
        # Rotate work by oldest successful reconciliation so large sets cannot starve.
        live = sorted((a for a, e in entries.items() if e['expires_at'] > self.now),
                      key=lambda a: entries[a].get('checked_at', 0))
        for address in live[:MAX_ACTIONS]:
            entry = entries[address]
            try:
                if entry['phase'] == 'cleanup':
                    self.remove(address, entry)
                    continue
                if entry['rate'] != self.cfg['rate_mbps'] * 1000000 // 8:
                    raise WatchError('速率配置已改变，请先 off 清理，再 on 应用新配置')
                self.install(address, entry)
            except (WatchError, OSError, subprocess.SubprocessError) as exc:
                entry['error'] = short_error(exc)
            entry['checked_at'] = self.now
            self.save()

    def on(self):
        if self.data.get('blocked_reason'):
            raise WatchError('禁止重新开启：' + self.data['blocked_reason'])
        self.host.peers(self.cfg)  # Refuse enabling against the wrong process/network mode.
        self.host.module(load=True)
        self.host.run(['systemctl', 'cat', TIMER])
        self.data['enabled'] = True
        self.save()
        self.host.timer('enable')
        self.host.timer('start')
        self.tick()

    def off(self):
        self.data['enabled'] = False
        self.save()  # Any waiting tick must see disabled before it can add anything.
        errors = []
        for action in ('disable', 'stop'):
            try:
                self.host.timer(action)
            except (WatchError, OSError, subprocess.SubprocessError) as exc:
                errors.append(short_error(exc))
        self.cleanup(list(self.data['entries']))
        if self.data['entries']:
            # No additions; keep retrying removals this boot, without enabling at boot.
            try:
                self.host.timer('start')
            except (WatchError, OSError, subprocess.SubprocessError) as exc:
                errors.append(short_error(exc))
        self.data['error'] = '; '.join(errors)
        self.save()

    def status(self):
        result = {'enabled': self.data['enabled'], 'timer': self.host.timer_status(),
                  'module_loaded': RULES.exists(), 'last_scan': self.data['last_scan'],
                  'error': self.data['error'], 'blocked_reason': self.data.get('blocked_reason', ''), 'entries': {}}
        rules = self.host.rules()
        families = {ipaddress.ip_address(a).version for a in self.data['entries']}
        all_routes = {family: self.host.routes(family) for family in families}
        for address, entry in self.data['entries'].items():
            rule = rules.get(prefix_for(address))
            family = ipaddress.ip_address(address).version
            routes = [r for r in all_routes[family] if str(route_network(r, family)) == prefix_for(address)]
            result['entries'][address] = {
                'remaining_seconds': max(0, int(entry['expires_at'] - self.now)),
                'last_seen': entry['last_seen'], 'expires_at': entry['expires_at'],
                'rate_mbps': entry['rate'] * 8 / 1000000, 'phase': entry['phase'],
                'rule_verified': bool(rule and self.own_rule(entry, rule)),
                'route_verified': bool(entry.get('plan') and len(routes) == 1 and route_matches(routes[0], entry['plan'])),
                'members': rule['members'] if rule and self.own_rule(entry, rule) else None,
                'error': entry['error']}
        result['host_brutal_connections'] = self.host.brutal_connections()
        result['connection_inspection'] = ('unknown: 不调用 ss 诊断；规则为空也不能证明存量连接已退出'
                                           if result['host_brutal_connections'] is None else 'module not loaded')
        result['shutdown_pending'] = not self.data['enabled'] and bool(self.data['entries'] or result['host_brutal_connections'] is None or result['host_brutal_connections'])
        return result


@contextlib.contextmanager
def exclusive_lock(path):
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description='独立 TCP Brutal v2 IP 规则管理；不修改节点部署')
    parser.add_argument('command', choices=('check', 'on', 'off', 'status', 'tick'))
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.exit(1, '请在 Linux 宿主机上以 root 执行。\n')
    try:
        host = Host()
        if args.command == 'check':
            cfg = config_load(CONFIG)
            data = Store(STATE).load(host.boot_id())
            if data.get('blocked_reason'):
                raise WatchError('兼容性阻止开启：' + data['blocked_reason'])
            # Strictly read-only, including no lock/state file creation and no modprobe.
            host.module()
            peers = host.peers(cfg)
            for family in {ipaddress.ip_address(a).version for a in peers}:
                if not simple_policy(host.policies(family)):
                    raise WatchError('检测到策略路由，不支持自动配置')
            data = Store(STATE).load(host.boot_id())
            print(json.dumps({'module': 'TCP Brutal v2 rules interface available', 'peer_count': len(peers),
                              'state_entries': len(data['entries']), 'timer': host.timer_status(),
                              'note': '只读检查通过；安装规则前还会逐 IP 检查冲突。'}, ensure_ascii=False))
            return 0
        with exclusive_lock(LOCK):
            manager = Manager({}, Store(STATE), host)
            # A broken/deleted config must not prevent off or pending cleanup.
            if args.command == 'on' or (args.command == 'tick' and manager.data['enabled']):
                manager.cfg = config_load(CONFIG)
            if args.command != 'status':
                getattr(manager, args.command)()
            status = manager.status()
            if args.command == 'tick':
                # Full IP tables belong in explicit status output, not minute-by-minute logs.
                log = {key: status[key] for key in ('enabled', 'last_scan', 'error', 'shutdown_pending')}
                log['ip_count'] = len(status['entries'])
                log['verified_rules'] = sum(e['rule_verified'] and e['route_verified'] for e in status['entries'].values())
                log['error_count'] = sum(bool(e['error']) for e in status['entries'].values())
                print(json.dumps(log, ensure_ascii=False))
            else:
                print(json.dumps(status, ensure_ascii=False, indent=2))
            return int(bool(status['error'] or any(e['error'] for e in status['entries'].values())))
    except (WatchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print('brutal-watch: ' + short_error(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('操作已取消；请通过 brutal-watch status 查看已完成的步骤。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
