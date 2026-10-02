import importlib.util
from pathlib import Path
import shutil
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
            return 'true\n'
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
        with patch.object(uninstall.watch, 'config_load', return_value={'container': 'my-node'}), \
             patch.object(uninstall.subprocess, 'run', side_effect=fail_restore):
            with self.assertRaisesRegex(ValueError, '手动启动'):
                uninstall.remove(False, unload_loaded=True)
        self.assertEqual(self.records, set())
        for path in (source, override, uninstall.CTL, uninstall.CONFIG, uninstall.COMMAND):
            self.assertTrue(path.exists(), path)
        self.assertEqual(uninstall.remove(False, unload_loaded=True), 0)
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
                with patch.object(uninstall.watch, 'config_load', return_value={'container': 'my-node'}), \
                     patch.object(uninstall.subprocess, 'run', side_effect=busy), \
                     patch.object(uninstall.time, 'monotonic', side_effect=lambda: next(clock)):
                    if failure == 'unload':
                        with self.assertRaisesRegex(ValueError, '仍被占用'):
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
                            self.assertNotIn(['docker', 'start', 'my-node'], self.actions)
                            self.assertFalse(uninstall.LOADED.exists())
                self.assertIn(['docker', 'stop', '--time', '30', 'my-node'], self.actions)
                self.assertIn(['docker', 'start', 'my-node'], self.actions)
                self.assertNotIn(['rmmod', '-f', 'brutal'], self.actions)


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
        for failure, expected in (('', 0), ('preflight', 1), ('off', 1), ('remove', 2)):
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
                if failure in ('off', 'preflight'):
                    self.assertNotIn('runtime-lock', result.stdout)
                    self.assertNotIn('uninstall.py remove', result.stdout)
                else:
                    self.assertLess(result.stdout.index('install-lock'), result.stdout.index('uninstall.py preflight'))
                    self.assertLess(result.stdout.index('uninstall.py preflight'), result.stdout.index('\noff'))
                    self.assertLess(result.stdout.index('\noff'), result.stdout.index('runtime-lock'))
                    self.assertLess(result.stdout.index('runtime-lock'), result.stdout.index('uninstall.py remove --keep-module'))

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
