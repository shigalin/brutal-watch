import os
import json
import sys
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parent.resolve()


class InstallerTests(unittest.TestCase):
    def flow(self, args='', fail=''):
        # All host-mutating stages are replaced. Only mktemp/cleanup use the filesystem.
        script = '''
source "$1/install.sh"
WATCH_COMMAND=brutal-watch
check_platform() { echo platform; }
check_docker() { echo docker; }
acquire_install_lock() { echo locked; }
acquire_runtime_lock() { echo runtime-locked; }
stop_existing_watch() { brutal-watch off || die 'stop failed'; }
release_runtime_lock() { echo unlocked; }
install_dependencies() { echo dependencies; }
project_source() { SOURCE_DIR=/mock-source; }
prepare_module() { echo prepare; %s; }
install_tool() { echo tool; }
install_module() { echo module; %s; }
python3() { echo "python:$*"; if [[ "$*" == *verify-node* ]]; then %s; fi; }
systemctl() { [[ "$1" == stop ]]; }
brutal-watch() { echo "watch:$*"; if [[ "$1" == off ]]; then %s; fi; }
main %s
''' % ('return 1' if fail == 'prepare' else ':', 'return 1' if fail == 'module' else ':',
       'return 1' if fail == 'verify' else ':', 'return 1' if fail == 'stop' else ':', args)
        if fail == 'idle':
            script = script.replace('if [[ "$*" == *verify-node* ]]', 'if [[ "$*" == *idle* ]]; then return 1; fi; if [[ "$*" == *verify-node* ]]')
        if fail == 'config':
            script = script.replace('if [[ "$*" == *verify-node* ]]',
                                    'if [[ "$*" == *verify-node* || "$*" == *validate-config* ]]; then return 1; fi; if [[ "$*" == *verify-node* ]]')
        return subprocess.run(['bash', '-c', script, 'test', str(ROOT)], capture_output=True, text=True)

    def test_default_does_not_reconfigure_or_enable(self):
        result = self.flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verify-node', result.stdout)
        self.assertNotIn('configure-node', result.stdout)
        self.assertNotIn('watch:on', result.stdout)
        self.assertLess(result.stdout.index('verify-node'), result.stdout.index('prepare'))
        self.assertLess(result.stdout.index('locked'), result.stdout.index('\ndocker'))
        self.assertLess(result.stdout.index('\ndocker'), result.stdout.index('dependencies'))
        self.assertLess(result.stdout.index('prepare'), result.stdout.index('watch:off'))
        self.assertLess(result.stdout.index('watch:off'), result.stdout.index('runtime-locked'))
        self.assertLess(result.stdout.index('runtime-locked'), result.stdout.index('setup.py idle'))
        self.assertLess(result.stdout.index('setup.py idle'), result.stdout.index('\ntool'))
        self.assertLess(result.stdout.index('unlocked'), result.stdout.index('watch:check'))

    def test_failed_stop_or_remaining_entries_never_updates_or_enables(self):
        for failure in ('stop', 'idle'):
            with self.subTest(failure=failure):
                result = self.flow('--configure-node --enable', failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('watch:off', result.stdout)
                self.assertIn('prepare', result.stdout)
                self.assertNotIn('\ntool', result.stdout)
                self.assertNotIn('\nmodule', result.stdout)
                self.assertNotIn('watch:on', result.stdout)
                if failure == 'stop':
                    self.assertNotIn('runtime-locked', result.stdout)

    def test_explicit_configuration_and_enable_order(self):
        result = self.flow('--configure-node --enable --ports 443 --rate-mbps 200')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setup.py config --ports 443 --rate-mbps 200', result.stdout)
        self.assertIn('validate-config --ports 443 --rate-mbps 200', result.stdout)
        self.assertLess(result.stdout.index('validate-config'), result.stdout.index('prepare'))
        self.assertLess(result.stdout.index('prepare'), result.stdout.index('watch:off'))
        self.assertLess(result.stdout.index('runtime-locked'), result.stdout.index('setup.py config '))
        self.assertLess(result.stdout.index('prepare'), result.stdout.index('configure-node'))
        self.assertLess(result.stdout.index('configure-node'), result.stdout.index('\nmodule'))
        self.assertLess(result.stdout.index('watch:check'), result.stdout.index('watch:on'))

    def test_failed_node_validation_never_loads_module(self):
        result = self.flow('--enable', 'verify')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('prepare', result.stdout)
        self.assertNotIn('watch:off', result.stdout)
        self.assertNotIn('\ntool', result.stdout)
        self.assertNotIn('\nmodule', result.stdout)
        self.assertNotIn('watch:on', result.stdout)

    def test_failed_preparation_never_changes_node_or_installed_tool(self):
        for args in ('--enable', '--configure-node --enable'):
            with self.subTest(args=args):
                result = self.flow(args, 'prepare')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('configure-node', result.stdout)
                self.assertNotIn('watch:off', result.stdout)
                self.assertNotIn('\ntool', result.stdout)
                self.assertNotIn('\nmodule', result.stdout)

    def test_failed_config_preflight_preserves_running_watch(self):
        for args in ('--enable', '--configure-node --enable'):
            with self.subTest(args=args):
                result = self.flow(args, 'config')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('prepare', result.stdout)
                self.assertNotIn('watch:off', result.stdout)
                self.assertNotIn('runtime-locked', result.stdout)

    def test_default_node_preflight_receives_install_arguments(self):
        result = self.flow('--container custom-node --ports 443 --rate-mbps 200')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verify-node --container custom-node --ports 443 --rate-mbps 200', result.stdout)
        self.assertIn('setup.py config --container custom-node --ports 443 --rate-mbps 200', result.stdout)

    def test_current_version_or_skip_module_needs_no_build_preparation(self):
        for skip, version in ((False, '2.0.1'), (True, '2.0.0')):
            script = '''source "$1/install.sh"
SKIP_MODULE=$2
modinfo() { echo "$mock_version"; }
loaded_module_version() { :; }
install_packages() { echo unexpected-package-install; return 1; }
modprobe() { echo unexpected-module-load; return 1; }
prepare_module
'''
            script = script.replace('SKIP_MODULE=$2', 'SKIP_MODULE=$2; mock_version=$3')
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), str(int(skip)), version], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')

    def test_prepare_and_install_share_module_mode_and_propagate_failure(self):
        for operation in ('prepare_module', 'install_module'):
            script = '''source "$1/install.sh"
module_mode() { echo mode-failed >&2; return 1; }
install_packages() { echo unexpected-packages; }
modprobe() { echo unexpected-load; }
"$2"
'''
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), operation], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('mode-failed', result.stderr)
            self.assertEqual(result.stdout, '')

    def test_failed_module_install_never_enables(self):
        result = self.flow('--enable', 'module')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('watch:on', result.stdout)

    def test_help_does_not_require_root_or_linux(self):
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--configure-node', result.stdout)

    def test_archive_wait_flag_is_forwarded_only_to_archive_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            script = '''source "$1/install.sh"
parse_args --archive-wait-minutes 30
ID=debian; SOURCE_DIR=/mock-source; TMP_WORK=/mock-work
update_package_index() { :; }
apt-cache() { echo 'Candidate: (none)'; }
python3() { printf '%s\\n' "$*"; }
printf 'config:%s\\n' ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"}
ensure_headers mock-kernel "$2"
'''
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), folder], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('config:\n', result.stdout)
            self.assertIn('/mock-source/scripts/debian_headers.py mock-kernel /mock-work/headers --archive-wait-minutes 30', result.stdout)

    def test_archive_wait_flag_rejects_missing_or_invalid_values(self):
        for value in ([], ['0'], ['31'], ['-1'], ['1.5'], ['yes']):
            with self.subTest(value=value):
                result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--archive-wait-minutes'] + value,
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('--archive-wait-minutes', result.stderr)

    def test_missing_headers_refresh_index_when_other_packages_are_installed(self):
        # Exercise headers acquisition directly, independent of loaded host modules.
        script = '''
source "$1/install.sh"
dpkg-query() { echo installed; }
apt-cache() { echo 'Candidate: 1.0'; }
apt-get() { echo "apt:$*"; [[ "$1" == update ]]; }
install_packages dkms gcc make libc6-dev
ensure_headers brutal-watch-test-missing-headers
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(ROOT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('安装 brutal-watch-test-missing-headers 的 headers 失败', result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'apt:update',
            'apt:install -y --no-install-recommends linux-headers-brutal-watch-test-missing-headers',
        ])

    def test_archive_rejects_traversal_and_symlinks(self):
        import tarfile, io
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for kind in ('escape', 'symlink'):
                archive = root / (kind + '.tar.gz')
                with tarfile.open(archive, 'w:gz') as tar:
                    item = tarfile.TarInfo('repo/../escaped' if kind == 'escape' else 'repo/link')
                    if kind == 'escape':
                        item.size = 1; tar.addfile(item, io.BytesIO(b'x'))
                    else:
                        item.type = tarfile.SYMTYPE; item.linkname = '/etc'; tar.addfile(item)
                out = root / ('out-' + kind); out.mkdir()
                command = 'source "$1/install.sh"; extract_archive "$2" "$3"'
                result = subprocess.run(['bash', '-c', command, 'test', str(ROOT), str(archive), str(out)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / 'escaped').exists())

    def test_existing_headers_do_not_refresh_or_use_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'Makefile').write_text('')
            script = '''source "$1/install.sh"
