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
acquire_locks() { echo locked; }
release_runtime_lock() { echo unlocked; }
install_dependencies() { echo dependencies; }
project_source() { SOURCE_DIR=/mock-source; }
install_tool() { echo tool; }
install_module() { echo module; %s; }
python3() { echo "python:$*"; if [[ "$*" == *verify-node* ]]; then %s; fi; }
brutal-watch() { echo "watch:$*"; }
main %s
''' % ('return 1' if fail == 'module' else ':', 'return 1' if fail == 'verify' else ':', args)
        return subprocess.run(['bash', '-c', script, 'test', str(ROOT)], capture_output=True, text=True)

    def test_default_does_not_reconfigure_or_enable(self):
        result = self.flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('verify-node', result.stdout)
        self.assertNotIn('configure-node', result.stdout)
        self.assertNotIn('watch:on', result.stdout)
        self.assertLess(result.stdout.index('unlocked'), result.stdout.index('watch:check'))

    def test_explicit_configuration_and_enable_order(self):
        result = self.flow('--configure-node --enable --ports 443')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('config --ports 443', result.stdout)
        self.assertLess(result.stdout.index('configure-node'), result.stdout.index('\nmodule'))
        self.assertLess(result.stdout.index('watch:check'), result.stdout.index('watch:on'))

    def test_failed_node_validation_never_loads_module(self):
        result = self.flow('--enable', 'verify')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('\nmodule', result.stdout)
        self.assertNotIn('watch:on', result.stdout)

    def test_failed_module_install_never_enables(self):
        result = self.flow('--enable', 'module')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('watch:on', result.stdout)

    def test_help_does_not_require_root_or_linux(self):
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--configure-node', result.stdout)

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
