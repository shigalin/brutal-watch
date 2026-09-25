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

    def test_list_without_godebug_preserves_existing_entries(self):
        for environment in (
            '\n      # keep this comment\n      - TZ=Asia/Shanghai\n      - TOKEN=${NODE_TOKEN}',
            ' [TZ=Asia/Shanghai, "TOKEN=${NODE_TOKEN}"]',
        ):
            with self.subTest(environment=environment):
                raw = 'services:\n  node:\n    environment:' + environment + '\n'
                updated, env = self.check(raw, 'netdns=go')
                self.assertEqual(env, ['GODEBUG=netdns=go,multipathtcp=0', 'TZ=Asia/Shanghai', 'TOKEN=${NODE_TOKEN}'])
                if '# keep this comment' in raw:
                    self.assertIn('# keep this comment', updated)

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
    def run_case(self, changed_image=False, fail_restart=False, unsafe=False, relative_path=False,
                 cli_stdout=None, fail_config=False, fail_rollback=False, fail_restore=False):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'compose.yml'
            original = 'services:\n  node:\n    image: example:stable\n'
            path.write_text(original)
            actions = []
            atomic_text = setup.atomic_text
            writes = []
            def write_file(*args):
                writes.append(args)
                if fail_restore and len(writes) == 2:
                    raise OSError('private-file-error')
                return atomic_text(*args)
            class Host:
                def run(self, argv, **kwargs):
                    actions.append((argv, kwargs))
                    if argv[:2] == ['docker', 'inspect']:
                        return json.dumps([{'Image': 'sha256:old', 'Config': {'Env': [], 'Labels': {
                            'com.docker.compose.service': 'node',
                            'com.docker.compose.project.config_files': path.name if relative_path else str(path),
                            'com.docker.compose.project.working_dir': folder,
                            'com.docker.compose.project': 'existing-project'}}}])
                    if argv[:3] == ['docker', 'compose', 'version']: return '2.30'
                    if argv[:3] == ['docker', 'image', 'inspect']:
                        return 'sha256:new' if changed_image else 'sha256:old'
                    if argv[-1] == 'config':
                        if fail_config: raise setup.watch.WatchError('PANEL_TOKEN=private-test-value')
                        return path.read_text()
                    if 'up' in argv:
                        attempts = sum('up' in a[0] for a in actions)
                        if fail_restart and attempts == 1: raise setup.watch.WatchError('simulated failure')
                        if fail_rollback and attempts == 2: raise setup.watch.WatchError('private-rollback-output')
                        return ''
                    raise AssertionError(argv)
                def peers(self, cfg):
                    if unsafe: raise setup.watch.UnsafeListener('still MPTCP')
                    return set()
            with patch.object(setup.watch, 'Host', Host), patch.object(setup.time, 'sleep'), \
                 patch.object(setup, 'atomic_text', write_file), \
                 contextlib.redirect_stdout(cli_stdout if cli_stdout is not None else io.StringIO()):
                if changed_image or fail_restart or unsafe or fail_config:
                    with self.assertRaises(ValueError) as caught:
                        setup.configure_node({'container': 'node', 'ports': [2053]})
                    message = str(caught.exception)
                    self.assertNotIn('private-', message)
                    if fail_restore:
                        self.assertIn('原文件恢复失败', message)
                        if unsafe or fail_restart:
                            self.assertIn('容器尚未回滚', message)
                            self.assertIn('运行状态需人工确认', message)
                        else:
                            self.assertIn('容器未重建', message)
                    elif changed_image or fail_config:
                        self.assertIn('容器未重建', message)
                    elif fail_rollback:
                        self.assertIn('容器回滚失败', message)
                    else:
                        self.assertIn('原文件和容器已恢复', message)
                    if fail_config: self.assertIn('Compose 配置渲染失败', message)
                    if changed_image: self.assertIn('本地镜像标签已变化', message)
                    if fail_restart: self.assertIn('重建目标容器失败', message)
                    if unsafe: self.assertIn('普通 TCP 监听检查失败', message)
                    if fail_restore:
                        self.assertEqual(next(path.parent.glob('*.before-brutal-watch-*')).read_text(), original)
                    else:
                        self.assertEqual(path.read_text(), original)
                else:
                    cfg = {'container': 'node', 'ports': [2053]}
                    if cli_stdout is None:
                        setup.configure_node(cfg)
                    else:
                        with patch.object(setup.sys, 'argv', ['setup.py', 'configure-node']), \
                             patch.object(setup.watch, 'config_load', return_value=cfg):
                            self.assertEqual(setup.main(), 0)
                    self.assertEqual(yaml.safe_load(path.read_text())['services']['node']['environment'], {'GODEBUG': 'multipathtcp=0'})
            self.assertEqual(len(list(path.parent.glob('*.before-brutal-watch-*'))), 1)
            commands = [a[0] for a in actions if 'up' in a[0]]
            for argv, kwargs in actions:
                if 'up' in argv or argv[-1] == 'config':
                    self.assertEqual(kwargs['cwd'], folder)
                    self.assertEqual(argv[argv.index('-f') + 1], str(path))
            return commands

    def test_success_uses_same_image_and_only_target_service(self):
        commands = self.run_case()
        self.assertEqual(len(commands), 1)
        self.assertIn('--no-deps', commands[0])
        self.assertIn('--no-build', commands[0])
        self.assertEqual(commands[0][-1], 'node')

    def test_latin1_output_does_not_trigger_configuration_rollback(self):
        out, err = io.BytesIO(), io.BytesIO()
        with io.TextIOWrapper(out, encoding='latin-1') as stdout, \
             io.TextIOWrapper(err, encoding='latin-1') as stderr, contextlib.redirect_stderr(stderr):
            self.assertEqual(len(self.run_case(cli_stdout=stdout)), 1)
            stdout.flush(); stderr.flush()
            self.assertIn('修改 GODEBUG', out.getvalue().decode('utf-8'))
            self.assertIn('普通 TCP 监听验证通过', out.getvalue().decode('utf-8'))
            self.assertEqual(err.getvalue(), b'')

    def test_changed_image_restores_file_without_restarting(self):
        self.assertEqual(self.run_case(changed_image=True), [])

    def test_relative_compose_path_uses_project_working_directory(self):
        self.assertEqual(len(self.run_case(relative_path=True)), 1)

    def test_relative_compose_path_without_absolute_working_directory_is_rejected(self):
        for working_dir in ('', 'relative-project'):
            obj = [{'Config': {'Labels': {
                'com.docker.compose.service': 'node',
                'com.docker.compose.project.config_files': 'compose.yml',
                'com.docker.compose.project.working_dir': working_dir}}}]
            with self.subTest(working_dir=working_dir), patch.object(setup.watch.Host, 'run', return_value=json.dumps(obj)) as run:
                with self.assertRaisesRegex(ValueError, '无法确定现有 Compose 文件'):
                    setup.configure_node({'container': 'node'})
                self.assertEqual(run.call_count, 1)

    def test_restart_failure_restores_original_configuration(self):
        self.assertEqual(len(self.run_case(fail_restart=True)), 2)

    def test_still_mptcp_rolls_back_instead_of_claiming_success(self):
        self.assertEqual(len(self.run_case(unsafe=True)), 2)

    def test_config_error_reports_stage_without_leaking_output(self):
        self.assertEqual(self.run_case(fail_config=True), [])

    def test_rollback_failure_is_reported_separately(self):
        self.assertEqual(len(self.run_case(fail_restart=True, fail_rollback=True)), 2)

    def test_file_restore_failure_keeps_backup_and_reports_failure(self):
        self.assertEqual(self.run_case(changed_image=True, fail_restore=True), [])

    def test_restore_failure_after_recreate_reports_container_not_rolled_back(self):
        self.assertEqual(len(self.run_case(unsafe=True, fail_restore=True)), 1)


if __name__ == '__main__': unittest.main()
