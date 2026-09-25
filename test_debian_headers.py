import contextlib
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from scripts import debian_headers as headers


KERNEL = '6.12.57+deb13-amd64'
VERSION = '6.12.57-1'
IMAGE = 'linux-image-' + KERNEL
HEADER = 'linux-headers-' + KERNEL
COMMON = 'linux-headers-6.12.57+deb13-common'
KBUILD = 'linux-kbuild-6.12.57+deb13'


class PlanTests(unittest.TestCase):
    def test_only_new_exact_packages_are_accepted(self):
        plan = 'Inst {} ({} local-deb [amd64])\nInst gcc-14 (14.2.0 Debian:13 [amd64])\n'.format(HEADER, VERSION)
        self.assertEqual(headers.validate_plan(plan, {HEADER: VERSION})[HEADER], VERSION)

    def test_removal_upgrade_downgrade_and_kernel_install_are_rejected(self):
        for line in ('Remv old-package [1.0]', 'Inst libelf1t64 [1.0] (2.0 Debian [amd64])',
                     'Inst gcc-14 [2.0] (1.0 Debian [amd64])', 'Inst linux-image-new (1.0 Debian [amd64])',
                     'Inst linux-headers-amd64 (1.0 Debian [amd64])', 'Inst docker.io (1.0 Debian [amd64])'):
            with self.subTest(line=line), self.assertRaises(headers.HeaderError):
                headers.validate_plan(line, {})

    def test_solver_must_use_downloaded_version(self):
        with self.assertRaises(headers.HeaderError):
            headers.validate_plan('Inst {} (999 Debian [amd64])'.format(HEADER), {HEADER: VERSION})

    def test_legacy_build_dependencies_and_unknown_expressions(self):
        depends = 'linux-headers-5.10.0-34-common (= 5.10.234-1), linux-kbuild-5.10 (>= 5.10.234-1), linux-compiler-gcc-10-x86, linux-base'
        self.assertEqual(len(list(headers.build_dependencies(depends))), 3)
        with self.assertRaises(headers.HeaderError):
            list(headers.build_dependencies('linux-kbuild-5.10 | unknown-provider'))


