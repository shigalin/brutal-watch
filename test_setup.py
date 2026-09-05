import copy
import json
import tempfile
from pathlib import Path
import unittest
import contextlib
import io
from unittest.mock import patch

import yaml
from scripts import setup


class ComposeTests(unittest.TestCase):
    def check(self, raw, inherited=''):
        before = yaml.safe_load(raw)
        updated = setup.update_compose(raw, 'node', inherited)
        after = yaml.safe_load(updated)
        other_before = copy.deepcopy(before)
        other_after = copy.deepcopy(after)
        other_before['services']['node'].pop('environment', None)
        other_after['services']['node'].pop('environment', None)
        self.assertEqual(other_before, other_after)
        return updated, after['services']['node']['environment']

    def test_new_environment_preserves_comments(self):
        raw = 'services:\n  node:\n    # keep this comment\n    image: example:stable\n'
        updated, env = self.check(raw)
        self.assertIn('# keep this comment', updated)
        self.assertEqual(env, {'GODEBUG': 'multipathtcp=0'})

    def test_map_preserves_existing_values_and_other_godebug_options(self):
        raw = 'services:\n  node:\n    environment:\n      TOKEN: ${NODE_TOKEN}\n      GODEBUG: "http2client=0,multipathtcp=2"\n'
        updated, env = self.check(raw)
        self.assertEqual(env, {'TOKEN': '${NODE_TOKEN}', 'GODEBUG': 'http2client=0,multipathtcp=0'})
        self.assertIn('TOKEN: ${NODE_TOKEN}', updated)

    def test_list_environment(self):
        raw = 'services:\n  node:\n    environment:\n      - TOKEN=${NODE_TOKEN}\n      - GODEBUG=netdns=go,multipathtcp=2\n'
        _, env = self.check(raw)
        self.assertEqual(env, ['TOKEN=${NODE_TOKEN}', 'GODEBUG=netdns=go,multipathtcp=0'])

    def test_flow_map_and_list(self):
        for env in ('{}', '[]', '{A: example}', '[A=example]'):
            _, result = self.check('services:\n  node:\n    environment: ' + env + '\n')
            self.assertTrue('GODEBUG' in result if isinstance(result, dict) else 'GODEBUG=multipathtcp=0' in result)

    def test_inherited_image_godebug_is_preserved(self):
        for raw in ('services:\n  node:\n    image: test\n',
                    'services:\n  node:\n    environment:\n      A: example\n'):
            _, env = self.check(raw, 'netdns=go,multipathtcp=2')
            self.assertEqual(env['GODEBUG'], 'netdns=go,multipathtcp=0')

    def test_external_or_shared_anchor_rejected(self):
        for raw in (
            'x-env: &env\n  A: test\nservices:\n  node:\n    environment: *env\n',
            'services:\n  node:\n    environment: &env\n      A: test\n  other:\n    environment: *env\n'):
            with self.assertRaises(ValueError): setup.update_compose(raw, 'node')

    def test_dynamic_godebug_rejected(self):
        with self.assertRaises(ValueError):
            setup.update_compose('services:\n  node:\n    environment:\n      GODEBUG: ${GODEBUG}\n', 'node')

    def test_already_configured_is_semantically_idempotent(self):
        raw = 'services:\n  node:\n    environment:\n      GODEBUG: "multipathtcp=0"\n'
        self.assertEqual(setup.update_compose(raw, 'node'), raw)


class ConfigTests(unittest.TestCase):
    def args(self, **kwargs):
        import argparse
        return argparse.Namespace(**kwargs)

    def test_invalid_config_not_written(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            with patch.object(setup.watch, 'CONFIG', path):
                with self.assertRaises(setup.watch.WatchError):
                    setup.write_config(self.args(ports=[0]))
            self.assertFalse(path.exists())

    def test_reinstall_preserves_user_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            with patch.object(setup.watch, 'CONFIG', path):
                setup.write_config(self.args(ports=[443], rate_mbps=80))
                previous = path.read_bytes()
                cfg = setup.write_config(self.args())
                self.assertEqual(cfg['ports'], [443])
                self.assertEqual(path.read_bytes(), previous)
                with self.assertRaises(ValueError):
                    setup.write_config(self.args(rate_mbps=100))
                self.assertEqual(path.read_bytes(), previous)


class ConfigureTransactionTests(unittest.TestCase):
    def run_case(self, changed_image=False, fail_restart=False, unsafe=False):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'compose.yml'
            original = 'services:\n  node:\n    image: example:stable\n'
            path.write_text(original)
            actions = []
            class Host:
                def run(self, argv, **kwargs):
                    actions.append((argv, kwargs))
                    if argv[:2] == ['docker', 'inspect']:
                        return json.dumps([{'Image': 'sha256:old', 'Config': {'Env': [], 'Labels': {
                            'com.docker.compose.service': 'node',
                            'com.docker.compose.project.config_files': str(path),
                            'com.docker.compose.project.working_dir': folder,
                            'com.docker.compose.project': 'existing-project'}}}])
                    if argv[:3] == ['docker', 'compose', 'version']: return '2.30'
                    if argv[:3] == ['docker', 'image', 'inspect']:
                        return 'sha256:new' if changed_image else 'sha256:old'
                    if argv[-1] == 'config': return path.read_text()
                    if 'up' in argv:
                        attempts = sum('up' in a[0] for a in actions)
                        if fail_restart and attempts == 1: raise setup.watch.WatchError('simulated failure')
                        return ''
                    raise AssertionError(argv)
                def peers(self, cfg):
                    if unsafe: raise setup.watch.UnsafeListener('still MPTCP')
                    return set()
            with patch.object(setup.watch, 'Host', Host), patch.object(setup.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                if changed_image or fail_restart or unsafe:
                    with self.assertRaises(ValueError): setup.configure_node({'container': 'node', 'ports': [2053]})
                    self.assertEqual(path.read_text(), original)
                else:
                    setup.configure_node({'container': 'node', 'ports': [2053]})
                    self.assertEqual(yaml.safe_load(path.read_text())['services']['node']['environment'], {'GODEBUG': 'multipathtcp=0'})
            self.assertEqual(len(list(path.parent.glob('*.before-brutal-watch-*'))), 1)
            commands = [a[0] for a in actions if 'up' in a[0]]
            for argv, kwargs in actions:
                if 'up' in argv or argv[-1] == 'config': self.assertEqual(kwargs['cwd'], folder)
            return commands

    def test_success_uses_same_image_and_only_target_service(self):
        commands = self.run_case()
        self.assertEqual(len(commands), 1)
        self.assertIn('--no-deps', commands[0])
        self.assertIn('--no-build', commands[0])
        self.assertEqual(commands[0][-1], 'node')

    def test_changed_image_restores_file_without_restarting(self):
        self.assertEqual(self.run_case(changed_image=True), [])

    def test_restart_failure_restores_original_configuration(self):
        self.assertEqual(len(self.run_case(fail_restart=True)), 2)

    def test_still_mptcp_rolls_back_instead_of_claiming_success(self):
        self.assertEqual(len(self.run_case(unsafe=True)), 2)


if __name__ == '__main__': unittest.main()