apt-get() { echo unexpected-apt; return 1; }
python3() { echo unexpected-archive; return 1; }
ensure_headers mock-kernel "$2"
'''
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), folder], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')

    def test_only_debian_missing_candidate_uses_archive(self):
        for distro, candidate in [('debian', False), ('debian', True), ('ubuntu', False), ('ubuntu', True)]:
            with self.subTest(distro=distro, candidate=candidate), tempfile.TemporaryDirectory() as folder:
                script = '''source "$1/install.sh"
ID=$2; SOURCE_DIR=/mock-source; TMP_WORK=/mock-work
apt-cache() { echo "Candidate: $4"; }
apt-get() { echo "apt:$*"; }
python3() { echo "archive:$*"; }
ensure_headers mock-kernel "$3"
'''.replace('echo "Candidate: $4"', 'echo ' + shlex.quote('Candidate: 1.0' if candidate else 'Candidate: (none)'))
                result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), distro, folder], capture_output=True, text=True)
                self.assertEqual('archive:' in result.stdout, distro == 'debian' and not candidate)
                self.assertEqual('apt:install' in result.stdout, candidate)
                self.assertEqual(result.returncode == 0, candidate or distro == 'debian')


class AutoStopTests(unittest.TestCase):
    def stop(self, installed=True, active=True, off_ok=True, stop_ok=True, stays_active=False,
             pending='0', invalid=False, entry_error='', top_error=''):
        script = '''source "$1/install.sh"
