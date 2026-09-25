import os
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
check_platform() { echo platform; }
check_docker() { echo docker; }
acquire_locks() { echo locked; }
release_runtime_lock() { echo unlocked; }
install_dependencies() { echo dependencies; }
project_source() { SOURCE_DIR=/mock-source; }
prepare_module() { echo prepare; %s; }
install_tool() { echo tool; }
install_module() { echo module; %s; }
python3() { echo "python:$*"; if [[ "$*" == *verify-node* ]]; then %s; fi; }
brutal-watch() { echo "watch:$*"; }
main %s
''' % ('return 1' if fail == 'prepare' else ':', 'return 1' if fail == 'module' else ':',
       'return 1' if fail == 'verify' else ':', args)
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
        self.assertLess(result.stdout.index('unlocked'), result.stdout.index('watch:check'))

    def test_explicit_configuration_and_enable_order(self):
        result = self.flow('--configure-node --enable --ports 443')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('config --ports 443', result.stdout)
        self.assertLess(result.stdout.index('prepare'), result.stdout.index('configure-node'))
        self.assertLess(result.stdout.index('configure-node'), result.stdout.index('\nmodule'))
        self.assertLess(result.stdout.index('watch:check'), result.stdout.index('watch:on'))

    def test_failed_node_validation_never_loads_module(self):
        result = self.flow('--enable', 'verify')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('prepare', result.stdout)
        self.assertNotIn('\ntool', result.stdout)
        self.assertNotIn('\nmodule', result.stdout)
        self.assertNotIn('watch:on', result.stdout)

    def test_failed_preparation_never_changes_node_or_installed_tool(self):
        result = self.flow('--configure-node --enable', 'prepare')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('configure-node', result.stdout)
        self.assertNotIn('\ntool', result.stdout)
        self.assertNotIn('\nmodule', result.stdout)

    def test_existing_v2_or_skip_module_needs_no_build_preparation(self):
        for skip in (False, True):
            script = '''source "$1/install.sh"
SKIP_MODULE=$2
modinfo() { echo 2.0.0; }
install_packages() { echo unexpected-package-install; return 1; }
modprobe() { echo unexpected-module-load; return 1; }
prepare_module
'''
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), str(int(skip))], capture_output=True, text=True)
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

    def test_clang_build_uses_llvm_without_gcc_override(self):
        for policy in ('auto', 'native'):
            for auto_conf in (False, True):
                with self.subTest(policy=policy, auto_conf=auto_conf):
                    result = self.clang_build(policy, auto_conf=auto_conf)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('LLVM=1', result.stdout.splitlines())
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