class RecoveryTests(unittest.TestCase):
    def exercise(self, corrupt=False, wrong_control=False, wrong_arch=False, custom=False,
                 missing_image=False, reuse_kbuild=False, native_kbuild=False, bad_plan=False,
                 bad_signature=False, wrong_signed_hash=False, missing_keyring=False, foreign_source=False,
                 first_index_failure=None, clock=None, record_result=None, wait_minutes=None):
        self.calls = []
        self.index_calls = 0
        self.index_limits = []
        self.output, self.diagnostics = io.StringIO(), io.StringIO()
        state = {} if missing_image else {IMAGE: [VERSION, 'amd64', 'custom-linux' if custom else 'linux-signed-amd64']}
        if reuse_kbuild:
            state[KBUILD] = [VERSION, 'amd64', 'linux']
        controls = {}
        blobs = {}
        for name in (HEADER, COMMON, KBUILD):
            body = name.encode('ascii')
            digest = hashlib.sha1(body).hexdigest()
            controls[digest] = {
                'Package': name, 'Version': VERSION, 'Architecture': 'all' if name == COMMON else 'amd64',
                'Source': 'linux', 'Pre-Depends': '',
                'Depends': '{0} (= {2}), {1}, {3} (= {2}) | {3}-unsigned (= {2}), gcc-14'.format(COMMON, KBUILD, VERSION, IMAGE) if name == HEADER else '',
            }
            blobs[digest] = body

        def metadata(path, timeout=300):
            self.calls.append(('metadata', path))
            if path.startswith('/mr/file/'):
                return {'result': [{'archive_name': 'debian', 'first_seen': '20251106T031859Z'}]}
            if path.startswith('/mr/package/linux/'):
                return {'result': [{'name': name, 'version': VERSION} for name in (HEADER, COMMON, KBUILD)]}
            name = unquote(path.split('/')[3])
            if name == IMAGE:
                return {'result': [{'architecture': 'amd64', 'hash': '0' * 40}]}
            digest = hashlib.sha1(name.encode('ascii')).hexdigest()
            arch = 'arm64' if wrong_arch else controls[digest]['Architecture']
            return {'result': [{'architecture': arch, 'hash': digest}]}

        def run(args, optional=False, visible=False, timeout=300, progress=None):
            self.calls.append(tuple(args))
            output, code = '', 0
            if args[:2] == ['dpkg', '--print-architecture']:
                output = 'amd64'
            elif args[:2] == ['dpkg', '--compare-versions']:
                code = int(args[2] != args[4])
            elif args[0] == 'dpkg-query':
                info = state.get(args[-1])
                output, code = ('installed\t' + '\t'.join(info), 0) if info else ('', 1)
            elif args[0] == 'apt-cache':
                if 'show' in args:
                    if record_result and self.index_calls == 1:
                        clock[0] += timeout
                        if record_result == 'timeout':
                            raise subprocess.TimeoutExpired(args, timeout)
                        return subprocess.CompletedProcess(args, 0, '', '')
                    name = args[-1].split('=')[0]
                    digest = hashlib.sha1(name.encode()).hexdigest()
                    record = dict(controls[digest], SHA256='0' * 64 if wrong_signed_hash else hashlib.sha256(blobs[digest]).hexdigest(),
                                  Size=str(len(blobs[digest])))
                    output = '\n'.join('{}: {}'.format(key, value) for key, value in record.items())
                else:
                    output = 'Candidate: ' + (VERSION if native_kbuild and args[-1] == KBUILD else '(none)')
            elif args[0] == 'curl':
                digest = args[args.index('-o') - 1].rsplit('/', 1)[-1]
                Path(args[-1]).write_bytes(b'corrupt' if corrupt else blobs[digest])
            elif args[0] == 'dpkg-deb':
                output = controls[Path(args[2]).stem][args[3]]
                if wrong_control and args[3] == 'Package': output = 'wrong-package'
                if foreign_source and args[3] == 'Source' and controls[Path(args[2]).stem]['Package'] == KBUILD:
                    output = 'linux (1.0)'
            elif args[0] == 'apt-get':
                if args[-1] == 'update':
                    self.index_calls += 1
                    self.index_limits.append(timeout)
                    self.assertGreater(timeout, 0)
                    self.assertLessEqual(timeout, headers.INDEX_TIMEOUT)
                    self.assertTrue(progress)
                    self.assertIn('Acquire::AllowInsecureRepositories=false', args)
                    self.assertIn('Acquire::AllowWeakRepositories=false', args)
                    self.assertIn('APT::Get::AllowUnauthenticated=false', args)
                    self.assertIn('APT::Update::Error-Mode=any', args)
                    source = Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('Dir::Etc::sourcelist=')))
                    self.assertIn('signed-by=' + str(headers.KEYRING), source.read_text())
                    self.assertIn('check-valid-until=no', source.read_text())
                    self.assertIn('Dir::Etc::sourceparts=-', args)
                    if first_index_failure == 'all-timeouts' or (self.index_calls == 1 and first_index_failure == 'timeout'):
                        if clock is not None: clock[0] += timeout
                        raise subprocess.TimeoutExpired(args, timeout)
                    if self.index_calls == 1 and first_index_failure == 'error':
                        return subprocess.CompletedProcess(args, 100, '', 'E: suite unavailable')
                    return subprocess.CompletedProcess(args, 100 if bad_signature else 0, '', 'BADSIG' if bad_signature else '')
                names = [controls[Path(arg).stem]['Package'] for arg in args if arg.endswith('.deb')]
                if '-s' in args:
                    output = '\n'.join('Inst {} ({} local-deb [amd64])'.format(name, VERSION) for name in names)
                    if bad_plan: output += '\nInst libc6 [1.0] (2.0 Debian [amd64])'
                else:
                    self.assertTrue(visible)
                    for name in names:
                        state[name] = [VERSION, 'all' if name == COMMON else 'amd64', 'linux']
            else:
                raise AssertionError(args)
            return subprocess.CompletedProcess(args, code, output, '')

        with tempfile.TemporaryDirectory() as folder, patch.object(headers, 'run', run), \
             patch.object(headers, 'metadata', metadata), contextlib.redirect_stdout(self.output), \
             contextlib.redirect_stderr(self.diagnostics), patch.object(headers, 'KEYRING', Path(folder) / 'keyring.gpg'), \
             (patch.object(headers.time, 'monotonic', side_effect=lambda: clock[0]) if clock is not None else contextlib.nullcontext()):
            if not missing_keyring: headers.KEYRING.write_bytes(b'test-keyring')
            recovery = headers.Recovery(KERNEL, Path(folder) / 'headers', 'trixie', wait_minutes)
            recovery.collect()
            recovery.install()
            return recovery.packages

    def test_exact_archive_set_downloaded_checked_and_installed(self):
        self.assertEqual(self.exercise(), {HEADER: VERSION, COMMON: VERSION, KBUILD: VERSION})
        apt = [call for call in self.calls if call[0] == 'apt-get' and 'install' in call]
        self.assertIn('-s', apt[0])
        self.assertIn('-y', apt[1])
        self.assertIn('--no-remove', apt[1])

    def test_existing_or_available_build_dependency_is_reused(self):
        for kwargs in ({'reuse_kbuild': True}, {'native_kbuild': True}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.exercise(**kwargs), {HEADER: VERSION, COMMON: VERSION})

    def test_unavailable_or_timed_out_index_falls_through_without_false_errors(self):
        for failure in ('error', 'timeout'):
            with self.subTest(failure=failure):
                self.assertEqual(len(self.exercise(first_index_failure=failure)), 3)
                self.assertEqual(self.index_calls, 2)
                self.assertEqual(self.diagnostics.getvalue(), '')

    def test_all_timeouts_report_summary_without_installing(self):
        with self.assertRaisesRegex(headers.HeaderError, '未找到通过 Debian 签名验证'):
            self.exercise(first_index_failure='all-timeouts')
        self.assertIn('索引更新超过', self.diagnostics.getvalue())
        self.assertFalse(any(call[0] == 'apt-get' and '-y' in call for call in self.calls))

    def test_shared_lookup_budget_clips_next_attempt_and_stops_before_more_branches(self):
        with patch.object(headers, 'LOOKUP_BUDGET', 1000), patch.object(headers, 'AUTO_WAIT_BUDGET', 1000), \
             self.assertRaisesRegex(headers.HeaderError, '共享总时限'):
            self.exercise(first_index_failure='all-timeouts', clock=[0])
        self.assertEqual(self.index_limits, [900, 100])
        self.assertEqual(self.index_calls, 2)
        self.assertFalse(any(call[0] == 'apt-get' and '-y' in call for call in self.calls))

    def test_long_wait_declined_stops_without_installing(self):
        with patch.object(headers, 'AUTO_WAIT_BUDGET', 100), patch.object(headers, 'LOOKUP_BUDGET', 1000), \
             patch.object(headers, 'confirm_long_wait', return_value=False) as confirm, \
             self.assertRaisesRegex(headers.HeaderError, '未获得继续长时间等待的确认'):
            self.exercise(first_index_failure='timeout', clock=[0])
        confirm.assert_called_once_with()
        self.assertEqual(self.index_limits, [100])
        self.assertFalse(any(call[0] == 'apt-get' and '-y' in call for call in self.calls))

    def test_preapproved_wait_works_without_terminal_confirmation(self):
        with patch.object(headers, 'confirm_long_wait', side_effect=AssertionError('must not prompt')):
            self.assertEqual(len(self.exercise(first_index_failure='timeout', clock=[0], wait_minutes=30)), 3)
        self.assertEqual(self.index_limits, [900, 900])

    def test_preapproved_wait_stops_at_selected_limit_without_prompt(self):
        with patch.object(headers, 'confirm_long_wait', side_effect=AssertionError('must not prompt')), \
             self.assertRaisesRegex(headers.HeaderError, '180 秒'):
            self.exercise(first_index_failure='all-timeouts', clock=[0], wait_minutes=3)
        self.assertEqual(self.index_limits, [180])
        self.assertFalse(any(call[0] == 'apt-get' and '-y' in call for call in self.calls))

    def test_long_wait_confirmed_retries_current_index_once(self):
        clock = [0]
        def consent_after_pause():
            clock[0] += 250
            return True
        with patch.object(headers, 'AUTO_WAIT_BUDGET', 100), patch.object(headers, 'LOOKUP_BUDGET', 1000), \
             patch.object(headers, 'confirm_long_wait', side_effect=consent_after_pause) as confirm:
            self.assertEqual(len(self.exercise(first_index_failure='timeout', clock=clock)), 3)
        confirm.assert_called_once_with()
        self.assertEqual(self.index_limits, [100, 800])
        updates = [call for call in self.calls if call[0] == 'apt-get' and call[-1] == 'update']
        self.assertEqual(updates[0], updates[1])
        self.assertEqual(self.diagnostics.getvalue(), '')

    def test_confirmed_long_wait_still_stops_at_total_limit(self):
        with patch.object(headers, 'AUTO_WAIT_BUDGET', 100), patch.object(headers, 'LOOKUP_BUDGET', 1000), \
             patch.object(headers, 'confirm_long_wait', return_value=True) as confirm, \
             self.assertRaisesRegex(headers.HeaderError, '共享总时限'):
            self.exercise(first_index_failure='all-timeouts', clock=[0])
        confirm.assert_called_once_with()
        self.assertEqual(self.index_limits, [100, 800, 100])
        self.assertFalse(any(call[0] == 'apt-get' and '-y' in call for call in self.calls))

    def test_budget_exhaustion_preserves_package_lookup_failures(self):
        for result, reason in [('missing', '已验证索引中没有目标包的精确版本和架构'),
                               ('timeout', '包记录查询超时')]:
            with self.subTest(result=result):
                with patch.object(headers, 'LOOKUP_BUDGET', 100), \
                     self.assertRaisesRegex(headers.HeaderError, '共享总时限'):
                    self.exercise(clock=[0], record_result=result)
                self.assertEqual(self.diagnostics.getvalue().count(reason), 1)
                self.assertFalse(any(call[0] == 'apt-get' and '-y' in call for call in self.calls))

    def test_untrusted_or_missing_inputs_never_reach_package_install(self):
        for flag in ('corrupt', 'wrong_control', 'wrong_arch', 'custom', 'missing_image', 'bad_plan',
                     'bad_signature', 'wrong_signed_hash', 'missing_keyring', 'foreign_source'):
            with self.subTest(flag=flag), self.assertRaises(headers.HeaderError):
                try:
                    self.exercise(**{flag: True})
                finally:
                    self.assertFalse(any(call[0] == 'apt-get' and '-y' in call for call in self.calls))
                    if flag in ('bad_signature', 'wrong_signed_hash', 'missing_keyring'):
                        self.assertFalse(any(call[0] == 'dpkg-deb' for call in self.calls))
                    if flag in ('bad_signature', 'missing_keyring'):
                        self.assertFalse(any(call[0] == 'curl' for call in self.calls))