active=$2; stop_ok=$3; stays_active=$4; TMP_WORK=$5
WATCH_COMMAND="$TMP_WORK/brutal-watch"
systemctl() {
  if [[ "$1" == stop ]]; then
    echo "systemctl:$*"
    [[ "$stop_ok" == 1 ]] || return 1
    active=$stays_active
  else
    [[ "$active" == 1 ]]
  fi
}
stop_existing_watch
echo stopped
'''
        with tempfile.TemporaryDirectory() as folder:
            if installed:
                # The installed path is deliberately absent from PATH.
                command = Path(folder) / 'brutal-watch'
                command.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys
settings = json.loads(pathlib.Path(__file__).with_suffix('.json').read_text())
progress = pathlib.Path(__file__).with_suffix('.count')
call = int(progress.read_text()) if progress.exists() else 0
progress.write_text(str(call + 1))
print('watch:' + ' '.join(sys.argv[1:]), file=sys.stderr)
if settings['invalid']:
    print('invalid-json')
    sys.exit(0)
counts = settings['counts']
remaining = counts[call] if call < len(counts) else 0
print(json.dumps({'enabled': False, 'error': settings['top_error'],
                  'entries': {str(i): {'error': settings['entry_error']} for i in range(remaining)}}))
sys.exit(int(not settings['off_ok'] or bool(remaining and settings['entry_error'])))
''')
                command.chmod(0o755)
                command.with_suffix('.json').write_text(json.dumps({
                    'counts': [int(n) for n in pending.split()], 'invalid': invalid,
                    'off_ok': off_ok, 'entry_error': entry_error, 'top_error': top_error,
                }))
            return subprocess.run(['bash', '-c', script, 'test', str(ROOT), str(int(active)),
                                   str(int(stop_ok)), str(int(stays_active)), folder],
                                  capture_output=True, text=True)

    def test_existing_install_stops_and_waits_for_both_units(self):
        for active in (True, False):
            result = self.stop(active=active)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('watch:off', result.stderr)
            self.assertEqual(result.stdout.splitlines()[-2:], [
                'systemctl:stop brutal-watch.timer brutal-watch.service', 'stopped'])

    def test_first_install_does_not_run_off_or_stop_units(self):
        result = self.stop(installed=False, active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'stopped\n')

    def test_cleanup_failure_preserves_retry_timer(self):
        result = self.stop(off_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('systemctl:stop', result.stdout)
        self.assertNotIn('stopped', result.stdout)

    def test_large_cleanup_continues_until_empty(self):
        result = self.stop(pending='40 8 0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count('watch:off'), 3)
        self.assertIn('仍有 40 条记录', result.stdout)
        self.assertIn('仍有 8 条记录', result.stdout)
        self.assertEqual(result.stdout.count('systemctl:stop'), 1)

    def test_large_cleanup_with_old_entry_errors_continues_until_empty(self):
        result = self.stop(pending='40 8 0', entry_error='速率配置已改变')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count('watch:off'), 3)
        self.assertIn('stopped', result.stdout)

    def test_top_level_error_stops_even_if_cleanup_is_progressing(self):
        result = self.stop(pending='40 8 0', top_error='timer stop failed')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.count('watch:off'), 1)
        self.assertNotIn('systemctl:stop', result.stdout)
        self.assertIn('timer stop failed', result.stderr)

    def test_stalled_cleanup_prints_entry_error(self):
        result = self.stop(pending='8 8', entry_error='rule ownership conflict')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('rule ownership conflict', result.stderr)
        self.assertNotIn('systemctl:stop', result.stdout)

    def test_stalled_or_invalid_cleanup_preserves_retry_timer(self):
        for args in ({'pending': '8 8'}, {'invalid': True},
                     {'pending': '8 8', 'entry_error': '规则冲突'},
                     {'pending': '8 9', 'entry_error': '规则冲突'}):
            with self.subTest(args=args):
                result = self.stop(**args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('systemctl:stop', result.stdout)
                self.assertNotIn('stopped', result.stdout)

    def test_missing_command_stop_failure_or_active_unit_blocks_install(self):
        for args in ({'installed': False}, {'stop_ok': False}, {'stays_active': True}):
            with self.subTest(args=args):
                result = self.stop(**args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('stopped', result.stdout)


class ModuleUpgradeTests(unittest.TestCase):
    def mode(self, disk='', loaded='', skip=False, unknown=False):
        script = '''source "$1/install.sh"
mock_disk=$2; mock_loaded=$3; SKIP_MODULE=$4; unknown=$5
modinfo() { if [[ -n "$mock_disk" ]]; then echo "$mock_disk"; else [[ "$unknown" == 1 && "$*" == brutal ]]; fi; }
loaded_module_version() { printf '%s' "$mock_loaded"; }
module_has_rules() { [[ -n "$mock_loaded" ]]; }
# Model the dpkg comparison; no host packages or kernel modules are touched.
dpkg() { [[ "$2" == 2.0.2 || "$2" == 2.1.0 ]]; }
module_mode
'''
        return subprocess.run(['bash', '-c', script, 'test', str(ROOT), disk, loaded,
                               str(int(skip)), str(int(unknown))], capture_output=True, text=True)

    def test_default_installs_missing_or_upgrades_old_v2(self):
        for disk, loaded in (('', ''), ('2.0.0', ''), ('2.0.0', '2.0.0'), ('', '2.0.0')):
            with self.subTest(disk=disk, loaded=loaded):
                result = self.mode(disk, loaded)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'build')

    def test_current_disk_version_does_not_rebuild(self):
        for loaded in ('', '2.0.1'):
            result = self.mode('2.0.1', loaded)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'existing')

    def test_default_pending_upgrade_is_rejected_during_preparation(self):
        result = self.mode('2.0.1', '2.0.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('手动重启', result.stderr)
        self.assertNotIn('existing', result.stdout)

    def test_unsupported_unknown_and_newer_versions_are_not_overwritten(self):
        for disk, loaded, unknown in (('1.0.2', '', False), ('custom', '', False),
                                     ('', '', True), ('2.0.2', '', False),
                                     ('2.0.1', '2.1.0', False)):
            with self.subTest(disk=disk, loaded=loaded, unknown=unknown):
                result = self.mode(disk, loaded, unknown=unknown)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('build', result.stdout)

    def test_skip_preserves_old_version_but_cannot_hide_pending_upgrade(self):
        self.assertEqual(self.mode('2.0.0', '', skip=True).stdout.strip(), 'existing')
        self.assertEqual(self.mode('2.0.0', '2.0.0', skip=True).stdout.strip(), 'loaded')
        result = self.mode('2.0.1', '2.0.0', skip=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('手动重启', result.stderr)

    def verify(self, disk='2.0.1', loaded='', after='2.0.1', rules=True, main=False):
        script = '''source "$1/install.sh"
mock_disk=$2; current=$3; after=$4; rules=$5
modinfo() { printf '%s' "$mock_disk"; }
loaded_module_version() { printf '%s' "$current"; }
module_has_rules() { [[ "$rules" == 1 ]]; }
modprobe() { echo "modprobe:$*"; current=$after; }
'''
        if main:
            script += '''
WATCH_COMMAND=brutal-watch
check_platform() { :; }
acquire_install_lock() { :; }
acquire_runtime_lock() { :; }
stop_existing_watch() { :; }
release_runtime_lock() { :; }
check_docker() { :; }
install_dependencies() { :; }
project_source() { SOURCE_DIR=/mock-source; }
python3() { :; }
prepare_module() { :; }
install_tool() { :; }
install_module() { verify_module_version; }
brutal-watch() { echo "watch:$*"; }
main --enable
'''
        else:
            script += 'verify_module_version\n'
        return subprocess.run(['bash', '-c', script, 'test', str(ROOT), disk, loaded, after,
                               str(int(rules))], capture_output=True, text=True)

    def test_pending_upgrade_never_loads_or_enables(self):
        result = self.verify(loaded='2.0.0', main=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已安装到磁盘', result.stderr)
        self.assertIn('升级尚未生效', result.stderr)
        self.assertNotIn('modprobe:', result.stdout)
        self.assertNotIn('watch:', result.stdout)

    def test_new_install_and_post_reboot_can_finish(self):
        for loaded in ('', '2.0.1'):
            result = self.verify(loaded=loaded, main=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('watch:check', result.stdout)
            self.assertIn('watch:on', result.stdout)

    def test_disk_load_and_interface_failures_never_enable(self):
        for values in ({'disk': '2.0.0'}, {'after': '2.0.0'}, {'rules': False}):
            with self.subTest(values=values):
                result = self.verify(main=True, **values)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('watch:on', result.stdout)


class ModulePreparationTests(unittest.TestCase):
    def prepare(self, case, main=False):
        import hashlib
        import io
        import tarfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            archive = path / 'upstream.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                content = b'printf "PACKAGE_VERSION=%s\\n" "$PACKAGE_VERSION"\n'
                item = tarfile.TarInfo('upstream/scripts/mkdkmsconf.sh')
                item.size = len(content)
                tar.addfile(item, io.BytesIO(content))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            commit = 'test-upstream-commit'
            source = path / 'source'
            override = path / 'dkms.conf'
            if case in ('unknown-source', 'known-source'):
                source.mkdir()
                (source / '.brutal-watch-upstream').write_text(commit if case == 'known-source' else 'unknown')
            if case == 'external-override':
                override.write_text('# externally managed\n')
            script = '''source "$1/install.sh"
MODULE_SOURCE="$2/source"; DKMS_OVERRIDE="$2/dkms.conf"
UPSTREAM_COMMIT=$3; UPSTREAM_SHA256=$4; mock_case=$5
curl() {
  echo download
  [[ "$mock_case" != download-failed ]] || return 1
  cp "$archive_fixture" "${@: -1}"
}
archive_fixture="$2/upstream.tar.gz"
if [[ "$mock_case" == bad-hash ]]; then UPSTREAM_SHA256=0000000000000000000000000000000000000000000000000000000000000000; fi
check_platform() { :; }
check_docker() { :; }
acquire_install_lock() { :; }
install_dependencies() { :; }
project_source() { SOURCE_DIR=/unused; }
modinfo() { if [[ "$mock_case" == pending ]]; then echo 2.0.1; else echo 2.0.0; fi; }
loaded_module_version() { echo 2.0.0; }
dpkg() { return 1; }
install_packages() { echo unexpected-package-install; return 1; }
stop_existing_watch() { echo unexpected-off; return 1; }
'''
            if main:
                script += 'python3() { :; }; main --enable\n'
            else:
                script += 'TMP_WORK="$2/work"; mkdir "$TMP_WORK"; prepare_module_source\n'
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), folder, commit, digest, case],
                                    capture_output=True, text=True)
            staged = path / 'work' / 'module'
            if case == 'valid':
                self.assertEqual((staged / '.brutal-watch-upstream').read_text().strip(), commit)
                self.assertIn('PACKAGE_VERSION=2.0.1-', (staged / 'dkms.conf').read_text())
                self.assertFalse(source.exists())
                self.assertFalse(override.exists())
            if case == 'unknown-source':
                self.assertEqual((source / '.brutal-watch-upstream').read_text(), 'unknown')
            if case == 'external-override':
                self.assertEqual(override.read_text(), '# externally managed\n')
            return result

    def test_pending_upgrade_fails_before_off_in_real_main_flow(self):
        result = self.prepare('pending', main=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('手动重启', result.stderr)
        self.assertNotIn('unexpected-off', result.stdout)
        self.assertNotIn('download', result.stdout)

    def test_source_preflight_failures_preserve_running_watch(self):
        for case, error in (('unknown-source', '未知内容'), ('external-override', '外部 DKMS'),
                            ('download-failed', '下载失败'), ('bad-hash', 'SHA256 校验失败')):
            with self.subTest(case=case):
                result = self.prepare(case, main=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertNotIn('unexpected-off', result.stdout)
                self.assertNotIn('unexpected-package-install', result.stdout)

    def test_valid_archive_is_staged_without_installing_source_or_override(self):
        result = self.prepare('valid')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('download'), 1)

    def test_owned_source_reuse_skips_download(self):
        result = self.prepare('known-source')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('download', result.stdout)


class DockerClientTests(unittest.TestCase):
    def check(self, client=False, server=True, active=True, install_ok=True, reachable=True,
              candidate=True, bundled=False, socket_active=False, update_ok=True, extra_dependency=False,
              cli_installed=False):
        # Mock host commands only; exercise the real client check and package installer.
        script = '''
source "$1/install.sh"
client=$2; server=$3; active=$4; install_ok=$5; reachable=$6
candidate=$7; bundled=$8; socket_active=$9; update_ok=${10}; extra_dependency=${11}
cli_installed=${12}
command() {
  if [[ "$*" == "-v docker" ]]; then [[ "$client" == 1 ]]; else builtin command "$@"; fi
}
dpkg-query() {
  if [[ "${@: -1}" == docker-cli && "$cli_installed" == 1 ]]; then echo installed; return; fi
  [[ "${@: -1}" == docker.io && "$server" == 1 ]] || return 1
  if [[ "$1" == -L ]]; then
    if [[ "$bundled" == 1 ]]; then echo /usr/bin/docker; fi
    return 0
  fi
  echo installed
}
systemctl() {
  [[ "$*" == "is-active --quiet docker" && "$active" == 1 ]] ||
    [[ "$*" == "is-active --quiet docker.socket" && "$socket_active" == 1 ]]
}
apt-cache() {
  [[ "$*" == "policy docker-cli" ]] || return 1
  if [[ "$candidate" == 1 ]]; then echo '  Candidate: 26.1.5'; else echo '  Candidate: (none)'; fi
}
apt-get() {
  echo "apt:$*"
  if [[ "$1" == update ]]; then [[ "$update_ok" == 1 ]] || return 1; fi
  if [[ "$1" == install ]]; then
    [[ "$install_ok" == 1 ]] || return 1
    client=1
  fi
}
docker() { [[ "$*" == info && "$reachable" == 1 ]]; }
check_docker
if [[ "$extra_dependency" == 1 ]]; then install_packages python3; fi
echo checked
'''
        args = [str(int(value)) for value in (client, server, active, install_ok, reachable,
                                             candidate, bundled, socket_active, update_ok, extra_dependency,
                                             cli_installed)]
        return subprocess.run(['bash', '-c', script, 'test', str(ROOT), *args], capture_output=True, text=True)

    def test_split_package_installs_only_client(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith('apt:')],
                         ['apt:update', 'apt:install -y --no-install-recommends docker-cli'])
        self.assertIn('checked', result.stdout)

    def test_existing_client_needs_no_install(self):
        result = self.check(client=True, server=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('apt:', result.stdout)

    def test_installed_but_invisible_client_stops_before_package_operations(self):
        result = self.check(cli_installed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('docker-cli 已安装', result.stderr)
        self.assertIn('PATH', result.stderr)
        self.assertNotIn('apt:', result.stdout)
        self.assertNotIn('补装', result.stdout + result.stderr)

    def test_missing_server_does_not_install(self):
        result = self.check(server=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('服务端', result.stderr)
        self.assertNotIn('apt:', result.stdout)

    def test_inactive_server_does_not_install(self):
        result = self.check(active=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Docker 未运行', result.stderr)
        self.assertNotIn('apt:', result.stdout)

    def test_failed_client_install_stops(self):
        result = self.check(install_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('docker-cli 安装失败', result.stderr)
        self.assertNotIn('checked', result.stdout)

    def test_bundled_client_is_not_treated_as_split_package(self):
        result = self.check(bundled=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('docker.io 已包含客户端', result.stderr)
        self.assertNotIn('apt:', result.stdout)

    def test_missing_candidate_has_actionable_error_without_install(self):
        result = self.check(candidate=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('没有可安装的 docker-cli', result.stderr)
        self.assertNotIn('apt:install', result.stdout)

    def test_active_socket_allows_client_install(self):
        result = self.check(active=False, socket_active=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('apt:install -y --no-install-recommends docker-cli', result.stdout)

    def test_failed_index_update_stops_before_install(self):
        result = self.check(update_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('软件源索引更新失败', result.stderr)
        self.assertNotIn('apt:install', result.stdout)

    def test_client_and_dependencies_share_one_index_update(self):
        result = self.check(extra_dependency=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines().count('apt:update'), 1)
        self.assertIn('apt:install -y --no-install-recommends python3', result.stdout)

    def test_unreachable_daemon_stops_with_or_without_existing_client(self):
        for client in (False, True):
            with self.subTest(client=client):
                result = self.check(client=client, reachable=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('无法连接 Docker', result.stderr)
                self.assertNotIn('checked', result.stdout)


class CompilerTests(unittest.TestCase):
    def clang_build(self, policy='auto', missing='', auto_conf=False):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            config = path / ('include/config/auto.conf' if auto_conf else '.config')
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text('CONFIG_CC_IS_CLANG=y\n')
            if auto_conf:
                (path / '.config').write_text('CONFIG_GCC_VERSION=130200\n')
            binary = path / 'bin'
            binary.mkdir()
            make = binary / 'make'
            make.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
            make.chmod(0o755)
            script = '''source "$1/scripts/module-build.sh"
command() { [[ "$1" == -v && "$2" != "$4" ]]; }
build_module "$3" "$2"
'''.replace('"$2" != "$4"', '"$2" != ' + shlex.quote(missing))
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'])
            return subprocess.run(['bash', '-c', script, 'test', str(ROOT), str(path), policy], env=env, capture_output=True, text=True)

    def test_clang_build_defers_to_upstream_without_compiler_override(self):
        for policy in ('auto', 'native'):
            for auto_conf in (False, True):
                with self.subTest(policy=policy, auto_conf=auto_conf):
                    result = self.clang_build(policy, auto_conf=auto_conf)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('all', result.stdout.splitlines())
                    self.assertNotIn('LLVM=1', result.stdout.splitlines())
                    self.assertFalse(any(arg.startswith('CC=') for arg in result.stdout.splitlines()))

    def test_clang_build_requires_tools(self):
        for tool in ('clang', 'ld.lld', 'llvm-objcopy'):
            result = self.clang_build(missing=tool)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(tool, result.stderr)

    def test_clang_docker_policy_is_not_silently_changed(self):
        result = self.clang_build('docker')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--compiler auto', result.stderr)

    def select(self, policy, kernel_version=130200, host_version='10.2.1'):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / '.config').write_text('CONFIG_GCC_VERSION={}\n'.format(kernel_version))
            # Shadow command lookup and gcc functions, without touching the system toolchain.
            script = '''source "$1/scripts/module-build.sh"
command() { [[ "$*" == "-v gcc" ]]; }
gcc() { echo "$4"; }
choose_compiler "$2" "$3"
'''
            # A shell function has its own arguments; bake only the test version as literal data.
            script = script.replace('echo "$4"', 'echo ' + shlex.quote(host_version))
            return subprocess.run(['bash', '-c', script, 'test', str(ROOT), str(path), policy], capture_output=True, text=True)

    def test_gcc10_host_gcc13_kernel_uses_container(self):
        result = self.select('auto')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'docker:13')

    def test_matching_native_compiler_used(self):
        result = self.select('auto', host_version='13.2.0')
        self.assertEqual(result.stdout.strip(), 'native:gcc')

    def test_native_only_fails_on_mismatch(self):
        self.assertNotEqual(self.select('native').returncode, 0)


if __name__ == '__main__': unittest.main()
