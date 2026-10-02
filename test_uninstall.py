import importlib.util
import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('uninstall', ROOT / 'scripts/uninstall.py')
uninstall = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uninstall)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('SOURCE', 'DKMS_CONFIG', 'UNITS', 'CONFIG', 'STATE', 'LIBEXEC'):
            target = self.root / name
            target.mkdir()
            self.patch_attr(name, target)
        for name in ('COMMAND', 'CTL', 'LOADED'):
            self.patch_attr(name, self.root / name)
        self.host = self.enter(patch.object(uninstall.watch, 'Host')).return_value
        self.host.boot_id.return_value = 'test-boot'
        self.host.routes.return_value = []
        self.host.rules.return_value = {}
        self.enter(patch.object(uninstall.shutil, 'which', return_value='/mock/command'))
        self.records = set()
        self.actions = []
        self.disk_remaining = False
        self.enter(patch.object(uninstall.subprocess, 'check_output', side_effect=self.output))
        self.enter(patch.object(uninstall.subprocess, 'run', side_effect=self.run_command))
        self.enter(patch.object(uninstall.time, 'sleep'))
        self.enter(patch.object(uninstall, 'node_ready', return_value=123))
        uninstall.COMMAND.write_text('watcher')
        (uninstall.CONFIG / 'config.json').write_text('original configuration')
        for unit in ('brutal-watch.timer', 'brutal-watch.service'):
            (uninstall.UNITS / unit).write_text('unit')
        (uninstall.LIBEXEC / 'module-build').write_text('build helper')

    def enter(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def patch_attr(self, name, value):
        self.enter(patch.object(uninstall, name, value))

    def output(self, argv, **kwargs):
        self.actions.append(argv)
        if argv == ['dkms', 'status']:
            return ''.join('tcp-brutal/{}, test-kernel, x86_64: installed\n'.format(v) for v in self.records)
        if argv[:2] == ['docker', 'inspect']:
            return 'a' * 64 if argv[3] == '{{.Id}}' else 'true\n'
        self.fail('Unexpected check_output: ' + repr(argv))

    def run_command(self, argv, **kwargs):
        self.actions.append(argv)
        if argv[:2] == ['dkms', 'remove']:
            self.records.remove(argv[5])
        if argv[:2] == ['rmmod', 'brutal'] and uninstall.LOADED.exists():
            shutil.rmtree(uninstall.LOADED)
        missing = argv[:2] == ['modinfo', '-n'] and not self.disk_remaining
        return subprocess.CompletedProcess(argv, int(missing), stdout='inactive\n',
                                           stderr='modinfo: ERROR: Module brutal not found.\n' if missing else '')

    def load(self, version='2.0.1'):
        uninstall.LOADED.mkdir(exist_ok=True)
        (uninstall.LOADED / 'version').write_text(version + '\n')

    def module(self, version='2.0.1-bwd2397ff', commit='d2397ff8bca04a29fd2de01cf7d2d4b825224de8'):
        source = uninstall.SOURCE / ('tcp-brutal-' + version)
        source.mkdir()
        (source / '.brutal-watch-upstream').write_text(commit)
        (source / 'tools').mkdir()
        (source / 'tools/brutalctl').write_text('original binary')
        uninstall.CTL.write_text('original binary')
        override = uninstall.DKMS_CONFIG / ('tcp-brutal-' + version + '.conf')
        override.write_text(uninstall.MANAGED_HEADER + '\nMAKE[0]="' + str(uninstall.LIBEXEC / 'module-build') + '"\n')
        self.records.add(version)
        return source, override

    def test_removes_current_and_old_owned_versions_and_is_repeatable(self):
        current = self.module()
        old = self.module('2.0.0-bw644db52', '644db52' + '0' * 33)
        self.load()
        self.assertEqual(uninstall.remove(False, unload_loaded=True), 0)
        self.assertEqual(self.records, set())
        for path in (*current, *old, uninstall.CONFIG, uninstall.STATE, uninstall.COMMAND,
                     uninstall.CTL, uninstall.LIBEXEC, uninstall.LOADED):
            self.assertFalse(path.exists(), path)
        self.assertLess(self.actions.index(['rmmod', 'brutal']),
                        next(i for i, a in enumerate(self.actions) if a[:2] == ['dkms', 'remove']))
        self.assertEqual(uninstall.remove(False), 0)

    def test_keep_module_does_not_touch_dkms_or_loaded_module(self):
        source, override = self.module()
        self.load()
        self.assertEqual(uninstall.remove(True), 0)
        for path in (source, override, uninstall.CTL, uninstall.LIBEXEC, uninstall.LOADED):
            self.assertTrue(path.exists(), path)
        self.assertFalse(uninstall.COMMAND.exists())
        self.assertFalse(any(a[0] in ('dkms', 'modinfo', 'rmmod', 'docker') for a in self.actions))

    def test_bad_ownership_stops_before_deletion(self):
        source, override = self.module()
        (source / '.brutal-watch-upstream').write_text('not our commit')
        with self.assertRaisesRegex(ValueError, '归属'):
            uninstall.remove(False)
        self.assertTrue(uninstall.COMMAND.exists())
        self.assertTrue(override.exists())
        self.assertFalse(any(a[:2] == ['dkms', 'remove'] for a in self.actions))

    def test_dkms_failure_preserves_tool_config_source_and_build_helper(self):
        source, override = self.module()
        original = self.run_command
        def fail(argv, **kwargs):
            if argv[:2] == ['dkms', 'remove']:
                raise subprocess.CalledProcessError(1, argv)
            return original(argv, **kwargs)
        with patch.object(uninstall.subprocess, 'run', side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                uninstall.remove(False)
        for path in (source, override, uninstall.COMMAND, uninstall.CONFIG, uninstall.LIBEXEC):
            self.assertTrue(path.exists(), path)

    def test_leftover_routes_or_invalid_state_prevent_deletion(self):
        self.host.routes.return_value = [{'protocol': 234}]
        with self.assertRaisesRegex(ValueError, '234'):
            uninstall.remove(True)
        self.assertTrue(uninstall.COMMAND.exists())
        self.host.routes.return_value = []
        (uninstall.STATE / 'state.json').write_text('broken')
        with self.assertRaises(uninstall.watch.WatchError):
            uninstall.remove(True)
        self.assertTrue(uninstall.COMMAND.exists())

    def test_foreign_module_and_modified_ctl_are_preserved_and_reported(self):
        self.module()
        self.records.add('2.0.1')
        uninstall.CTL.write_text('another installation')
        self.disk_remaining = True
        self.assertEqual(uninstall.remove(False), 2)
        self.assertEqual(self.records, {'2.0.1'})
        self.assertEqual(uninstall.CTL.read_text(), 'another installation')
        self.assertTrue((uninstall.LIBEXEC / 'module-build').exists())
        self.assertFalse(uninstall.COMMAND.exists())

    def test_loaded_module_requires_opt_in_even_with_matching_version(self):
        self.module()
        for version in ('2.0.0', '2.0.1', None):
            with self.subTest(version=version):
                self.actions.clear()
                self.load(version or '')
                if version is None:
                    (uninstall.LOADED / 'version').unlink()
                self.assertEqual(uninstall.remove(False), 2)
                self.assertTrue(uninstall.LOADED.exists())
                self.assertTrue((uninstall.CONFIG / 'config.json').exists())
                self.assertTrue(uninstall.COMMAND.exists())
                self.assertFalse(any(a[0] in ('rmmod', 'docker') or a[:2] == ['dkms', 'remove']
                                     for a in self.actions))
                self.assertEqual(self.records, {'2.0.1-bwd2397ff'})

    def test_explicit_unload_can_remove_loaded_module_without_own_dkms(self):
        self.load()
        self.assertEqual(uninstall.remove(False, unload_loaded=True), 0)
        self.assertFalse(uninstall.LOADED.exists())
        self.assertIn(['rmmod', 'brutal'], self.actions)

    def test_modinfo_error_keeps_ctl_and_build_helper(self):
        source, override = self.module()
        original = self.run_command
        def broken(argv, **kwargs):
            if argv[:2] == ['modinfo', '-n']:
                return subprocess.CompletedProcess(argv, 1, stderr='modinfo: ERROR: could not open modules.dep\n')
            return original(argv, **kwargs)
        with patch.object(uninstall.subprocess, 'run', side_effect=broken):
            with self.assertRaisesRegex(ValueError, '无法核对'):
                uninstall.remove(False)
        for path in (uninstall.CTL, uninstall.LIBEXEC / 'module-build', uninstall.COMMAND, source, override):
            self.assertTrue(path.exists(), path)
        self.assertEqual(self.records, set())
        self.assertEqual(uninstall.remove(False), 0)
        for path in (uninstall.CTL, uninstall.LIBEXEC, uninstall.COMMAND, source, override):
            self.assertFalse(path.exists(), path)

    def test_restore_failure_retains_ownership_after_dkms_removal_for_retry(self):
        source, override = self.module()
        self.load()
        attempts = []
        original = self.run_command
        def fail_restore(argv, **kwargs):
            if argv == ['rmmod', 'brutal']:
                attempts.append(argv)
                if len(attempts) == 1:
                    return subprocess.CompletedProcess(argv, 1)
            if argv[:2] == ['docker', 'start']:
                raise subprocess.CalledProcessError(1, argv)
            return original(argv, **kwargs)
        with patch.object(uninstall.watch, 'config_load', return_value={'container': 'my-node', 'ports': [443]}), \
             patch.object(uninstall.subprocess, 'run', side_effect=fail_restore):
            with self.assertRaisesRegex(ValueError, '手动启动'):
                uninstall.remove(False, unload_loaded=True)
        self.assertEqual(self.records, set())
        for path in (source, override, uninstall.CTL, uninstall.CONFIG, uninstall.COMMAND):
            self.assertTrue(path.exists(), path)
        receipt = uninstall.STATE / 'uninstall-node.json'
        self.assertEqual(json.loads(receipt.read_text()),
                         {'container': 'my-node', 'id': 'a' * 64, 'ports': [443]})
        self.assertFalse(uninstall.LOADED.exists())
        self.actions.clear()
        self.assertEqual(uninstall.remove(False, unload_loaded=True), 0)
        self.assertIn(['docker', 'start', 'a' * 64], self.actions)
        self.assertFalse(receipt.exists())
        self.assertFalse(source.exists())
        self.assertFalse(uninstall.CTL.exists())

    def test_foreign_rules_prevent_module_removal(self):
        self.module()
        self.host.rules.return_value = {'foreign': {}}
        with self.assertRaisesRegex(ValueError, '其他 TCP Brutal'):
            uninstall.remove(False)
        self.assertTrue(uninstall.COMMAND.exists())

    def test_symlink_source_is_never_deleted(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (uninstall.SOURCE / 'tcp-brutal-2.0.1-bwd2397ff').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '链接'):
            uninstall.remove(False)
        self.assertTrue(outside.exists())
        self.assertTrue(uninstall.COMMAND.exists())

    def test_busy_module_interrupts_only_target_and_restarts_even_on_failure(self):
        for failure in ('none', 'unload', 'body', 'restart'):
            with self.subTest(failure=failure):
                self.actions.clear()
                self.load()
                attempts = []
                def busy(argv, **kwargs):
                    if argv == ['rmmod', 'brutal']:
                        self.actions.append(argv)
                        attempts.append(argv)
                        # Orphaned connections keep the module busy for a while after docker stop.
                        if len(attempts) <= 3 or failure == 'unload':
                            return subprocess.CompletedProcess(argv, 1)
                        shutil.rmtree(uninstall.LOADED)
                        return subprocess.CompletedProcess(argv, 0)
                    if argv[:2] == ['docker', 'start'] and failure == 'restart':
                        self.actions.append(argv)
                        raise subprocess.CalledProcessError(1, argv)
                    return self.run_command(argv, **kwargs)
                clock = iter(range(0, 1000, 10))
                with patch.object(uninstall.watch, 'config_load', return_value={'container': 'my-node', 'ports': [443]}), \
                     patch.object(uninstall.subprocess, 'run', side_effect=busy), \
                     patch.object(uninstall.time, 'monotonic', side_effect=lambda: next(clock)):
                    if failure == 'unload':
                        with self.assertRaisesRegex(ValueError, '仍无法卸载'):
                            with uninstall.unload_module():
                                self.fail('module never unloaded')
                    elif failure == 'body':
                        with self.assertRaises(subprocess.CalledProcessError):
                            with uninstall.unload_module():
                                raise subprocess.CalledProcessError(1, ['dkms', 'remove'])
                    elif failure == 'restart':
                        # The original failure stays visible next to the manual-restart instruction.
                        with self.assertRaisesRegex(ValueError, "dkms.*手动启动.*my-node"):
                            with uninstall.unload_module():
                                raise subprocess.CalledProcessError(1, ['dkms', 'remove'])
                    else:
                        with uninstall.unload_module():
                            self.assertNotIn(['docker', 'start', 'a' * 64], self.actions)
                            self.assertFalse(uninstall.LOADED.exists())
                self.assertIn(['docker', 'stop', '--time', '30', 'a' * 64], self.actions)
                self.assertIn(['docker', 'start', 'a' * 64], self.actions)
                self.assertNotIn(['rmmod', '-f', 'brutal'], self.actions)

    def test_stopped_node_waits_for_unload_and_stays_stopped(self):
        for failure in ('none', 'unload', 'body'):
            with self.subTest(failure=failure):
                self.actions.clear()
                self.load()
                attempts = []
                def busy(argv, **kwargs):
                    self.actions.append(argv)
                    self.assertEqual(argv, ['rmmod', 'brutal'])
                    attempts.append(argv)
                    if len(attempts) <= 2 or failure == 'unload':
                        return subprocess.CompletedProcess(argv, 1, stderr='Module brutal is in use')
                    shutil.rmtree(uninstall.LOADED)
                    return subprocess.CompletedProcess(argv, 0)
                clock = iter(range(0, 1000, 10))
                with patch.object(uninstall.watch, 'config_load', return_value={'container': 'my-node', 'ports': [443]}), \
                     patch.object(uninstall.subprocess, 'check_output', return_value='false\n'), \
                     patch.object(uninstall.subprocess, 'run', side_effect=busy), \
                     patch.object(uninstall.time, 'monotonic', side_effect=lambda: next(clock)), \
                     patch.object(uninstall.time, 'sleep') as sleep:
                    if failure == 'unload':
                        with self.assertRaisesRegex(ValueError, 'Module brutal is in use') as error:
                            with uninstall.unload_module():
                                self.fail('module never unloaded')
                        self.assertNotIn('已恢复节点', str(error.exception))
                        self.assertTrue(uninstall.LOADED.exists())
                    elif failure == 'body':
                        with self.assertRaises(subprocess.CalledProcessError):
                            with uninstall.unload_module():
                                raise subprocess.CalledProcessError(1, ['dkms', 'remove'])
                    else:
                        with uninstall.unload_module():
                            self.assertFalse(uninstall.LOADED.exists())
                    self.assertTrue(sleep.called)
                self.assertGreater(len(attempts), 1)
                self.assertFalse(any(a[0] == 'docker' for a in self.actions))

    def test_unload_failure_preserves_rmmod_error(self):
        self.load()
        clock = iter(range(0, 1000, 10))
        with patch.object(uninstall.watch, 'config_load', return_value={'container': 'my-node', 'ports': [443]}), \
             patch.object(uninstall.subprocess, 'check_output', return_value='false\n'), \
             patch.object(uninstall.subprocess, 'run', return_value=subprocess.CompletedProcess(
                 ['rmmod', 'brutal'], 1, stderr='rmmod: ERROR: Operation not permitted')) as run, \
             patch.object(uninstall.time, 'monotonic', side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(ValueError, 'Operation not permitted'):
                with uninstall.unload_module():
                    self.fail('module never unloaded')
            self.assertEqual(run.call_args.kwargs['stderr'], subprocess.PIPE)

    def test_interruption_after_stop_restores_node_and_preserves_installation(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                self.load()
                self.actions.clear()
                def interrupt_stop(argv, **kwargs):
                    if argv == ['rmmod', 'brutal']:
                        return subprocess.CompletedProcess(argv, 1, stderr='busy')
                    if argv[:2] == ['docker', 'stop']:
                        # Recovery target must be durable before the stop operation begins.
                        self.assertTrue((uninstall.STATE / 'uninstall-node.json').exists())
                        signal.raise_signal(sig)
                    return self.run_command(argv, **kwargs)
                with patch.object(uninstall.watch, 'config_load', return_value={'container': 'my-node', 'ports': [443]}), \
                     patch.object(uninstall.subprocess, 'run', side_effect=interrupt_stop), \
                     patch.object(uninstall.sys, 'argv', ['uninstall.py', 'remove', '--unload-module']):
                    self.assertEqual(uninstall.main(), 130)
                self.assertIn(['docker', 'start', 'a' * 64], self.actions)
                self.assertTrue(uninstall.COMMAND.exists())
                self.assertTrue(uninstall.LOADED.exists())
                self.assertFalse((uninstall.STATE / 'uninstall-node.json').exists())


class NodeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = {'container': 'my-node', 'id': 'a' * 64, 'ports': [443]}
        self.receipt = self.root / 'uninstall-node.json'
        self.receipt.write_text(json.dumps(self.record))
        patcher = patch.object(uninstall, 'STATE', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retry_restores_recorded_container_even_without_module(self):
        with patch.object(uninstall, 'start_node') as start:
            uninstall.recover_node()
        start.assert_called_once_with(self.record)
        self.assertFalse(self.receipt.exists())

    def test_failed_recovery_keeps_receipt(self):
        with patch.object(uninstall, 'start_node', side_effect=ValueError('not listening')):
            with self.assertRaisesRegex(ValueError, 'not listening'):
                uninstall.recover_node()
        self.assertEqual(json.loads(self.receipt.read_text()), self.record)

    def test_running_container_without_ports_is_not_recovered(self):
        # The container exists and Docker accepts start, but the node never listens.
        with patch.object(uninstall.subprocess, 'run'), \
             patch.object(uninstall.subprocess, 'check_output', return_value='true\n'), \
             patch.object(uninstall, 'node_ready', create=True, side_effect=ValueError('missing port 443')), \
             patch.object(uninstall.time, 'monotonic', side_effect=range(0, 1000, 10)), \
             patch.object(uninstall.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, '443'):
                uninstall.start_node(self.record)

    def test_recovery_waits_for_stable_listeners(self):
        with patch.object(uninstall.subprocess, 'run') as run, \
             patch.object(uninstall.subprocess, 'check_output', return_value='true\n'), \
             patch.object(uninstall, 'node_ready', create=True,
                          side_effect=[ValueError('starting'), 123, 123, 124, 124, 124]) as ready, \
             patch.object(uninstall.time, 'sleep'):
            uninstall.start_node(self.record)
        self.assertEqual(ready.call_count, 6)
        self.assertEqual(run.call_args.args[0], ['docker', 'start', self.record['id']])

    def test_invalid_receipt_never_starts_container(self):
        self.receipt.write_text(json.dumps(dict(self.record, id='my-node')))
        with patch.object(uninstall.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, '恢复记录损坏'):
                uninstall.recover_node()
        run.assert_not_called()
        self.assertTrue(self.receipt.exists())

    def test_listener_must_belong_to_recorded_container_process(self):
        proc = self.root / 'proc'
        process = proc / '123'
        (process / 'fd').mkdir(parents=True)
        (process / 'net').mkdir()
        (process / 'fd/5').symlink_to('socket:[100]')
        table = process / 'net/tcp'
        header = 'sl local_address rem_address st tx_queue rx_queue tr tm_when retrnsmt uid timeout inode\n'
        with patch.object(uninstall.watch, 'PROC', proc), \
             patch.object(uninstall.subprocess, 'check_output', return_value='123 true\n'):
            for inode, state, port, ready in (('999', '0A', '01BB', False),
                                             ('100', '01', '01BB', False),
                                             ('100', '0A', '0050', False),
                                             ('100', '0A', '01BB', True)):
                with self.subTest(inode=inode, state=state, port=port):
                    table.write_text(header + '0: 00000000:{} 00000000:0000 {} 0:0 0:0 0 0 0 {}\n'.format(
                        port, state, inode))
                    if ready:
                        self.assertEqual(uninstall.node_ready(self.record), 123)
                    else:
                        with self.assertRaises(uninstall.watch.WatchError):
                            uninstall.node_ready(self.record)

    def test_second_interrupt_does_not_abort_recovery(self):
        def restoring(record):
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        with patch.object(uninstall, 'start_node', side_effect=restoring):
            uninstall.recover_node()
        self.assertFalse(self.receipt.exists())
        for sig, handler in handlers.items():
            self.assertEqual(signal.getsignal(sig), handler)


class UninstallEntryTests(unittest.TestCase):
    def test_unload_opt_in_is_forwarded_to_preflight_and_remove(self):
        script = '''source "$1/install.sh"
check_uninstall_platform() { :; }
acquire_install_lock() { :; }
project_source() { SOURCE_DIR=/mock-source; }
stop_existing_watch() { :; }
acquire_runtime_lock() { :; }
release_runtime_lock() { :; }
python3() { echo "$*"; }
main --uninstall --unload-module
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('uninstall.py preflight --unload-module', result.stdout)
        self.assertIn('uninstall.py remove --unload-module', result.stdout)

    def test_real_uninstall_sequence_and_failure_exit_codes(self):
        for failure, expected in (('', 0), ('recover-node', 1), ('preflight', 1), ('off', 1), ('remove', 2)):
            with self.subTest(failure=failure):
                script = '''source "$1/install.sh"
mock_failure=$2
check_uninstall_platform() { echo platform; }
acquire_install_lock() { echo install-lock; }
project_source() { SOURCE_DIR=/mock-source; }
stop_existing_watch() { echo off; [[ "$mock_failure" != off ]]; }
acquire_runtime_lock() { echo runtime-lock; }
release_runtime_lock() { echo unlock; }
python3() {
  echo "$*"
  if [[ "$mock_failure" == "$2" ]]; then
    [[ "$2" != remove ]] || return 2
    return 1
  fi
}
check_docker() { echo unexpected-docker; return 1; }
install_dependencies() { echo unexpected-packages; return 1; }
main --uninstall --keep-module
'''
                result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), failure], capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn('unexpected', result.stdout)
                self.assertLess(result.stdout.index('install-lock'), result.stdout.index('runtime-lock'))
                self.assertLess(result.stdout.index('runtime-lock'), result.stdout.index('uninstall.py recover-node'))
                if failure == 'recover-node':
                    self.assertNotIn('uninstall.py preflight', result.stdout)
                    self.assertNotIn('\noff', result.stdout)
                    self.assertNotIn('uninstall.py remove', result.stdout)
                elif failure in ('off', 'preflight'):
                    self.assertEqual(result.stdout.count('runtime-lock'), 1)
                    self.assertNotIn('uninstall.py remove', result.stdout)
                else:
                    self.assertLess(result.stdout.index('uninstall.py recover-node'), result.stdout.index('uninstall.py preflight'))
                    self.assertLess(result.stdout.index('install-lock'), result.stdout.index('uninstall.py preflight'))
                    self.assertLess(result.stdout.index('uninstall.py preflight'), result.stdout.index('\noff'))
                    self.assertLess(result.stdout.index('\noff'), result.stdout.rindex('runtime-lock'))
                    self.assertLess(result.stdout.rindex('runtime-lock'), result.stdout.index('uninstall.py remove --keep-module'))

    def test_uninstall_branch_bypasses_installation_and_preserves_order(self):
        script = '''source "$1/install.sh"
uninstall_main() { echo uninstall:$KEEP_MODULE; }
check_platform() { echo unexpected-install; return 1; }
main --uninstall --keep-module
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(ROOT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'uninstall:1')

    def test_rejects_mixed_install_and_uninstall_flags(self):
        for args in (['--keep-module'], ['--uninstall', '--enable'], ['--uninstall', '--skip-module'],
                     ['--uninstall', '--ports', '443'], ['--uninstall', '--compiler', 'auto'],
                     ['--unload-module'], ['--uninstall', '--keep-module', '--unload-module']):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(ROOT / 'install.sh')] + args, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('错误', result.stderr)


if __name__ == '__main__':
    unittest.main()
