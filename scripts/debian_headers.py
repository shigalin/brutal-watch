#!/usr/bin/env python3
"""Recover exact Debian kernel build packages without changing APT sources or kernels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
from urllib.parse import quote

if __package__:
    from .setup import watch
else:
    from setup import watch

SNAPSHOT = 'https://snapshot.debian.org'
KEYRING = Path('/usr/share/keyrings/debian-archive-keyring.gpg')
BUILD_PREFIXES = ('linux-headers-', 'linux-kbuild-', 'linux-compiler-')
INDEX_TIMEOUT = 900
LOOKUP_BUDGET = 1800
AUTO_WAIT_BUDGET = 120
PROGRESS_INTERVAL = 30
CONFIRM_TIMEOUT = 60


class HeaderError(Exception):
    pass


def confirm_long_wait():
    try:
        with open('/dev/tty', 'rb', buffering=0) as reader, \
             open('/dev/tty', 'w', encoding='utf-8') as writer:
            if not reader.isatty():
                return False
            writer.write('归档查找已等待 {} 分钟。是否继续？本轮累计最多 {} 分钟，{} 秒未回答按否处理 [y/N]：'.format(
                AUTO_WAIT_BUDGET // 60, LOOKUP_BUDGET // 60, CONFIRM_TIMEOUT))
            writer.flush()
            deadline, answer = time.monotonic() + CONFIRM_TIMEOUT, bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([reader], [], [], remaining)[0]:
                    writer.write('\n确认超时，按否停止。\n')
                    writer.flush()
                    return False
                char = reader.read(1)
                if not char:
                    return False
                if char in (b'\n', b'\r'):
                    return answer.decode('ascii', errors='replace').strip().lower() in ('y', 'yes')
                answer.extend(char)
    except (OSError, EOFError, KeyboardInterrupt):
        return False


def redact_output(text):
    # Even a local .deb install can fetch dependencies from authenticated sources.
    return re.sub(r'\b(?:https?|ftp)://\S+', '<repository-url>', text)


def run(args, optional=False, visible=False, timeout=300, progress=None):
    env = dict(os.environ, LC_ALL='C', DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
    if visible:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding='utf-8', errors='replace', env=env) as process:
            for line in process.stdout:
                print(redact_output(line), end='', flush=True)
            result = subprocess.CompletedProcess(args, process.wait(), '', '')
    elif progress:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   encoding='utf-8', errors='replace', env=env, start_new_session=True)
        started = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    output, error = process.communicate(timeout=min(PROGRESS_INTERVAL, timeout - elapsed))
                    result = subprocess.CompletedProcess(args, process.returncode, output, error)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    if elapsed < timeout:
                        print('{}：仍在下载或验证，已等待 {} 秒'.format(progress, int(elapsed)), flush=True)
        except BaseException:
            # Stop APT acquisition methods as well as their parent before moving on.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.stdout.close()
                process.stderr.close()
            raise
    else:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding='utf-8', errors='replace', timeout=timeout, env=env)
    if result.returncode and not optional:
        if args[0] == 'apt-get' and not visible:
            print(redact_output(result.stdout + result.stderr), file=sys.stderr)
        raise HeaderError('{} 失败（退出码 {}）'.format(args[0], result.returncode))
    return result


def metadata(path, timeout=300):
    response = run(['curl', '-fsSL', '--proto', '=https', '--proto-redir', '=https',
                    '--connect-timeout', '15', '--max-time', '45', '--retry', '2', SNAPSHOT + path],
                   timeout=timeout, progress='查询 Debian 归档')
    return json.loads(response.stdout)


def installed(name):
    result = run(['dpkg-query', '-W', '-f=${db:Status-Status}\t${Version}\t${Architecture}\t${source:Package}', name], optional=True)
    fields = result.stdout.strip().split('\t')
    return fields[1:] if result.returncode == 0 and len(fields) == 4 and fields[0] == 'installed' else None


def satisfies(version, relation, required):
    return not relation or run(['dpkg', '--compare-versions', version, relation, required], optional=True).returncode == 0


def candidate(name):
    result = run(['apt-cache', 'policy', name])
    match = re.search(r'^\s*Candidate:\s*(\S+)', result.stdout, re.M)
    return match[1] if match and match[1] != '(none)' else None


def control_stanzas(text):
    fields = {}
    for line in text.splitlines() + ['']:
        if not line:
            if fields:
                yield fields
                fields = {}
        elif not line[0].isspace() and ': ' in line:
            key, value = line.split(': ', 1)
            fields[key] = value


def build_dependencies(depends):
    for group in depends.split(','):
        group = group.strip()
        if not group.startswith(BUILD_PREFIXES):
            continue
        match = re.fullmatch(r'([a-z0-9][a-z0-9+.-]+)(?:\s*\((<<|<=|=|>=|>>)\s*([^()\s]+)\))?', group)
        if not match:
            raise HeaderError('不支持的内核构建依赖表达式，请手动准备 headers')
        yield match[1], match[2], match[3]


def validate_plan(output, packages):
    """Only additions are allowed; never replace installed packages or install a kernel."""
    planned = {}
    for line in output.splitlines():
        if line.startswith('Remv '):
            raise HeaderError('headers 安装计划需要删除已有软件包，已停止')
        if not line.startswith('Inst '):
            continue
        match = re.match(r'^Inst (\S+)(?: \[([^]]+)\])? \((\S+)', line)
        if not match:
            raise HeaderError('无法解析 headers 安装计划，已停止；APT 行：' + redact_output(line))
        if match[2]:
            raise HeaderError(redact_output(
                'headers 安装计划需要替换已有软件包，已停止；包名 {}，已安装版本 {}，目标版本 {}'.format(
                    match[1], match[2], match[3])))
        name = match[1].split(':')[0]
        if name.startswith(('linux-image', 'grub', 'systemd', 'docker')):
            raise HeaderError('headers 安装计划涉及内核或系统服务，已停止')
        if name.startswith('linux-headers-') and name not in packages:
            raise HeaderError('headers 安装计划包含非目标内核的 headers，已停止')
        planned[name] = match[3]
    for name, version in packages.items():
        if planned.get(name) != version:
            raise HeaderError('headers 安装计划未采用已校验的精确包版本，已停止')
    return planned


class Recovery:
    def __init__(self, kernel, directory, codename, wait_minutes=None):
        if not re.fullmatch(r'[0-9][a-zA-Z0-9.+_-]+', kernel):
            raise HeaderError('内核版本格式无效')
        self.kernel, self.directory = kernel, Path(directory)
        if not re.fullmatch(r'[a-z][a-z0-9-]+', codename):
            raise HeaderError('无法确定 Debian 发行版代号')
        if wait_minutes is not None and (type(wait_minutes) is not int or not 1 <= wait_minutes <= 30):
            raise HeaderError('归档等待分钟数必须是 1–30 的整数')
        self.wait_budget = LOOKUP_BUDGET if wait_minutes is None else wait_minutes * 60
        self.auto_wait_budget = AUTO_WAIT_BUDGET if wait_minutes is None else self.wait_budget
        self.codename, self.indexes, self.index_errors = codename, {}, {}
        self.lookup_deadline = None
        self.lookup_started = None
        self.long_wait_confirmed = wait_minutes is not None
        self.packages, self.paths = {}, []
        self.arch = run(['dpkg', '--print-architecture']).stdout.strip()
        if self.arch not in ('amd64', 'arm64'):
            raise HeaderError('归档恢复仅支持 amd64/arm64')

    def image_version(self):
        for name in ('linux-image-' + self.kernel, 'linux-image-' + self.kernel + '-unsigned'):
            info = installed(name)
            if not info:
                continue
            version, arch, source = info
            if arch != self.arch or source not in ('linux', 'linux-signed-' + self.arch):
                raise HeaderError('当前内核包不是受支持的 Debian 官方内核来源')
            entries = self.archive_metadata('/mr/binary/{}/{}/binfiles'.format(quote(name, safe=''), quote(version, safe='')))['result']
            if not any(entry['architecture'] == arch for entry in entries):
                raise HeaderError('官方归档未确认当前内核包的版本和架构')
            return version
        raise HeaderError('找不到当前内核对应的已安装 Debian 内核包，不能自动匹配归档')

    def lookup_timeout(self, per_command, failures=()):
        if self.lookup_deadline is None:
            self.lookup_started = time.monotonic()
            self.lookup_deadline = self.lookup_started + min(self.auto_wait_budget, self.wait_budget)
        remaining = self.lookup_deadline - time.monotonic()
        if remaining <= 0:
            elapsed = time.monotonic() - self.lookup_started
            can_extend = not self.long_wait_confirmed and self.wait_budget > self.auto_wait_budget and elapsed < self.wait_budget
            if can_extend and confirm_long_wait():
                self.long_wait_confirmed = True
                # User decision time is excluded from the remaining wait budget.
                self.lookup_deadline = time.monotonic() + self.wait_budget - elapsed
                return min(per_command, self.lookup_deadline - time.monotonic())
            reasons = list(failures) + [(suite, reason) for (_, _, suite), reason in self.index_errors.items()]
            for suite, reason in dict.fromkeys(reasons):
                print('{}：{}'.format(suite, reason.strip()), file=sys.stderr)
            if can_extend:
                raise HeaderError('未获得继续长时间等待的确认，已停止；非交互执行默认不继续，尚未安装归档包')
            raise HeaderError('归档查找超过共享总时限（{} 秒），已停止；尚未安装归档包'.format(self.wait_budget))
        return min(per_command, remaining)

    def lookup_operation(self, operation, per_command, failures=()):
        remaining = per_command
        while True:
            limit = self.lookup_timeout(remaining, failures)
            started = time.monotonic()
            try:
                return operation(limit)
            except subprocess.TimeoutExpired:
                remaining -= time.monotonic() - started
                if (remaining > 0 and not self.long_wait_confirmed and self.wait_budget > self.auto_wait_budget and
                        time.monotonic() >= self.lookup_deadline):
                    self.lookup_timeout(remaining, failures)  # Require explicit consent before retrying.
                    continue
                raise

    def archive_metadata(self, path, failures=()):
        return self.lookup_operation(lambda limit: metadata(path, timeout=limit), 300, failures)

    def signed_record(self, name, version, entry):
        if not KEYRING.is_file():
            raise HeaderError('缺少 debian-archive-keyring，不能验证归档签名')
        try:
            locations = self.archive_metadata('/mr/file/' + entry['hash'] + '/info')['result']
        except subprocess.TimeoutExpired:
            self.lookup_timeout(300)  # Report the shared limit if it was exhausted.
            raise HeaderError('归档位置查询超时，尚未安装归档包') from None
        failures = []
        for location in locations:
            archive, stamp = location['archive_name'], location['first_seen']
            if archive not in ('debian', 'debian-security') or not re.fullmatch(r'\d{8}T\d{6}Z', stamp):
                continue
            suites = ([self.codename + '-security'] if archive == 'debian-security' else
                      [self.codename + '-proposed-updates', self.codename, self.codename + '-updates',
                       self.codename + '-backports', 'sid'])
            for suite in suites:
                self.lookup_timeout(INDEX_TIMEOUT, failures)
                key = (archive, stamp, suite)
                if key not in self.indexes:
                    root = self.directory / ('index-' + str(len(self.indexes)))
                    root.mkdir()
                    (root / 'lists').mkdir()
                    (root / 'empty-status').write_text('', encoding='utf-8')
                    source = root / 'snapshot.list'
                    source.write_text('deb [arch={arch} signed-by={keyring} check-valid-until=no '
                                      'allow-insecure=no allow-weak=no allow-downgrade-to-insecure=no] '
                                      '{base}/archive/{archive}/{stamp}/ {suite} main\n'.format(
                                          arch=self.arch, keyring=KEYRING, base=SNAPSHOT,
                                          archive=archive, stamp=stamp, suite=suite), encoding='utf-8')
                    options = []
                    settings = {
                        'Dir::Etc::sourcelist': str(source), 'Dir::Etc::sourceparts': '-',
                        'Dir::State::lists': str(root / 'lists'),
                        'Dir::State::status': str(root / 'empty-status'),
                        'Dir::Cache::pkgcache': '', 'Dir::Cache::srcpkgcache': '',
                        'APT::Architecture': self.arch, 'APT::Default-Release': '',
                        'Acquire::Languages': 'none', 'Acquire::PDiffs': 'false',
                        'Acquire::http::Timeout': '20', 'Acquire::https::Timeout': '20', 'Acquire::Retries': '2',
                        'Acquire::AllowInsecureRepositories': 'false', 'Acquire::AllowWeakRepositories': 'false',
                        'Acquire::AllowDowngradeToInsecureRepositories': 'false',
                        'APT::Get::AllowUnauthenticated': 'false', 'APT::Update::Error-Mode': 'any',
                    }
                    for setting, value in settings.items():
                        options += ['-o', setting + '=' + value]
                    # Fresh isolated lists; never query a failed/unsigned update.
                    print('验证 Debian 归档签名索引：{} / {} / {}'.format(archive, suite, stamp), flush=True)
                    try:
                        result = self.lookup_operation(
                            lambda limit: run(['apt-get'] + options + ['update'], optional=True,
                                              timeout=limit, progress=suite), INDEX_TIMEOUT, failures)
                        self.indexes[key] = options if result.returncode == 0 else None
                        if result.returncode:
                            self.index_errors[key] = redact_output(result.stdout + result.stderr)[-4000:]
                    except subprocess.TimeoutExpired as exc:
                        self.indexes[key] = None
                        self.index_errors[key] = '索引更新超过 {:.0f} 秒，已终止本次获取'.format(exc.timeout)
                options = self.indexes[key]
                if options is None:
                    failures.append((suite, self.index_errors[key]))
                    continue
                try:
                    result = self.lookup_operation(
                        lambda limit: run(['apt-cache'] + options + ['show', name + '=' + version], optional=True,
                                          timeout=limit), 300, failures)
                except subprocess.TimeoutExpired:
                    failures.append((suite, '包记录查询超时'))
                    continue
                if result.returncode:
                    failures.append((suite, redact_output(result.stdout + result.stderr)[-4000:]))
                    continue
                records = [record for record in control_stanzas(result.stdout)
                           if record.get('Package') == name and record.get('Version') == version and
                           record.get('Architecture') == entry['architecture']]
                if not records:
                    failures.append((suite, '已验证索引中没有目标包的精确版本和架构'))
                    continue
                if len(records) != 1 or not re.fullmatch(r'[0-9a-f]{64}', records[0].get('SHA256', '')):
                    raise HeaderError('已签名索引缺少唯一的 SHA256 包记录，已停止')
                return records[0]
        for suite, reason in failures:
            print('{}：{}'.format(suite, reason.strip()), file=sys.stderr)
        raise HeaderError('未找到通过 Debian 签名验证的精确包索引；请检查归档可达性和 debian-archive-keyring')

    def download(self, name, version):
        entries = self.archive_metadata('/mr/binary/{}/{}/binfiles'.format(quote(name, safe=''), quote(version, safe='')))['result']
        entries = [entry for entry in entries if entry['architecture'] in (self.arch, 'all')]
        if len(entries) != 1 or not re.fullmatch(r'[0-9a-f]{40}', entries[0]['hash']):
            raise HeaderError('归档包架构或文件标识不明确，已停止')
        entry = entries[0]
        record = self.signed_record(name, version, entry)
        path = self.directory / (entry['hash'] + '.deb')
        self.lookup_operation(lambda limit: run(
            ['curl', '-fsSL', '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '15',
             '--max-time', '120', '--retry', '2', SNAPSHOT + '/file/' + entry['hash'], '-o', str(path)],
            timeout=limit, progress='下载归档包 ' + name), 300)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != record['SHA256'] or path.stat().st_size != int(record.get('Size', '-1')):
            raise HeaderError('归档包文件校验失败')
        # Single-field queries keep multiline descriptions out of control parsing.
        control = {field: run(['dpkg-deb', '-f', str(path), field]).stdout.strip()
                   for field in ('Package', 'Version', 'Architecture', 'Source', 'Depends', 'Pre-Depends')}
        source = re.fullmatch(r'linux(?: \(([^)]+)\))?', control['Source'])
        if (control['Package'] != name or control['Version'] != version or
                control['Architecture'] != entry['architecture'] or not source):
            raise HeaderError('归档包的名称、版本、架构或源码来源不匹配')
        path.chmod(0o644)
        self.packages[name] = version
        self.paths.append(str(path))
        return control, source[1] or version

    def collect(self):
        version = self.image_version()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.directory.chmod(0o755)
        name = 'linux-headers-' + self.kernel
        if installed(name):
            raise HeaderError('精确 headers 包已安装但构建目录缺失，请修复原包')
        control, source_version = self.download(name, version)
        records = self.archive_metadata('/mr/package/linux/{}/binpackages'.format(quote(source_version, safe='')))['result']
        queue = list(build_dependencies(control['Depends'] + ',' + control['Pre-Depends']))
        while queue:
            name, relation, required = queue.pop(0)
            if name in self.packages:
                if not satisfies(self.packages[name], relation, required):
                    raise HeaderError('内核构建包版本约束冲突')
                continue
            info = installed(name)
            if info:
                if not satisfies(info[0], relation, required):
                    raise HeaderError('现有构建包不满足版本要求；不会自动替换已有包')
                continue
            available = candidate(name)
            if available and satisfies(available, relation, required):
                continue  # Let current APT sources provide compatible dependencies.
            versions = sorted({record['version'] for record in records if record['name'] == name and
                               satisfies(record['version'], relation, required)})
            if len(versions) != 1 or len(self.packages) >= 8:
                raise HeaderError('无法唯一匹配同源码版本的内核构建依赖，请手动准备 headers')
            control, child_source = self.download(name, versions[0])
            if child_source != source_version:
                raise HeaderError('归档构建依赖的源码版本与目标 headers 不一致')
            queue.extend(build_dependencies(control['Depends'] + ',' + control['Pre-Depends']))

    def install(self):
        args = ['apt-get', '--no-remove', '--no-install-recommends', 'install'] + self.paths
        plan = run(args[:1] + ['-s'] + args[1:])
        additions = validate_plan(plan.stdout, self.packages)
        # Check dpkg as well as APT's printed plan, including same-version reinstalls.
        if any(installed(name) for name in additions):
            raise HeaderError('安装计划包含已安装的软件包，已停止')
        print('Debian 精确 headers 归档安装计划：' + ', '.join(sorted(additions)), flush=True)
        run(args[:1] + ['-y'] + args[1:], visible=True)
        for name, version in self.packages.items():
            info = installed(name)
            if not info or info[0] != version:
                raise HeaderError('归档包安装后的版本验证失败')


def main():
    watch.configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel')
    parser.add_argument('directory')
    parser.add_argument('--archive-wait-minutes', type=int, choices=range(1, 31),
                        help='预授权本次归档查找的等待分钟数，不再交互确认')
    args = parser.parse_args()
    stage = '确认 Debian 官方内核与归档包'
    try:
        release = Path('/etc/os-release').read_text(encoding='utf-8')
        if not re.search(r'''^ID=(?:debian|"debian"|'debian')$''', release, re.M):
            raise HeaderError('归档恢复只适用于 Debian')
        if args.kernel != os.uname().release:
            raise HeaderError('只允许恢复当前运行内核的 headers')
        codename = re.search(r'''^VERSION_CODENAME=["']?([a-z][a-z0-9-]+)["']?$''', release, re.M)
        if not codename:
            raise HeaderError('无法确定 Debian 发行版代号')
        recovery = Recovery(args.kernel, args.directory, codename[1], args.archive_wait_minutes)
        recovery.collect()
        stage = '检查并安装精确 headers'
        recovery.install()
        print('精确 headers 归档安装完成')
    except (HeaderError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, HeaderError) else type(exc).__name__
        print(stage + '失败：' + detail, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
