#!/usr/bin/env python3
"""Real APT trust/isolation checks. Run only in a disposable Debian container.

Does not install a kernel, load modules, or mock apt-get/apt-cache. It temporarily
adds an invalid system source to prove the helper ignores system source files.
"""
import contextlib
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import debian_headers as headers


def check(condition, message):
    if not condition:
        raise RuntimeError(message)
    print('PASS: ' + message, flush=True)


def digest_tree(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def main():
    if not Path('/.dockerenv').is_file() or sys.argv[1:] != ['--disposable-container']:
        raise SystemExit('Requires --disposable-container inside a disposable Debian container')
    poison = Path('/etc/apt/sources.list.d/brutal-watch-isolation-test.list')
    if poison.exists():
        raise SystemExit('Test source already exists; refusing to overwrite')
    status = Path('/var/lib/dpkg/status').read_bytes()
    normal_lists = digest_tree(Path('/var/lib/apt/lists'))
    name, version = 'linux-headers-6.1.0-18-amd64', '6.1.76-1'
    try:
        poison.write_text('THIS_IS_NOT_A_VALID_APT_SOURCE\n')
        invalid = headers.run(['apt-cache', 'policy', 'bash'], optional=True)
        check(invalid.returncode != 0, 'invalid global source is detected by ordinary APT')
        with tempfile.TemporaryDirectory(prefix='brutal-apt-') as folder:
            root = Path(folder)
            recovery = headers.Recovery('6.1.0-18-amd64', root, 'bookworm')
            control, source = recovery.download(name, version)
            check(control['Package'] == name and source == version,
                  'real isolated APT authenticated and downloaded the exact historical headers')
            options = next(value for value in recovery.indexes.values() if value is not None)
            isolated = headers.run(['apt-cache'] + options + ['policy', 'bash'], optional=True)
            with_status = headers.run(['apt-cache'] + options +
                                      ['-o', 'Dir::State::status=/var/lib/dpkg/status', 'policy', 'bash'])
            installed = headers.installed('bash')[0]
            check('Installed: ' + installed not in isolated.stdout and
                  'Installed: ' + installed in with_status.stdout,
                  'empty dpkg status isolates installed packages (negative control passed)')
            check(Path('/var/lib/dpkg/status').read_bytes() == status and
                  digest_tree(Path('/var/lib/apt/lists')) == normal_lists,
                  'global dpkg status and APT lists remain unchanged')

            # Finish this recovery before unrelated negative checks consume its wait budget.
            poison.unlink()
            dependencies = list(headers.build_dependencies(control['Depends']))
            common, relation, common_version = next(dep for dep in dependencies if dep[0].endswith('-common'))
            check(relation == '=', 'headers declare an exact common-headers dependency')
            common_control, common_source = recovery.download(common, common_version)
            check(common_control['Package'] == common and common_source == source,
                  'common headers authenticate successfully and share the source version')
            plan = headers.run(['apt-get', '-s', '--no-remove', '--no-install-recommends', 'install'] + recovery.paths)
            additions = headers.validate_plan(plan.stdout, recovery.packages)
            check(additions.get(name) == version and additions.get(common) == common_version,
                  'validate_plan accepts the real APT simulation with both exact header packages')

            # A valid signature must still fail with an unrelated/empty trust anchor.
            bad_keyring = root / 'empty-keyring.gpg'
            bad_keyring.write_bytes(b'')
            bad_root = root / 'bad-signature'
            bad_root.mkdir()
            bad = headers.Recovery('6.1.0-18-amd64', bad_root, 'bookworm')
            output, error = io.StringIO(), io.StringIO()
            rejected = False
            with patch.object(headers, 'KEYRING', bad_keyring), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                try:
                    bad.download(name, version)
                except headers.HeaderError:
                    rejected = True
            check(rejected and not list(bad_root.glob('*.deb')) and
                  any(word in error.getvalue() for word in ('NO_PUBKEY', 'not signed', 'signatures')),
                  'real APT rejects an untrusted keyring before downloading the deb')

            # Use a fresh list directory so a previously accepted index cannot help.
            source_path = Path(next(arg.split('=', 1)[1] for arg in options
                                    if arg.startswith('Dir::Etc::sourcelist=')))
            original = source_path.read_text()
            source_path.write_text(original.replace('check-valid-until=no', 'check-valid-until=yes'))
            expired_lists = root / 'expired-lists'
            expired_lists.mkdir()
            try:
                expired = headers.run(['apt-get'] + options +
                                      ['-o', 'Dir::State::lists=' + str(expired_lists), 'update'], optional=True)
            finally:
                source_path.write_text(original)
            check(expired.returncode != 0 and 'expired' in (expired.stdout + expired.stderr).lower(),
                  'historical Valid-Until relaxation is necessary and limited to the temporary source')

    finally:
        if poison.exists():
            poison.unlink()
    check(Path('/var/lib/dpkg/status').read_bytes() == status and
          digest_tree(Path('/var/lib/apt/lists')) == normal_lists,
          'verification completed without installing packages or modifying global indexes')


if __name__ == '__main__':
    main()
