"""Host-network mutations are simulated; tests never call ip, docker or modprobe."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import io
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('watch', Path(__file__).with_name('brutal_watch.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

A, B = '8.8.8.8', '9.9.9.9'
DEFAULT = {'dst': 'default', 'gateway': '192.0.2.1', 'dev': 'eth0', 'flags': ['onlink']}
CFG = {'container': 'xboard-node', 'ports': [2053], 'rate_mbps': 100,
       'ttl_seconds': 1800, 'max_ips': 1024, 'exclude_cidrs': []}


class FakeHost:
    def __init__(self):
        self.boot = 'boot-one'
        self.kernel_rules = {}
        self.route_list = [copy.deepcopy(DEFAULT)]
        self.peer_set = {A}
        self.scan_error = False
        self.fail_route_add = False
        self.fail_rule_del = False
        self.actions = []
        self.next_id = 1
        self.loaded = True
        self.remaining_connections = 0
        self.policy = [{'priority': 0, 'src': 'all', 'table': 255},
                       {'priority': 32766, 'src': 'all', 'table': 254},
                       {'priority': 32767, 'src': 'all', 'table': 253}]

    def boot_id(self):
        return self.boot

    def module(self, load=False):
        if not self.loaded:
            raise w.WatchError('module unavailable')

    def rules(self):
        return copy.deepcopy(self.kernel_rules)

    def routes(self, family):
        return copy.deepcopy(self.route_list if family == 4 else [])

    def policies(self, family):
        return copy.deepcopy(self.policy)

    def peers(self, cfg):
        if self.scan_error:
            raise w.WatchError('ss failed')
        return set(self.peer_set)

    def timer(self, action):
        self.actions.append(('timer', action))

    def timer_status(self):
        return {'is-enabled': 'enabled', 'is-active': 'active'}

    def brutal_connections(self):
        return self.remaining_connections

    def write_rule(self, command):
        self.actions.append(('rule', command))
        parts = command.split()
        if parts[0] == 'add':
            if parts[1] in self.kernel_rules:
                raise AssertionError('test guard: existing rule was overwritten')
            self.kernel_rules[parts[1]] = {'id': self.next_id, 'rate': int(parts[2].split('=')[1]),
                                         'gain': 20, 'lock': 1, 'members': 0, 'sent': 0}
            self.next_id += 1
        elif parts[0] == 'del':
            if self.fail_rule_del:
                raise w.WatchError('proc write failed')
            del self.kernel_rules[parts[1]]
        else:
            raise AssertionError('global flush must never be called')

    def run(self, argv, optional=False):
        self.actions.append(tuple(argv))
        if argv[0] == 'systemctl':
            return ''
        if argv[:1] != ['ip'] or argv[2] != 'route':
            raise AssertionError(argv)
        action, prefix = argv[3:5]
        if action == 'add':
            if self.fail_route_add:
                raise w.WatchError('route add failed')
            if any(r.get('dst') == prefix for r in self.route_list):
                raise AssertionError('test guard: route overwrite')
            self.route_list.append({'dst': prefix, 'gateway': argv[argv.index('via') + 1] if 'via' in argv else None,
                                    'dev': argv[argv.index('dev') + 1], 'flags': ['onlink'] if 'onlink' in argv else [],
                                    'protocol': w.ROUTE_PROTOCOL, 'metric': w.ROUTE_METRIC,
                                    'metrics': [{'congestion': 'brutal'}],
                                    'prefsrc': argv[argv.index('src') + 1] if 'src' in argv else None})
        elif action == 'del':
            self.route_list = [r for r in self.route_list if not (r.get('dst') == prefix and r.get('protocol') == w.ROUTE_PROTOCOL)]
        else:
            raise AssertionError('route replace/flush must never be called')
        return ''


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = w.Store(Path(self.tmp.name) / 'state.json')
        self.host = FakeHost()
        self.cfg = copy.deepcopy(CFG)

    def manager(self, now):
        return w.Manager(self.cfg, self.store, self.host, now)

    def enable(self, now=1000):
        m = self.manager(now)
        m.on()
        return m

    def test_first_seen_sets_100_mbps_and_preserves_onlink(self):
        m = self.enable()
        self.assertEqual(self.host.kernel_rules[A + '/32']['rate'], 12_500_000)
        self.assertEqual(m.data['entries'][A]['expires_at'], 2800)
        route = next(a for a in self.host.actions if a[:4] == ('ip', '-4', 'route', 'add'))
        self.assertIn('onlink', route)
        self.assertEqual(route[-3:], ('congctl', 'lock', 'brutal'))
        self.assertEqual(self.host.route_list[0], DEFAULT)

    def test_seen_again_extends_ttl_without_reinstall(self):
        self.enable()
        self.host.actions.clear()
        m = self.manager(1600)
        m.tick()
        self.assertEqual(m.data['entries'][A]['expires_at'], 3400)
        self.assertEqual(m.data['entries'][A]['last_seen'], 1600)
        self.assertEqual(self.host.actions, [])

    def test_absent_kept_until_expiry_then_removed_route_first(self):
        self.enable()
        self.host.peer_set.clear()
        self.manager(2799).tick()
        self.assertIn(A + '/32', self.host.kernel_rules)
        self.host.actions.clear()
        m = self.manager(2800)
        m.tick()
        self.assertEqual(m.data['entries'], {})
        self.assertEqual(self.host.kernel_rules, {})
        self.assertEqual(self.host.route_list, [DEFAULT])
        self.assertEqual(self.host.actions[0][:4], ('ip', '-4', 'route', 'del'))
        self.assertEqual(self.host.actions[1], ('rule', 'del ' + A + '/32'))

    def test_failed_scan_does_not_expire_anyone_or_renew(self):
        self.enable()
        self.host.scan_error = True
        self.host.actions.clear()
        m = self.manager(9999)
        m.tick()
        self.assertEqual(self.host.actions, [])
        self.assertEqual(m.data['entries'][A]['expires_at'], 2800)
        self.assertEqual(m.data['last_scan'], 1000)
        self.assertIn('ss failed', m.data['error'])

    def test_unsafe_listener_withdraws_rules_and_disables(self):
        self.enable()
        def unsafe(cfg): raise w.UnsafeListener('MPTCP listener detected')
        self.host.peers = unsafe
        m = self.manager(1060)
        m.tick()
        self.assertFalse(m.data['enabled'])
        self.assertIn('MPTCP', m.data['blocked_reason'])
        self.assertEqual(self.host.kernel_rules, {})
        self.assertEqual(self.host.route_list, [DEFAULT])
        self.assertIn(('timer', 'disable'), self.host.actions)

    def test_reboot_restores_only_unexpired_without_renewing_absent_ip(self):
        self.enable()
        self.host.boot = 'boot-two'
        self.host.kernel_rules.clear()
        self.host.route_list = [copy.deepcopy(DEFAULT)]
        self.host.peer_set.clear()
        m = self.manager(2000)
        m.tick()
        self.assertIn(A + '/32', self.host.kernel_rules)
        self.assertEqual(m.data['entries'][A]['expires_at'], 2800)
        self.host.boot = 'boot-three'
        self.host.kernel_rules.clear()
        self.host.route_list = [copy.deepcopy(DEFAULT)]
        m = self.manager(2900)
        m.tick()
        self.assertEqual(m.data['entries'], {})
        self.assertEqual(self.host.kernel_rules, {})

    def test_off_removes_only_ours_and_reports_draining(self):
        self.enable()
        foreign = {'id': 99, 'rate': 10, 'gain': 20, 'lock': 1, 'members': 1, 'sent': 0}
        self.host.kernel_rules[B + '/32'] = foreign.copy()
        self.host.remaining_connections = 3
        m = self.manager(1200)
        m.off()
        self.assertFalse(m.data['enabled'])
        self.assertEqual(m.data['entries'], {})
        self.assertEqual(self.host.kernel_rules, {B + '/32': foreign})
        self.assertTrue(m.status()['shutdown_pending'])
        self.assertIn(('timer', 'disable'), self.host.actions)

    def test_off_cleanup_failure_retains_record_and_retries_without_adding(self):
        self.enable()
        self.host.fail_rule_del = True
        m = self.manager(1200)
        m.off()
        self.assertFalse(m.data['enabled'])
        self.assertEqual(m.data['entries'][A]['phase'], 'cleanup')
        self.assertTrue(m.data['entries'][A]['error'])
        self.assertEqual(self.host.actions[-1], ('timer', 'start'))
        self.host.fail_rule_del = False
        self.host.peer_set.add(B)
        m = self.manager(1260)
        m.tick()
        self.assertEqual(m.data['entries'], {})
        self.assertEqual(self.host.kernel_rules, {})

    def test_foreign_rule_conflict_expires_without_deleting_foreign_rule(self):
        foreign = {'id': 90, 'rate': 100, 'gain': 20, 'lock': 1, 'members': 0, 'sent': 0}
        self.host.kernel_rules[A + '/32'] = foreign.copy()
        m = self.enable()
        self.assertTrue(m.data['entries'][A]['error'])
        self.host.peer_set.clear()
        m = self.manager(2800)
        m.tick()
        self.assertEqual(m.data['entries'], {})
        self.assertEqual(self.host.kernel_rules[A + '/32'], foreign)

    def test_existing_exact_route_never_overwritten(self):
        foreign = {'dst': A + '/32', 'dev': 'eth1', 'protocol': 4}
        self.host.route_list.append(foreign.copy())
        m = self.enable()
        self.assertTrue(m.data['entries'][A]['error'])
        self.assertEqual(self.host.kernel_rules, {})
        self.assertIn(foreign, self.host.route_list)

    def test_route_failure_keeps_rule_identity_for_recovery_and_off(self):
        self.host.fail_route_add = True
        m = self.enable()
        self.assertEqual(m.data['entries'][A]['phase'], 'route_pending')
        self.assertIsNotNone(m.data['entries'][A]['rule_id'])
        self.host.fail_route_add = False
        m = self.manager(1060)
        m.tick()
        self.assertEqual(m.data['entries'][A]['phase'], 'active')
        m.off()
        self.assertEqual(self.host.kernel_rules, {})

    def test_unconfirmed_rule_after_crash_is_not_adopted(self):
        m = self.enable()
        m.data['entries'][A].update(rule_id=None, phase='rule_pending')
        m.save()
        self.host.actions.clear()
        m = self.manager(1060)
        m.tick()
        self.assertTrue(m.data['entries'][A]['error'])
        self.assertEqual(self.host.actions, [])

    def test_foreign_modification_is_not_deleted(self):
        self.enable()
        self.host.kernel_rules[A + '/32']['rate'] = 1
        m = self.manager(1100)
        m.off()
        self.assertEqual(self.host.kernel_rules[A + '/32']['rate'], 1)
        self.assertIn(A, m.data['entries'])
        self.assertTrue(m.data['entries'][A]['error'])

    def test_record_cap_and_atomic_single_state_file(self):
        self.cfg['max_ips'] = 1
        self.host.peer_set.add(B)
        m = self.enable()
        self.assertEqual(len(m.data['entries']), 1)
        self.assertTrue(m.data['error'])
        for tick in range(10):
            self.manager(1060 + tick).tick()
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ['state.json'])
        self.assertEqual(len(json.loads(self.store.path.read_text())['entries']), 1)

    def test_changed_gateway_rebuilds_only_owned_route(self):
        self.enable()
        self.host.route_list[0]['gateway'] = '192.0.2.2'
        m = self.manager(1060)
        m.tick()
        self.assertEqual(m.data['entries'][A]['plan']['gateway'], '192.0.2.2')
        self.assertEqual(len(self.host.kernel_rules), 1)

    def test_missing_rule_removes_selection_route_before_reinstall(self):
        self.enable()
        self.host.kernel_rules.clear()
        self.host.actions.clear()
        self.manager(1060).tick()
        self.assertEqual(self.host.actions[0][:4], ('ip', '-4', 'route', 'del'))
        self.assertEqual(len(self.host.kernel_rules), 1)

    def test_empty_rule_list_does_not_claim_existing_sockets_recovered(self):
        m = self.manager(1000)
        self.host.remaining_connections = 7
        status = m.status()
        self.assertEqual(status['entries'], {})
        self.assertTrue(status['shutdown_pending'])

    def test_policy_routing_is_rejected_without_mutation(self):
        self.host.policy.append({'priority': 100, 'src': 'all', 'table': 100})
        m = self.enable()
        self.assertTrue(m.data['entries'][A]['error'])
        self.assertEqual(self.host.kernel_rules, {})
        self.assertEqual(self.host.route_list, [DEFAULT])

    def test_corrupt_state_is_never_silently_replaced(self):
        self.store.path.write_text('{broken')
        with self.assertRaises(w.WatchError):
            self.manager(1000)
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_incident_block_prevents_reenable_before_any_host_mutation(self):
        m = self.manager(1000)
        m.data['blocked_reason'] = 'kernel MPTCP fault requires investigation'
        m.save()
        with self.assertRaises(w.WatchError):
            m.on()
        self.assertFalse(m.data['enabled'])
        self.assertEqual(self.host.actions, [])

    def test_failed_removals_do_not_starve_later_expired_records(self):
        self.host.peer_set = {'8.8.8.{}'.format(i) for i in range(1, w.MAX_ACTIONS + 2)}
        self.enable()
        last = '8.8.8.{}'.format(w.MAX_ACTIONS + 1)
        self.host.peer_set.clear()
        self.host.fail_rule_del = True
        m = self.manager(2800)
        m.tick()
        self.assertIn(last, m.data['entries'])
        m = self.manager(2860)
        m.tick()
        self.assertNotIn(last, m.data['entries'])
        self.assertEqual(len(m.data['entries']), w.MAX_ACTIONS)


class ParsingTests(unittest.TestCase):
    def test_kernel_oops_requires_recovery_before_socket_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sys/kernel').mkdir(parents=True)
            (root / 'sys/kernel/tainted').write_text(str(4096 | 128))
            with patch.object(w, 'PROC', root), patch.object(w.ctypes, 'CDLL', side_effect=AssertionError('must reject first')):
                with self.assertRaises(w.UnsafeListener):
                    w.Host().validate_plain_tcp(765, [2053])

    def test_actual_listener_protocol_guard(self):
        import socket
        class Lib:
            def syscall(self, number, *args): return 1000 if number == 434 else 1001
        class Conn:
            family = socket.AF_INET
            def __init__(self, protocol): self.protocol = protocol
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def getsockname(self): return ('0.0.0.0', 2053)
            def getsockopt(self, level, option):
                return 1 if option == socket.SO_ACCEPTCONN else self.protocol
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '765/fd').mkdir(parents=True)
            (root / '765/fd/12').symlink_to('socket:[123]')
            for protocol in (6, 262):
                with patch.object(w, 'PROC', root), patch.object(w.ctypes, 'CDLL', return_value=Lib()), \
                     patch.object(w.socket, 'socket', return_value=Conn(protocol)), patch.object(w.os, 'close'):
                    if protocol == 6:
                        w.Host().validate_plain_tcp(765, [2053])
                    else:
                        with self.assertRaises(w.UnsafeListener):
                            w.Host().validate_plain_tcp(765, [2053])
    def test_proc_collector_uses_process_socket_ownership_without_ss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '765'
            (root / 'fd').mkdir(parents=True)
            (root / 'net').mkdir()
            (root / 'fd/1').symlink_to('socket:[101]')
            (root / 'fd/2').symlink_to('socket:[102]')
            rows = ['header',
                    '0: 00000000:0805 00000000:0000 0A 0:0 0:0 0 0 0 101',
                    '1: 00000000:0805 04030201:1234 01 0:0 0:0 0 0 0 102',
                    '2: 00000000:0805 08080808:1234 01 0:0 0:0 0 0 0 999']
            (root / 'net/tcp').write_text('\n'.join(rows))
            host = w.Host()
            host.validate_plain_tcp = lambda pid, ports: None
            def run(argv):
                self.assertEqual(argv[:2], ['docker', 'inspect'])
                return '765 true host'
            host.run = run
            with patch.object(w, 'PROC', Path(directory)):
                self.assertEqual(host.peers(CFG), {'1.2.3.4'})

    def test_uninterruptible_child_timeout_never_calls_unbounded_wait(self):
        class UnkillableProcess:
            pid = 99
            returncode = None
            stdout = io.StringIO()
            stderr = io.StringIO()
            def __enter__(self): return self
            def __exit__(self, *args): self.wait()
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired('ss', 8)
            def kill(self): pass
            def wait(self, timeout=None):
                if timeout is None:
                    raise AssertionError('unbounded wait on an uninterruptible child')
                raise subprocess.TimeoutExpired('ss', timeout)
        with patch.object(w.subprocess, 'Popen', return_value=UnkillableProcess()):
            with self.assertRaises(w.WatchError):
                w.Host().run(['ss', '-Htin'])

    def test_status_never_invokes_sock_diag_and_reports_unknown(self):
        host = w.Host()
        host.run = lambda *args, **kwargs: self.fail('status must not invoke ss / SOCK_DIAG')
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / 'rules'
            rules.write_text('')
            with patch.object(w, 'RULES', rules):
                self.assertIsNone(host.brutal_connections())

    def test_load_uses_upstream_kernel_module_name(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / 'rules'
            host = w.Host()
            calls = []
            def run(argv):
                calls.append(argv)
                rules.write_text('')
                return ''
            host.run = run
            with patch.object(w, 'RULES', rules):
                host.module(load=True)
            self.assertEqual(calls, [['modprobe', 'brutal']])

    def test_iproute_5_9_numeric_strings_are_normal_default_policy(self):
        rows = [{'priority': 0, 'src': 'all', 'table': '255'},
                {'priority': 32766, 'src': 'all', 'table': '254'},
                {'priority': 32767, 'src': 'all', 'table': '253'}]
        self.assertTrue(w.simple_policy(rows))
        self.assertFalse(w.simple_policy(rows + [{'priority': 100, 'src': 'all', 'table': '100'}]))
        plan = w.plan_route(A, [{'dst': '0.0.0.0/1', 'dev': 'eth0', 'scope': '253', 'protocol': '2'}])
        self.assertEqual(plan['scope'], 'link')

    def test_pid_port_established_filter_and_ip_dedup(self):
        text = '\n'.join([
            'ESTAB 0 0 [::ffff:198.51.100.1]:2053 [::ffff:8.8.8.8]:123 users:(("xboard-node",pid=765,fd=12))',
            'ESTAB 0 0 198.51.100.1:2053 8.8.8.8:456 users:(("xboard-node",pid=765,fd=13))',
            'ESTAB 0 0 198.51.100.1:22 9.9.9.9:123 users:(("sshd",pid=592,fd=3))',
            'SYN-RECV 0 0 198.51.100.1:2053 9.9.9.9:456 users:(("xboard-node",pid=765,fd=14))',
            'ESTAB 0 0 198.51.100.1:2053 1.1.1.1:123 users:(("other",pid=1765,fd=3))',
            'ESTAB 0 0 198.51.100.1:2053 127.0.0.1:123 users:(("xboard-node",pid=765,fd=15))'])
        self.assertEqual(w.parse_peers(text, 765, [2053], []), {A})
        self.assertEqual(w.parse_peers(text, 765, [2053], [A + '/32']), set())

    def test_ipv6_host_prefix_and_route(self):
        address = '2606:4700:4700::1111'
        plan = w.plan_route(address, [{'dst': 'default', 'gateway': 'fe80::1', 'dev': 'eth0', 'pref': 'medium', 'expires': 60}])
        self.assertEqual(plan['dst'], address + '/128')
        self.assertEqual(w.route_command('add', plan)[:2], ['ip', '-6'])

    def test_special_route_metrics_rejected_not_silently_lost(self):
        route = dict(DEFAULT, metrics=[{'mtu': 1400}])
        with self.assertRaises(w.WatchError):
            w.plan_route(A, [route])

    def test_rule_parser_requires_v2_fields(self):
        rules = w.parse_rules('dst=8.8.8.8/32 rate=12500000 gain=20 lock=1 id=4 members=3 sent=10\n')
        self.assertEqual(rules[A + '/32']['members'], 3)
        with self.assertRaises(w.WatchError):
            w.parse_rules('dst=8.8.8.8/32 rate=12500000')


if __name__ == '__main__':
    unittest.main()
