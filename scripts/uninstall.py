#!/usr/bin/env python3
"""Remove only brutal-watch-owned files; called with installer/runtime locks held."""
import argparse
import contextlib
import filecmp
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('watch', ROOT / 'brutal_watch.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

SOURCE = Path('/usr/src')
DKMS_CONFIG = Path('/etc/dkms')
UNITS = Path('/etc/systemd/system')
CONFIG = watch.CONFIG.parent
STATE = watch.STATE.parent
COMMAND = Path('/usr/local/sbin/brutal-watch')
LIBEXEC = Path('/usr/local/libexec/brutal-watch')
CTL = Path('/usr/local/bin/brutalctl')
LOADED = Path('/sys/module/brutal')
MANAGED_HEADER = '# Managed by brutal-watch'
UNLOAD_WAIT_SECONDS = 60


def run(argv):
    # Preserve DKMS progress/errors; never execute discovered configuration files.
    subprocess.run(argv, check=True)


def registered_versions():
    if not shutil.which('dkms'):
        if Path('/var/lib/dkms/tcp-brutal').exists():
            raise ValueError('存在 TCP Brutal DKMS 记录但缺少 dkms 命令，请修复后重试')
        return set()
    output = subprocess.check_output(['dkms', 'status'], text=True)
    versions = set()
    for line in output.splitlines():
        if line.startswith(('tcp-brutal/', 'tcp-brutal,')):
            match = re.match(r'^tcp-brutal(?:/|,\s*)([^,:\s]+)(?:[:,]|$)', line)
            if not match:
                raise ValueError('无法识别 TCP Brutal DKMS 记录，保留文件')
            versions.add(match[1])
    return versions


def plain_path(path, directory=False):
    if path.is_symlink() or (path.exists() and (not path.is_dir() if directory else not path.is_file())):
        raise ValueError('拒绝删除链接或类型异常的路径：' + str(path))


def module_plan():
    registered = registered_versions()
    versions = set(registered)
    versions.update(p.name[len('tcp-brutal-'):] for p in SOURCE.glob('tcp-brutal-*'))
    versions.update(p.name[len('tcp-brutal-'):-5] for p in DKMS_CONFIG.glob('tcp-brutal-*.conf'))
    owned, foreign = [], []
    for version in sorted(versions):
        match = re.fullmatch(r'2\.\d+\.\d+-bw([0-9a-f]{7})', version)
        if not match:
            foreign.append(version)
            continue
        source = SOURCE / ('tcp-brutal-' + version)
        override = DKMS_CONFIG / ('tcp-brutal-' + version + '.conf')
        plain_path(source, directory=True)
        plain_path(override)
        marker = source / '.brutal-watch-upstream'
        plain_path(marker)
        if source.exists():
            commit = marker.read_text().strip() if marker.exists() else ''
            if not re.fullmatch(r'[0-9a-f]{40}', commit) or not commit.startswith(match[1]):
                raise ValueError('模块源码归属无法确认：' + str(source))
        if override.exists():
            if MANAGED_HEADER not in override.read_text().splitlines():
                raise ValueError('DKMS 配置归属无法确认：' + str(override))
        # A source marker or managed override must survive for retry/recovery.
        if not source.exists() and not override.exists():
            raise ValueError('DKMS 记录缺少项目归属标记：' + version)
        if version in registered and not shutil.which('dkms'):
            raise ValueError('缺少 dkms，无法移除模块')
        owned.append((version, source, override))
    return owned, foreign, registered


def preflight(keep_module):
    for directory in (CONFIG, STATE, LIBEXEC):
        plain_path(directory, directory=True)
    for path in (COMMAND, UNITS / 'brutal-watch.service', UNITS / 'brutal-watch.timer'):
        plain_path(path)
    if not keep_module:
        for command in ('modinfo', 'rmmod'):
            if not shutil.which(command):
                raise ValueError('卸载缺少命令：' + command)
        return module_plan()
    return None


def try_unload():
    result = subprocess.run(['rmmod', 'brutal'], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    error = watch.short_error((result.stderr or '').strip())
    return not LOADED.exists(), error or 'rmmod 退出码 {}，模块仍在内存中'.format(result.returncode)


def start_node(container):
    try:
        run(['docker', 'start', container])
        running = subprocess.check_output(['docker', 'inspect', '-f', '{{.State.Running}}', container], text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('节点容器恢复失败，请手动启动并检查日志：{}（{}）'.format(container, watch.short_error(exc))) from exc
    if running.strip() != 'true':
        raise ValueError('节点容器未恢复运行，请手动启动并检查日志：' + container)


@contextlib.contextmanager
def unload_module():
    if not LOADED.exists():
        yield
        return
    unloaded, error = try_unload()
    if unloaded:
        yield
        return
    # Only stop the configured node; never kill arbitrary processes or force rmmod.
    try:
        cfg = watch.config_load(CONFIG / 'config.json')
    except FileNotFoundError as exc:
        raise ValueError('模块未能卸载，且配置文件已不存在，无法确定目标容器。'
                         '请检查后重试。rmmod：' + error) from exc
    container = cfg['container']
    if not shutil.which('docker'):
        raise ValueError('模块未能卸载且缺少 docker，无法检查目标节点。rmmod：' + error)
    result = subprocess.check_output(['docker', 'inspect', '-f', '{{.State.Running}}', container], text=True)
    if result.strip() not in ('true', 'false'):
        raise ValueError('无法确认目标节点运行状态，保留节点及模块。rmmod：' + error)
    was_running = result.strip() == 'true'
    if was_running:
        print('模块未能卸载，停止节点容器并中断现有连接：' + container, flush=True)
    else:
        print('目标节点已停止，等待模块释放并重试卸载：' + container, flush=True)
    try:
        if was_running:
            run(['docker', 'stop', '--time', '30', container])
        # Orphaned connections keep the congestion module referenced until they finish closing.
        deadline = time.monotonic() + UNLOAD_WAIT_SECONDS
        while True:
            unloaded, error = try_unload()
            if unloaded:
                break
            if time.monotonic() >= deadline:
                raise ValueError('节点已停止，但 brutal 模块 {} 秒内仍无法卸载。rmmod：{}'.format(
                    UNLOAD_WAIT_SECONDS, error))
            time.sleep(1)
        yield  # Keep the node stopped until DKMS files are removed too.
    except BaseException as exc:
        # Restore the node even when stop/unload fails after partially succeeding.
        if was_running:
            try:
                start_node(container)
            except ValueError as restore_error:
                raise ValueError(watch.short_error(exc) + '；' + str(restore_error)) from exc
        raise
    if was_running:
        start_node(container)


def ensure_idle():
    host = watch.Host()
    data = watch.Store(STATE / 'state.json').load(host.boot_id())
    if data['enabled'] or data['entries'] or data['error']:
        raise ValueError('仍有未关闭状态或待清理记录，保留工具，请修复后重试卸载')
    for unit in ('brutal-watch.timer', 'brutal-watch.service'):
        result = subprocess.run(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'],
                                check=True, text=True, stdout=subprocess.PIPE)
        if result.stdout.strip() not in ('inactive', 'failed'):
            raise ValueError('任务尚未停止：' + unit)
    for family in (4, 6):
        if any(str(route.get('protocol')) == str(watch.ROUTE_PROTOCOL) for route in host.routes(family)):
            raise ValueError('仍有协议 234 的路由，无法确认归属，保留工具及状态')


def remove(keep_module, unload_loaded=False):
    plan = preflight(keep_module)  # Recheck after stopping and taking the runtime lock.
    ensure_idle()
    warnings = []
    owned = []
    if not keep_module:
        owned, foreign, registered = plan
        # A bare version (or even identical build contents) does not record who
        # loaded a module. Existing installations have no trustworthy load receipt.
        # Keep recovery/configuration files so a later explicit retry can stop the node.
        if LOADED.exists() and not unload_loaded:
            print('加速已关闭；无法确认内存中 brutal 模块的安装来源，未卸载模块、未停止节点。')
            print('本次未继续删除文件。确认要移除当前已加载的 brutal 模块（包括外部安装的模块）时，'
                  '请使用 --uninstall --unload-module；仅删除 watcher 可使用 --uninstall --keep-module。')
            return 2
        if (owned or unload_loaded) and watch.Host().rules():
            raise ValueError('仍有其他 TCP Brutal 规则，保留模块；只卸载工具可使用 --keep-module')
        ctl_owned = (CTL.is_file() and not CTL.is_symlink() and any(
            (source / 'tools/brutalctl').is_file()
            and not (source / 'tools/brutalctl').is_symlink()
            and filecmp.cmp(CTL, source / 'tools/brutalctl', shallow=False)
            for _, source, _ in owned))
        unload = unload_loaded and LOADED.exists()
        with unload_module() if unload else contextlib.nullcontext():
            for version, source, override in owned:
                if version in registered:
                    run(['dkms', 'remove', '-m', 'tcp-brutal', '-v', version, '--all'])
                    if version in registered_versions():
                        raise ValueError('DKMS 记录未清除，保留源码与工具：' + version)
        disk = subprocess.run(['modinfo', '-n', 'brutal'], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
        # kmod exits 1 for every error; only "not found" proves the module is gone.
        if disk.returncode != 0 and 'not found' not in disk.stderr:
            raise ValueError('无法核对磁盘模块是否已移除：' + watch.short_error(disk.stderr))
        if foreign or disk.returncode == 0:
            warnings.append('保留了外部安装或 DKMS 恢复的 TCP Brutal 模块，请按其安装方式处理')
        if ctl_owned and not foreign and disk.returncode != 0:
            CTL.unlink()
        elif CTL.exists() or CTL.is_symlink():
            warnings.append('brutalctl 仍有其他模块使用或归属无法确认，已保留：' + str(CTL))
        # Other kernel/module versions may still invoke our build helper.
        helper = LIBEXEC / 'module-build'
        removing_overrides = {override for _, _, override in owned}
        references = any(str(helper) in p.read_text() for p in DKMS_CONFIG.rglob('*.conf')
                         if p.is_file() and p not in removing_overrides)
        if references or foreign or disk.returncode == 0:
            warnings.append('保留 DKMS 构建入口：' + str(helper))
        else:
            plain_path(helper)
            if helper.exists():
                helper.unlink()
            if LIBEXEC.exists() and not any(LIBEXEC.iterdir()):
                LIBEXEC.rmdir()
        if LOADED.exists():
            warnings.append('brutal 模块仍在内存，可能由其他服务重新加载，请检查其他使用者')

    # Do not remove recovery state or the watcher until module operations succeed.
    timer = UNITS / 'brutal-watch.timer'
    if timer.exists():
        run(['systemctl', 'disable', 'brutal-watch.timer'])
    for path in (timer, UNITS / 'brutal-watch.service'):
        if path.exists():
            path.unlink()
    run(['systemctl', 'daemon-reload'])
    # Drop a failed last tick from systemctl --failed; units already unloaded make this fail harmlessly.
    subprocess.run(['systemctl', 'reset-failed', 'brutal-watch.timer', 'brutal-watch.service'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if COMMAND.exists():
        COMMAND.unlink()
    for directory in (CONFIG, STATE):
        if directory.exists():
            shutil.rmtree(directory)
    # Keep the source marker and compiled brutalctl until module checks, node
    # restoration and tool cleanup succeed. A retry can still establish ownership
    # even after DKMS has already removed its registration/build tree.
    for _, source, override in owned:
        if source.exists():
            shutil.rmtree(source)
        if override.exists():
            override.unlink()
    print('brutal-watch 工具、配置、状态及 systemd 任务已卸载。')
    print('未改动 Compose（包括 GODEBUG=multipathtcp=0），未删除共享系统依赖。')
    if keep_module:
        print('按 --keep-module 保留模块、brutalctl 和 DKMS 构建入口。')
    for warning in warnings:
        print('待处理：' + warning)
    if warnings:
        print('卸载尚有待处理项，退出码 2；不要据此认定 TCP Brutal 已完全退出。')
        return 2
    print('所选卸载范围已完成。')
    return 0


def main():
    watch.configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'remove'))
    parser.add_argument('--keep-module', action='store_true')
    parser.add_argument('--unload-module', action='store_true', help='允许卸载当前已加载的 brutal 模块，包括外部安装的模块')
    args = parser.parse_args()
    if args.keep_module and args.unload_module:
        parser.error('--keep-module 不能与 --unload-module 一起使用')
    try:
        if args.action == 'preflight':
            preflight(args.keep_module)
            return 0
        return remove(args.keep_module, args.unload_module)
    except (OSError, ValueError, watch.WatchError, subprocess.SubprocessError) as exc:
        print('卸载失败：' + watch.short_error(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