class ConfirmationTests(unittest.TestCase):
    def test_only_explicit_yes_from_terminal_continues(self):
        class Terminal(io.BytesIO):
            def isatty(self): return True
        for answer, expected in [('y\n', True), ('YES\n', True), ('n\n', False), ('\n', False), ('', False)]:
            with self.subTest(answer=answer):
                reader, writer = Terminal(answer.encode()), io.StringIO()
                with patch('builtins.open', side_effect=lambda path, mode, **kw:
                           contextlib.nullcontext(reader if mode == 'rb' else writer)), \
                     patch.object(headers.select, 'select', return_value=([reader], [], [])):
                    self.assertEqual(headers.confirm_long_wait(), expected)
                self.assertIn('[y/N]', writer.getvalue())

    def test_no_controlling_terminal_never_continues(self):
        with patch('builtins.open', side_effect=OSError('no tty')):
            self.assertFalse(headers.confirm_long_wait())

    def test_prompt_times_out_even_with_partial_input(self):
        for pending in (b'', b'y'):
            with self.subTest(pending=pending):
                read_fd, write_fd = os.pipe()
                class Terminal:
                    def isatty(self): return True
                    def fileno(self): return read_fd
                    def read(self, size): return os.read(read_fd, size)
                reader, writer = Terminal(), io.StringIO()
                try:
                    if pending: os.write(write_fd, pending)
                    with patch.object(headers, 'CONFIRM_TIMEOUT', 0.05), \
                         patch('builtins.open', side_effect=lambda path, mode, **kw:
                               contextlib.nullcontext(reader if mode == 'rb' else writer)):
                        started = time.monotonic()
                        self.assertFalse(headers.confirm_long_wait())
                        self.assertLess(time.monotonic() - started, 1)
                    self.assertIn('确认超时', writer.getvalue())
                finally:
                    os.close(read_fd)
                    os.close(write_fd)


class OutputTests(unittest.TestCase):
    def test_slow_command_reports_progress_and_terminates_on_timeout(self):
        output = io.StringIO()
        started = time.monotonic()
        with patch.object(headers, 'PROGRESS_INTERVAL', 0.02), contextlib.redirect_stdout(output), \
             self.assertRaises(subprocess.TimeoutExpired):
            headers.run([sys.executable, '-c', 'import time; time.sleep(3)'],
                        optional=True, timeout=0.15, progress='test-index')
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn('仍在下载或验证', output.getvalue())

    def test_visible_command_failure_keeps_reason_and_redacts_repository_credentials(self):
        output = io.StringIO()
        script = 'import sys; print("E: No space left on device"); print("https://user:private-value@example.test/pkg?token=private-value", file=sys.stderr); sys.exit(100)'
        with contextlib.redirect_stdout(output), self.assertRaises(headers.HeaderError):
            headers.run([sys.executable, '-c', script], visible=True)
        self.assertIn('No space left on device', output.getvalue())
        self.assertNotIn('private-value', output.getvalue())
        self.assertIn('<repository-url>', output.getvalue())


if __name__ == '__main__':
    unittest.main()
