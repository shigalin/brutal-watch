#!/usr/bin/env python3
"""Installer support. Never prints Docker environment, Compose content or credentials."""
import argparse
import copy
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('watch', ROOT / 'brutal_watch.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def merge_godebug(value):
    if not isinstance(value, str) or '$' in value:
        raise ValueError('GODEBUG 使用动态插值或非字符串值，请手动配置，避免破坏原有语义')
    parts = [p.strip() for p in value.split(',') if p.strip() and p.strip().split('=', 1)[0] != 'multipathtcp']
    return ','.join(parts + ['multipathtcp=0'])


def update_compose(raw, name, inherited_godebug=''):
    import yaml
    old = yaml.safe_load(raw)
    original_env = old['services'][name].get('environment')
    if isinstance(original_env, (dict, list)):
        seen, references = set(), [0]
        def visit(obj):
            if obj is original_env: references[0] += 1
            if not isinstance(obj, (dict, list)) or id(obj) in seen: return
            seen.add(id(obj))
            for child in (obj.values() if isinstance(obj, dict) else obj): visit(child)
        visit(old)
        if references[0] > 1:
            raise ValueError('environment 与其他配置共享 YAML anchor，请手动调整')
    expected = copy.deepcopy(old)
    service = expected['services'][name]
    document = yaml.compose(raw)
    def item(node, key):
        if not isinstance(node, yaml.MappingNode):
            raise ValueError('不支持此 Compose YAML 结构')
        return next(((k, v) for k, v in node.value if k.value == key), (None, None))
    _, services_node = item(document, 'services')
    service_key, node = item(services_node, name)
    env_key, env = item(node, 'environment')
    current = service.get('environment')
    if current is None:
        service['environment'] = {'GODEBUG': merge_godebug(inherited_godebug)}
        setting = json.dumps(service['environment']['GODEBUG'])
        if env_key is None:
            insert = raw.find('\n', service_key.end_mark.index)
            if insert < 0 or node.flow_style:
                raise ValueError('请手动为行内 Compose 服务增加 GODEBUG')
            indent = ' ' * (service_key.start_mark.column + 2)
            change = '\n' + indent + 'environment:\n' + indent + '  GODEBUG: ' + setting
        else:
            insert = raw.find('\n', env_key.end_mark.index)
            if insert < 0:
                insert = len(raw)
            change = '\n' + ' ' * (env_key.start_mark.column + 2) + 'GODEBUG: ' + setting
        updated = raw[:insert] + change + raw[insert:]
    elif isinstance(current, dict):
        service['environment']['GODEBUG'] = merge_godebug(current.get('GODEBUG', inherited_godebug))
        value_key, value = item(env, 'GODEBUG')
        if env.start_mark.index < node.start_mark.index or env.end_mark.index > node.end_mark.index:
            raise ValueError('environment 使用外部 YAML anchor，请手动调整')
        if env.flow_style:
            updated = raw[:env.start_mark.index] + json.dumps(service['environment']) + raw[env.end_mark.index:]
        elif value_key is not None:
            updated = raw[:value.start_mark.index] + json.dumps(service['environment']['GODEBUG']) + raw[value.end_mark.index:]
        else:
            insert = raw.find('\n', env_key.end_mark.index)
            change = '\n' + ' ' * (env_key.start_mark.column + 2) + 'GODEBUG: ' + json.dumps(service['environment']['GODEBUG'])
            updated = raw[:insert] + change + raw[insert:]
    elif isinstance(current, list):
        values = [v for v in current if isinstance(v, str) and v.startswith('GODEBUG=')]
        if len(values) > 1:
            raise ValueError('存在重复 GODEBUG，请先人工整理')
        desired = 'GODEBUG=' + merge_godebug(values[0].split('=', 1)[1] if values else inherited_godebug)
        service['environment'] = [desired if v in values else v for v in current]
        if not values:
            service['environment'].append(desired)
        if env.flow_style:
            updated = raw[:env.start_mark.index] + json.dumps(service['environment']) + raw[env.end_mark.index:]
        elif values:
            value = next(v for v in env.value if v.value == values[0])
            updated = raw[:value.start_mark.index] + json.dumps(desired) + raw[value.end_mark.index:]
        else:
            insert = raw.find('\n', env_key.end_mark.index)
            updated = raw[:insert] + '\n' + ' ' * (env_key.start_mark.column + 2) + '- ' + json.dumps(desired) + raw[insert:]
    else:
        raise ValueError('不支持 environment 类型，请手动配置')
    if yaml.safe_load(updated) != expected:
        raise ValueError('Compose 编辑影响了目标设置以外的内容，拒绝写入')
    return updated


def atomic_text(path, text, mode=0o600):
    temp = path.with_name(path.name + '.brutal-watch.tmp')
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def ensure_idle():
    data = watch.Store(watch.STATE).load(watch.Host().boot_id())
    if data['enabled'] or data['entries']:
        raise ValueError('工具尚未关闭或仍有待清理记录，请先 brutal-watch off 并处理清理错误')
    host = watch.Host()
    states = host.timer_status()
    if states['is-active'] == 'active':
        raise ValueError('定时器仍活动，请先关闭')


def write_config(args):
    path = watch.CONFIG
    if path.exists():
        # Reinstallation must not overwrite working user configuration.
        current = watch.config_load(path)
        requested = {k: v for k, v in vars(args).items() if k in current and v is not None}
        if any(current[k] != v for k, v in requested.items()):
            raise ValueError('现有配置与命令参数不同，请关闭后手动修改 /etc/brutal-watch/config.json；安装器不会覆盖')
        return current
    cfg = json.loads((ROOT / 'config.example.json').read_text())
    for key in cfg:
        value = getattr(args, key, None)
        if value is not None: cfg[key] = value
    watch.validate_config(cfg)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_text(path, json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    return watch.config_load(path)


def configure_node(cfg):
    import yaml
    host = watch.Host()
    obj = json.loads(host.run(['docker', 'inspect', cfg['container']]))[0]
    labels = obj['Config'].get('Labels') or {}
    name = labels.get('com.docker.compose.service')
    paths = [Path(p) for p in labels.get('com.docker.compose.project.config_files', '').split(',') if p]
    working_dir = labels.get('com.docker.compose.project.working_dir')
    if working_dir and Path(working_dir).is_absolute():
        paths = [p if p.is_absolute() else Path(working_dir) / p for p in paths]
    if not name or not paths or any(not p.is_absolute() or not p.is_file() for p in paths):
        raise ValueError('无法确定现有 Compose 文件；请手动设置 GODEBUG=multipathtcp=0 并重建容器')
    candidates = [p for p in paths if name in (yaml.safe_load(p.read_text()).get('services') or {})]
    target = candidates[-1]
    raw = target.read_text()
    inherited = [v.split('=', 1)[1] for v in obj['Config'].get('Env', []) if v.startswith('GODEBUG=')]
    if len(inherited) > 1: raise ValueError('运行容器有重复 GODEBUG，请先人工整理')
    changed = update_compose(raw, name, inherited[0] if inherited else '')
    if changed == raw:
        try:
            host.peers(cfg)
            return
        except watch.UnsafeListener:
            pass  # File is correct, but the running container has not applied it yet.
    try:
        host.run(['docker', 'compose', 'version'])
        command = ['docker', 'compose']
    except watch.WatchError:
        command = ['docker-compose']
    project = labels.get('com.docker.compose.project')
    if project: command += ['-p', project]
    for path in paths: command += ['-f', str(path)]
    working_dir = working_dir or str(paths[0].parent)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = target.with_name(target.name + '.before-brutal-watch-' + stamp)
    shutil.copy2(target, backup)
    atomic_text(target, changed, target.stat().st_mode & 0o777)
    recreate = command + ['up', '-d', '--no-deps', '--no-build', '--force-recreate', name]
    recreate_started = False
    try:
        # Capture rendered config privately: it can contain credentials.
        rendered = yaml.safe_load(host.run(command + ['config'], timeout=30, cwd=working_dir))
        image = rendered['services'][name]['image']
        actual = host.run(['docker', 'image', 'inspect', '-f', '{{.Id}}', image])
        if actual != obj['Image']:
            raise ValueError('本地镜像标签已变化，拒绝借配置调整更换节点镜像')
        print('修改 GODEBUG 并重建目标容器；备份：' + str(backup), flush=True)
        recreate_started = True
        host.run(recreate, timeout=90, cwd=working_dir)
        last = None
        for _ in range(20):
            try:
                host.peers(cfg)
                last = None
                break
            except (watch.WatchError, OSError) as exc:
                last = exc
                time.sleep(1)
        if last: raise last
    except Exception:
        atomic_text(target, raw, backup.stat().st_mode & 0o777)
        if recreate_started:
            try:
                if host.run(['docker', 'image', 'inspect', '-f', '{{.Id}}', image]) != obj['Image']:
                    raise ValueError('image changed during rollback')
                host.run(recreate, timeout=90, cwd=working_dir)
            except Exception:
                raise ValueError('节点调整失败，文件已恢复但容器恢复失败；请检查备份：' + str(backup)) from None
        raise ValueError('节点调整未通过验证，已恢复原文件和容器；备份：' + str(backup)) from None


def resolve_mptcp_block(cfg):
    host = watch.Host()
    host.module()
    host.peers(cfg)
    store = watch.Store(watch.STATE)
    data = store.load(host.boot_id())
    reason = data.get('blocked_reason', '')
    if not reason:
        return
    if 'mptcp' not in reason.lower():
        raise ValueError('存在非 MPTCP 故障标记，安装器不会自动清除，请先处理该故障')
    data.pop('blocked_reason', None)
    data['error'] = ''
    data['compatibility_fix'] = {'method': 'verified plain TCP listener', 'resolved_by': 'brutal-watch installer'}
    store.save(data)
    print('实际监听协议已验证，已解除原 MPTCP 故障标记')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['idle', 'config', 'configure-node', 'verify-node', 'resolve-mptcp-block'])
    parser.add_argument('--container')
    parser.add_argument('--ports', type=lambda s: [int(x) for x in s.split(',')])
    parser.add_argument('--rate-mbps', type=int)
    parser.add_argument('--ttl-seconds', type=int)
    parser.add_argument('--max-ips', type=int)
    args = parser.parse_args()
    try:
        if args.action == 'idle':
            ensure_idle()
        elif args.action == 'config':
            write_config(args)
        else:
            cfg = watch.config_load(watch.CONFIG)
            if args.action == 'configure-node': configure_node(cfg)
            if args.action == 'resolve-mptcp-block': resolve_mptcp_block(cfg)
            peers = watch.Host().peers(cfg)
            print('普通 TCP 监听验证通过；当前客户端 IP 数：' + str(len(peers)))
    except Exception as exc:
        # Do not expose YAML exception snippets or captured process output.
        message = str(exc) if isinstance(exc, (ValueError, watch.WatchError)) else type(exc).__name__
        if isinstance(exc, yaml_error_types()): message = 'Compose/YAML 解析失败，未输出可能含凭据的文件内容'
        print('安装检查失败：' + message, file=sys.stderr)
        return 1
    return 0


def yaml_error_types():
    try:
        import yaml
        return (yaml.YAMLError,)
    except ImportError:
        return ()


if __name__ == '__main__': sys.exit(main())
