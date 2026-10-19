"""Production systemd/cgroup-v2 adapter: fixed runner и проверенные OS facts.

Здесь нет shell, sudo, arbitrary destination write или управления чужим unit.
Все subprocess имеют deadlines/output caps и своего reap owner. Файлы ядра
читаются с cap, O_NONBLOCK/O_NOFOLLOW; enumeration ограничен TasksMax.
"""
import asyncio
import ctypes
import errno
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import signal
import socket
import stat
import time

from mop.node.host_scope import Facts, Installed, Refused, Scope, StartUnknown, Unknown, unique

CGROUP_ROOT = Path('/sys/fs/cgroup')
TASKS_MAX = 128
TOOLS = ('/usr/bin/systemd-run', '/usr/bin/systemctl', '/usr/bin/busctl')
COMMAND_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
               'SYSTEMD_COLORS': '0', 'SYSTEMD_PAGER': 'cat'}


class CommandFailure(Unknown):
    """Raw diagnostic отдельно: root socket никогда не отражает его в ответ."""
    def __init__(self, code, stderr):
        super().__init__('systemd command failed (exit '+str(code)+'); outcome unknown')
        self.stderr = stderr
        self.code = code


class BoundedCommand:
    """Ограничены stdout+stderr вместе, время и ownership subprocess cleanup."""
    def __init__(self, *, timeout=3., limit=65536):
        if not .05 <= timeout <= 300 or type(limit) is not int or not 1024 <= limit <= 262144:
            raise Refused('bounded systemd command required')
        self.timeout, self.limit = timeout, limit
        self.active = set()
        self._closed = False
        self._cleanup_error = None

    async def _reap(self, creation):
        process = await creation
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # Полный StreamReader уже pause_reading: wait() ждёт и EOF pipes.
        # Сначала закрываем bounded pipe transports, потом reap, иначе deadlock.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream._transport.close()
        await process.wait()

    def _done(self, task):
        self.active.discard(task)
        if task.cancelled():
            self._cleanup_error = 'systemd subprocess cleanup cancelled; outcome unknown'
        elif task.exception() is not None:
            self._cleanup_error = 'systemd subprocess cleanup failed: '+type(task.exception()).__name__

    async def run(self, executable, *args):
        if self._closed:
            raise Unknown('systemd command owner closed')
        creation = asyncio.create_task(asyncio.create_subprocess_exec(executable, *args,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=COMMAND_ENV, close_fds=True,
            start_new_session=True, limit=8192))
        readers = []
        try:
            async with asyncio.timeout(self.timeout):
                process = await asyncio.shield(creation)
                size = 0
                async def capture(stream):
                    nonlocal size
                    chunks = []
                    while True:
                        chunk = await stream.read(4096)
                        if not chunk:
                            return b''.join(chunks)
                        size += len(chunk)
                        if size > self.limit:
                            raise Unknown('systemd output exceeds bounded capture limit')
                        chunks.append(chunk)
                readers = [asyncio.create_task(capture(s)) for s in (process.stdout, process.stderr)]
                stdout, stderr = await asyncio.gather(*readers)
                code = await process.wait()
                if code:
                    raise CommandFailure(code, stderr)
                return stdout
        except TimeoutError as error:
            raise Unknown('systemd command deadline expired; outcome unknown') from error
        except OSError as error:
            raise Unknown('installed systemd command unavailable: ' + type(error).__name__) from error
        finally:
            for task in readers:
                if not task.done():
                    task.cancel()
            cleanup = asyncio.create_task(self._reap(creation))
            self.active.add(cleanup)
            cleanup.add_done_callback(self._done)
            # Cancellation не отменяет единственного владельца kill/reap.
            done, _ = await asyncio.wait({cleanup}, timeout=.5)
            for task in done:
                if not task.cancelled() and task.exception():
                    # Creation exception уже идёт к caller; raw stderr не отражается.
                    task.exception()

    async def aclose(self):
        self._closed = True
        if self.active:
            _, pending = await asyncio.wait(self.active, timeout=1.)
            if pending:
                raise Unknown('systemd subprocess cleanup incomplete; outcome unknown')
        if self._cleanup_error:
            raise Unknown(self._cleanup_error)


def open_directory(path, *, owner=None, private=False, immutable=False):
    """No-follow walk; production immutable запрещает writable ancestors."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('absolute no-follow installed directory required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if immutable and (info.st_uid != 0 or info.st_mode & 0o022):
                raise Refused('root-owned immutable installed ancestors required')
        info = os.fstat(fd)
        if owner is not None and info.st_uid != owner:
            raise Refused('installed directory owner mismatch')
        if info.st_mode & (0o077 if private else 0o022):
            raise Refused('installed directory permissions mismatch')
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def read_bytes_at(directory, name, *, limit=16384):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused('regular bounded kernel fact required')
        data = os.read(fd, limit+1)
        if len(data) > limit:
            raise Unknown('kernel fact exceeds bounded read limit')
        return data
    finally:
        os.close(fd)


def read_at(directory, name, *, limit=16384):
    try:
        return read_bytes_at(directory, name, limit=limit).decode('ascii').strip()
    except UnicodeError as error:
        raise Refused('invalid kernel fact encoding') from error


def parse_proc_start(raw, pid):
    """comm между скобками — opaque bytes, proof только PID/starttime ядра."""
    if (type(raw) is not bytes or not 0 < len(raw) <= 16384
            or type(pid) is not int or not 0 < pid < 2**31):
        raise Refused('bounded process stat and numeric PID required')
    head, opening, record = raw.partition(b' (')
    _, closing, tail = record.rpartition(b')')
    fields = tail.split()
    if (head != str(pid).encode('ascii') or not opening or not closing or len(fields) < 20
            or not fields[19].isdigit() or len(fields[19]) > 20 or int(fields[19]) >= 2**64):
        raise Refused('numeric PID-bound process starttime required')
    return int(fields[19])


def parse_proc_status(raw):
    """Декодируем только authority fields; Name и прочий текст не authority."""
    if type(raw) is not bytes or not 0 < len(raw) <= 16384:
        raise Refused('bounded process status required')
    required = {b'Uid', b'Gid', b'Groups', b'NoNewPrivs', b'CapInh', b'CapPrm', b'CapEff', b'CapAmb'}
    values = {}
    try:
        for line in raw.split(b'\n'):
            key, separator, value = line.partition(b':')
            if key in required:
                name = key.decode('ascii')
                if not separator or name in values:
                    raise Refused('duplicate or malformed process authority field')
                values[name] = value.decode('ascii').strip()
    except UnicodeError as error:
        raise Refused('invalid process authority field encoding') from error
    if len(values) != len(required):
        raise Refused('incomplete process authority fields')
    return values


def read_kernel(path, limit=16384):
    fd = open_directory(Path(path).parent)
    try:
        return read_at(fd, Path(path).name, limit=limit)
    finally:
        os.close(fd)


def installed_file(path):
    fd = open_directory(path.parent, owner=0, immutable=True)
    try:
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(child)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or not info.st_mode & 0o111 or info.st_mode & 0o6000):
                raise Refused('root-installed non-writable executable required')
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        finally:
            os.close(child)
    finally:
        os.close(fd)


def properties(installed, scope):
    body = str(installed.body(scope))
    return {
        'User': str(installed.body_uid), 'Group': str(installed.body_gid),
        'Type': 'exec', 'RemainAfterExit': 'yes', 'Slice': 'mop-host.slice',
        'NoNewPrivileges': 'yes', 'Delegate': 'no', 'ProtectControlGroups': 'yes',
        'CapabilityBoundingSet': '', 'AmbientCapabilities': '', 'SupplementaryGroups': '',
        'RestrictSUIDSGID': 'yes', 'RestrictNamespaces': 'yes', 'ProtectSystem': 'strict',
        'ProtectHome': 'yes', 'PrivateTmp': 'yes', 'PrivateDevices': 'yes',
        'ProtectKernelTunables': 'yes', 'ProtectKernelModules': 'yes', 'ProtectKernelLogs': 'yes',
        'LockPersonality': 'yes', 'RestrictRealtime': 'yes',
        'KillMode': 'control-group', 'SendSIGKILL': 'yes', 'Restart': 'no',
        'TasksMax': str(TASKS_MAX), 'UMask': '0077', 'WorkingDirectory': body,
        'ReadWritePaths': body,
        'InaccessiblePaths': ' '.join((str(installed.node_private), '-/run/user',
                                     '-/run/dbus', '-/run/systemd/private')),
        # Никакого host HOME, node.env, bearer или caller environment.
        'Environment': 'HOME='+body+' PATH=/usr/bin:/bin LANG=C.UTF-8',
        'StandardInput': 'null', 'StandardOutput': 'null', 'StandardError': 'null',
    }


def removed_cgroup_empty(before, after, fdpath, scope):
    """cgroup-v2 rmdir разрешён ядром лишь пустому subtree; не name absence.

    Kernfs сохраняет nlink=2: нужен exact d_path deleted именно pinned fd.
    Root ранее проверил cgroup-v2 mount, invocation и stable directory inode.
    """
    return (stat.S_ISDIR(before.st_mode) and stat.S_ISDIR(after.st_mode)
        and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        and fdpath == '/sys/fs/cgroup'+scope.cgroup+' (deleted)')


@dataclass(frozen=True)
class _PinnedCgroup:
    facts: Facts
    fd: int
    identity: object


@dataclass(frozen=True)
class _CleanupScope:
    original: Scope

    @property
    def unit(self): return self.original.unit.replace('mop-host-', 'mop-clean-', 1)
    @property
    def cgroup(self): return '/mop.slice/mop-host.slice/'+self.unit
    @property
    def launch_id(self): return self.original.launch_id


def cleanup_argv(installed, scope, limits):
    props = properties(installed, scope)
    props.update(RuntimeMaxSec='8s', TimeoutStopSec='2s')
    if limits is not None:
        props.update(limits.properties())
        props['TasksMax'] = str(limits.tasks_max)
    return (TOOLS[0], '--quiet', '--unit='+_CleanupScope(scope).unit,
        *('--property='+key+'='+value for key, value in props.items()),
        '--', str(installed.runner), '--cleanup', scope.unit)


def launch_argv(installed, scope):
    return (TOOLS[0], '--quiet', '--unit='+scope.unit,
            *('--property='+k+'='+v for k, v in properties(installed, scope).items()),
            '--', str(installed.runner), '--scope', scope.unit)


def parse_properties(raw, names):
    if type(raw) is not bytes or len(raw) > 65536:
        raise Refused('bounded unit properties required')
    try:
        result = {}
        for line in raw.decode('ascii').splitlines():
            key, separator, value = line.partition('=')
            if not separator or key not in names or key in result:
                raise Refused('duplicate or unrequested unit property')
            result[key] = value
        if set(result) != set(names):
            raise Refused('incomplete unit properties: '+', '.join(sorted(set(names)-set(result))))
        return result
    except UnicodeError as error:
        raise Refused('invalid unit property encoding') from error


def _parse_exec_details(raw, extended):
    signature = 'a(sasasttttuii)' if extended else 'a(sasbttttuii)'
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if (type(value) is not dict or set(value) != {'type', 'data'}
                or value['type'] != signature or type(value['data']) is not list
                or len(value['data']) != 1):
            raise Refused('single installed ExecStart required')
        row = value['data'][0]
        if (type(row) is not list or len(row) != 10 or type(row[0]) is not str
                or type(row[1]) is not list or not all(type(v) is str for v in row[1])
                or (row[2] != [] if extended else row[2] is not False)
                or not all(type(v) is int for v in row[3:])):
            raise Refused('exact ExecStart shape required')
        return row[0], tuple(row[1]), (row[8], row[9])
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refused('invalid structured ExecStart') from error


def _parse_exec(raw, extended):
    return _parse_exec_details(raw, extended)[:2]


def parse_exec(raw):
    return _parse_exec(raw, False)


def parse_exec_ex(raw):
    return _parse_exec(raw, True)


EXTRA_PROPERTIES = {
    'Id', 'InvocationID', 'ControlGroup', 'LoadState', 'ActiveState', 'SubState', 'MainPID',
    'Transient', 'FragmentPath', 'PassEnvironment', 'RootDirectory', 'RootImage',
    'BindPaths', 'BindReadOnlyPaths', 'DynamicUser', 'PrivateUsers',
}
EMPTY_PROPERTIES = {'PassEnvironment', 'RootDirectory', 'RootImage', 'BindPaths', 'BindReadOnlyPaths'}
STRUCTURED_EMPTY = dict.fromkeys(('ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost',
                                 'ExecCondition'), 'a(sasbttttuii)')
STRUCTURED_EMPTY['EnvironmentFiles'] = 'a(sb)'


def parse_empty(raw, signature):
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refused('invalid structured empty unit property') from error
    if (type(value) is not dict or set(value) != {'type', 'data'}
            or value['type'] != signature or value['data'] != []):
        raise Refused('unexpected unit exec hook or environment file')


def _paths(value):
    return tuple(sorted(v.removeprefix('-') for v in value.split()))


class _NativeSocket:
    """Original no-follow inode pin и peer без registry/key или protocol payload.

    PrivateUsers=no и отсутствие PID namespace в installed unit дают общий
    PID/user domain с root. Mount namespace иной (ProtectSystem/PrivateTmp):
    сверяем тот же fixed inode через pinned /proc/native/root, не setns и не
    предположение о равенстве mount namespaces. Никакого caller path.
    """
    def __init__(self, installed, reference, native):
        self.installed, self.reference, self.native = installed, reference, native
        self.body = installed.body(reference.scope)
        self.home = self.runtime = self.socket = None
        self.failed = False
        try:
            self.home = open_directory(self.body, owner=installed.body_uid, private=True)
            self.runtime = os.open('r', os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=self.home)
            for fd in (self.home, self.runtime):
                info = os.fstat(fd)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (installed.body_uid, installed.body_gid, 0o700):
                    raise Refused('native socket requires exact private installed body directories')
            self.socket = os.open('session.sock', os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=self.runtime)
            self.identities = tuple(self._identity(os.fstat(fd)) for fd in (self.home, self.runtime, self.socket))
            self.check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink

    @property
    def inode(self):
        return list(self.identities[2][:2])

    def check(self):
        from mop.node.host_backend import Process
        if self.failed:
            raise Refused('original native socket proof invalidated; replacement forbidden')
        home = open_directory(self.body, owner=self.installed.body_uid, private=True)
        try:
            observed = (os.fstat(home), os.stat('r', dir_fd=home, follow_symlinks=False),
                        os.stat('session.sock', dir_fd=self.runtime, follow_symlinks=False))
            if tuple(self._identity(info) for info in observed) != self.identities:
                raise Refused('original native socket or private directory inode changed')
            info = os.fstat(self.socket)
            if (self._identity(info) != self.identities[2] or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != self.installed.body_uid or info.st_gid != self.installed.body_gid
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise Refused('native socket type/private installed ownership changed')
            proc = open_directory(Path('/proc')/str(self.native.pid))
            view = None
            try:
                if read_at(proc, 'cgroup', limit=4096) != '0::'+self.reference.scope.cgroup:
                    raise Refused('native socket peer left exact retained cgroup')
                # Единственный deliberate proc magic-link: root этого exact PID.
                view = os.open('root', os.O_PATH|os.O_DIRECTORY|os.O_CLOEXEC, dir_fd=proc)
                for part in (self.body/'r').parts[1:]:
                    child = os.open(part, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=view)
                    os.close(view)
                    view = child
                if (self._identity(os.fstat(view)) != self.identities[1]
                        or self._identity(os.stat('session.sock', dir_fd=view, follow_symlinks=False)) != self.identities[2]):
                    raise Refused('native mount view differs from fixed root socket inode')
            finally:
                if view is not None: os.close(view)
                os.close(proc)
            if Process.read(self.native.pid) != self.native:
                raise Refused('native socket peer PID/start/boot changed')
        finally:
            os.close(home)

    async def observe(self):
        from mop.node.host_scope_supervisor import peer_credentials
        self.check()
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.setblocking(False)
        try:
            # Только credential handshake: нет native key, request или bearer.
            # Dirfd anchor не позволяет path walk уйти через replaced runtime.
            async with asyncio.timeout(.25):
                await asyncio.get_running_loop().sock_connect(peer, '/proc/self/fd/'+str(self.runtime)+'/session.sock')
            self.check()
            if peer_credentials(peer) != (self.native.pid, self.installed.body_uid, self.installed.body_gid):
                raise Refused('native socket peer differs from exact retained native PID/UID/GID')
        finally:
            peer.close()

    def close(self):
        for name in ('socket', 'runtime', 'home'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


class SystemdCgroupAdapter:
    """Production binding требует actual root/systemd/unified writable cgroup2.

    Конструктор не рекламирует capability. Installer/acceptance и composition
    root — отдельная задача; existing NativePhysicalSessions не меняется.
    """
    def __init__(self, installed: Installed):
        if type(installed) is not Installed or os.geteuid() != 0:
            raise Refused('installed root HOST supervisor required')
        self.installed = installed
        self._phase_limits = {}
        self._phase_cgroups = {}
        self._native_sockets = {}
        self.command = BoundedCommand()
        self._runner_identity = installed_file(installed.runner)
        for path in TOOLS:
            installed_file(Path(path))
        for path in (installed.body_root, Path('/run/systemd/system')):
            fd = open_directory(path, owner=0, immutable=True)
            os.close(fd)
        fd = open_directory(installed.node_private.parent, owner=0, immutable=True)
        os.close(fd)
        fd = open_directory(installed.node_private, owner=installed.node_uid, private=True)
        os.close(fd)
        self._check_cgroup_root()
        self.boot_id = read_kernel('/proc/sys/kernel/random/boot_id', 64)

    def _check_cgroup_root(self):
        fd = open_directory(CGROUP_ROOT, owner=0, immutable=True)
        try:
            buffer = ctypes.create_string_buffer(256)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.fstatfs(fd, ctypes.byref(buffer)) or ctypes.c_long.from_buffer(buffer).value != 0x63677270:
                raise Refused('unified cgroup-v2 filesystem required')
            if os.fstatvfs(fd).f_flag & os.ST_RDONLY:
                raise Refused('private writable cgroup-v2 containment required')
        finally:
            os.close(fd)

    def _runner(self):
        if installed_file(self.installed.runner) != self._runner_identity:
            raise Refused('installed HOST runner identity changed')

    def _body(self, scope):
        self._runner()
        parent = open_directory(self.installed.body_root, owner=0, immutable=True)
        try:
            fd = os.open(scope.launch_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=parent)
            try:
                info = os.fstat(fd)
                if (info.st_uid != self.installed.body_uid or info.st_gid != self.installed.body_gid
                        or info.st_mode & 0o077):
                    raise Refused('explicit private body consumer directory required')
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def configure_phase(self, scope, limits):
        from mop.node.host_phase_manifest import ResourceLimits
        if type(scope) is not Scope or type(limits) is not ResourceLimits or scope in self._phase_limits:
            raise Refused('one installed resource projection per new HOST scope required')
        self._phase_limits[scope] = limits

    def retain_phase_cgroup(self, facts):
        reference = facts.verify(self.installed, facts.scope).reference
        owned = getattr(self, '_phase_cgroups', None)
        if owned is None or reference in owned:
            raise Refused('one retained cgroup descriptor per physical phase start required')
        fd = self._cgroup(facts.scope)
        try:
            owned[reference] = _PinnedCgroup(facts, fd, os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise

    def _properties(self, scope):
        result = properties(self.installed, scope)
        limits = getattr(self, '_phase_limits', {}).get(scope)
        if limits is not None:
            result['TasksMax'] = str(limits.tasks_max)
        return result

    async def resource_readback(self, scope):
        limits = getattr(self, '_phase_limits', {}).get(scope)
        if limits is None:
            raise Refused('HOST scope has no retained installed capacity projection')
        names = ('MemoryMax', 'TasksMax', 'CPUWeight', 'CPUQuotaPerSecUSec', 'CPUQuotaPeriodUSec')
        values = parse_properties(await self.command.run(TOOLS[1], 'show', '--all', '--no-pager',
            '--property='+','.join(names), '--', scope.unit), names)
        from mop.node.host_phase_manifest import duration_usec
        if (values['MemoryMax'] != str(limits.memory_max) or values['TasksMax'] != str(limits.tasks_max)
                or values['CPUWeight'] != str(limits.cpu_weight)
                or duration_usec(values['CPUQuotaPerSecUSec']) != limits.cpu_quota_usec
                or duration_usec(values['CPUQuotaPeriodUSec']) != 100000):
            raise Refused('actual HOST manager resource limits differ from installed capacity')
        fd = self._cgroup(scope)
        try:
            if (read_at(fd, 'memory.max') != str(limits.memory_max)
                    or read_at(fd, 'pids.max') != str(limits.tasks_max)
                    or read_at(fd, 'cpu.weight') != str(limits.cpu_weight)
                    or read_at(fd, 'cpu.max') != str(max(1000, limits.cpu_quota_usec//10))+' 100000'):
                raise Refused('actual HOST kernel resource limits differ from installed capacity')
        finally:
            os.close(fd)
        return limits

    async def runner_process(self, reference, *, singleton=False):
        from mop.node.host_backend import Process
        facts = await self.inspect(reference.scope)
        if facts is None:
            raise Unknown('planned HOST runner has no physical unit')
        facts.verify(self.installed, reference.scope, reference)
        result = await self._unit(reference.scope)
        if result is None or result[0]['InvocationID'] != reference.invocation_id:
            raise Refused('planned runner invocation changed')
        pid = int(result[0]['MainPID'])
        if pid <= 0:
            raise Unknown('planned runner has no live MainPID')
        process = Process.read(pid)
        if process.boot_id != reference.boot_id:
            raise Refused('planned runner boot changed')
        fd = self._cgroup(reference.scope)
        try:
            members = read_at(fd, 'cgroup.procs').splitlines()
            if str(pid) not in members or singleton and members != [str(pid)]:
                raise Unknown('planned phase helpers are not quiescent in the exact cgroup')
        finally:
            os.close(fd)
        if Process.read(pid) != process:
            raise Unknown('planned runner MainPID/start changed')
        return process

    async def native_attachment(self, reference, report, identity, tools, pins):
        """Root повторно доказывает native/tmux в exact retained body cgroup."""
        from mop.common.producer import ProducerReport
        if type(report) is not ProducerReport:
            raise Refused('typed installed body native report required')
        from mop.node.host_backend import Process
        boot, ticks = report.process_start.split(':')
        native = Process(report.pid, int(ticks), boot)
        claimed = identity['socket_inode']
        if type(claimed) is not list or len(claimed) != 2 or any(type(v) is not int or v < 0 for v in claimed):
            raise Refused('exact body-observed socket inode required')
        launched = dict(pid=report.pid, process_start=report.process_start,
            session_id=report.scope.session_id, tmux_pid=identity['tmux_pid'], tmux_start=identity['tmux_start'])
        pinned = self._native_sockets.get(reference)
        try:
            if pinned is not None:
                if pinned.native != native:
                    raise Refused('original native socket cannot attach a replacement process')
                pinned.check()
            result = await self.native_launch(reference, launched, tools, pins)
            if pinned is None:
                pinned = _NativeSocket(self.installed, reference, native)
                self._native_sockets[reference] = pinned
            if claimed != pinned.inode:
                raise Refused('body-observed socket inode differs from independent root observation')
            await pinned.observe()
            if await self.native_launch(reference, launched, tools, pins) != result:
                raise Refused('native process proof changed around socket observation')
            await pinned.observe()
            result['socket_inode'] = pinned.inode
            return result
        except BaseException as error:
            if pinned is not None:
                pinned.failed = True
            if isinstance(error, OSError):
                raise Unknown('fixed native socket evidence unavailable: '+type(error).__name__) from error
            raise

    async def native_launch(self, reference, launched, tools, pins):
        """До registry/ACK удерживаем только реальные процессы; это не producer proof."""
        import json
        from datetime import datetime, timezone
        from mop.node.host_backend import Process
        runner = await self.runner_process(reference)
        # Installed PrivateUsers=no, без PrivatePIDs/RootDirectory/RootImage:
        # SO_PEERCRED и body proc PID/UID читаются в том же domain, не переводятся
        # по body JSON. Иные domains не поддерживаются этим installed contract.
        for namespace in ('pid', 'user'):
            if os.readlink('/proc/'+str(runner.pid)+'/ns/'+namespace) != os.readlink('/proc/self/ns/'+namespace):
                raise Refused('installed body PID/user domain differs from root supervisor')
        boot, ticks = launched['process_start'].split(':')
        native = Process(launched['pid'], int(ticks), boot)
        tmux = Process(launched['tmux_pid'], int(launched['tmux_start']), reference.boot_id)
        if native.boot_id != reference.boot_id or len({runner.pid, native.pid, tmux.pid}) != 3:
            raise Refused('actual native/tmux PID/start/boot changed')
        for name, process in (('native', native), ('tmux', tmux)):
            try:
                current = Process.read(process.pid)
            except OSError as error:
                raise Unknown('actual '+name+' PID/start unavailable before root proof: '+type(error).__name__) from error
            if current != process:
                raise Refused('actual native/tmux PID/start/boot changed')
        body = self.installed.body(reference.scope)
        runtime = body/'r'
        facts = {}
        for name, process in (('native', native), ('tmux', tmux)):
            fd, step = None, 'proc directory'
            try:
                fd = open_directory(Path('/proc')/str(process.pid))
                step = 'status'
                status = parse_proc_status(read_bytes_at(fd, 'status', limit=16384))
                if (status['Uid'].split() != [str(self.installed.body_uid)]*4
                        or status['Gid'].split() != [str(self.installed.body_gid)]*4):
                    raise Refused('actual native/tmux UID/GID changed')
                step = 'executable'
                executable = os.readlink('exe', dir_fd=fd)
                if executable != str(tools[name]):
                    raise Refused('actual native/tmux executable differs from installed content')
                step = 'cgroup'
                cgroup = read_at(fd, 'cgroup', limit=4096)
                if cgroup.strip() != '0::'+reference.scope.cgroup:
                    raise Refused('actual native/tmux is outside exact retained cgroup')
                step = 'working directory'
                if name == 'native' and os.readlink('cwd', dir_fd=fd) != str(body/'repo'):
                    raise Refused('actual native cwd differs from fixed clone')
                for namespace in ('mnt', 'pid', 'user'):
                    step = 'namespace '+namespace
                    if os.readlink('ns/'+namespace, dir_fd=fd) != os.readlink('/proc/'+str(runner.pid)+'/ns/'+namespace):
                        raise Refused('actual native/tmux namespace differs from body runner')
                step = 'argv'
                raw = read_bytes_at(fd, 'cmdline', limit=16384)
                if not raw.endswith(b'\x00'):
                    raise Refused('bounded actual native/tmux argv required')
                argv = tuple(part.decode('utf-8') for part in raw[:-1].split(b'\x00'))
                facts[name] = dict(process=process.wire(), executable=executable, argv=list(argv))
            except OSError as error:
                raise Unknown('actual '+name+' '+step+' unavailable: '+type(error).__name__) from error
            finally:
                if fd is not None:
                    os.close(fd)
        argv = facts['native']['argv']
        expected = [str(tools['native']), '--model', pins.bootstrap_model, '--session-id', launched['session_id'],
            '--messaging-socket-path', str(runtime/'session.sock'), '--permission-mode', 'default',
            '--settings', str(runtime/'settings.json'), '--setting-sources', 'user', '--strict-mcp-config', '--mcp-config']
        if len(argv) != len(expected)+1 or argv[:-1] != expected:
            raise Refused('actual native argv differs from fixed approved initial launch')
        try:
            mcp = json.loads(argv[-1])
            entry = mcp['mcpServers']['mop']
            args = entry['args']
            deadline = datetime.fromisoformat(args[-1])
            if deadline.tzinfo is not timezone.utc or deadline.isoformat() != args[-1]:
                raise ValueError('canonical original UTC launcher deadline required')
            entrypoint = self.installed.runner.parents[2]/'mop/client/body_entry.py'
            exact_mcp = dict(mcpServers=dict(mop=dict(type='stdio', command=str(tools['python']),
                args=['-I', '-S', '-B', str(entrypoint), '--scope', reference.scope.unit, '--deadline', deadline.isoformat()])))
            if mcp != exact_mcp:
                raise ValueError('fixed MCP launch required')
        except (KeyError, IndexError, TypeError, ValueError):
            raise Refused('actual native MCP argv differs from fixed isolated body entry') from None
        fd = self._cgroup(reference.scope)
        try:
            members = read_at(fd, 'cgroup.procs').splitlines()
            if any(str(p.pid) not in members for p in (runner, native, tmux)):
                raise Refused('actual native attachment is not in retained cgroup')
        finally:
            os.close(fd)
        try:
            changed = any(Process.read(p.pid) != p for p in (runner, native, tmux))
        except OSError as error:
            raise Unknown('actual attachment PID/start disappeared during root proof: '+type(error).__name__) from error
        if changed or await self.runner_process(reference) != runner:
            raise Refused('actual native attachment changed during root proof')
        return dict(reference=reference.wire(), runner=runner.wire(), **facts,
            session_id=launched['session_id'])

    async def cleanup_workspace(self, reference, connection, python):
        # Никакого root walk/rmtree. Fixed cleanup runner ждёт root на том же socket.
        from mop.node.host_backend import Process
        from mop.node.host_scope import decode, encode, invocation
        from mop.node.host_scope_supervisor import peer_credentials, read_frame
        from mop.node.host_phases import require_frame
        scope, cleanup = reference.scope, _CleanupScope(reference.scope)
        limits = getattr(self, '_phase_limits', {}).get(scope)
        if limits is None:
            raise Refused('cleanup-only broker requires retained installed resource limits')
        self._phase_limits[cleanup] = limits
        if await self._unit(cleanup) is not None:
            raise Unknown('cleanup-only unit collision; no foreign adoption or stop')
        self._runner()
        writer, fd = None, None
        try:
            async with asyncio.timeout(10.):
                await self.command.run(*cleanup_argv(self.installed, scope, limits))
                reader, writer, peer = await asyncio.shield(connection)
                observed = await self._unit(cleanup)
                if observed is None:
                    raise Unknown('cleanup-only unit vanished before physical ownership proof')
                values, executable, argv = observed
                invocation(values['InvocationID'])
                process = Process.read(peer[0])
                if (peer[1:] != (self.installed.body_uid, self.installed.body_gid)
                        or int(values['MainPID']) != process.pid or process.boot_id != reference.boot_id
                        or values['ActiveState'] != 'active' or values['SubState'] != 'running'
                        or executable != str(self.installed.runner)
                        or argv != (str(self.installed.runner), '--cleanup', scope.unit)):
                    raise Refused('cleanup-only actual MainPID/invocation/boot/executable changed')
                proc = open_directory(Path('/proc')/str(process.pid))
                try:
                    if (os.readlink('exe', dir_fd=proc) != str(python)
                            or read_bytes_at(proc, 'cmdline', limit=4096).split(b'\0') != [
                                b'/usr/bin/python3', b'-ISB', os.fsencode(self.installed.runner), b'--cleanup', scope.unit.encode(), b'']):
                        raise Refused('cleanup-only actual fixed interpreter/argv changed')
                finally: os.close(proc)
                bounds = parse_properties(await self.command.run(TOOLS[1], 'show', '--no-pager',
                    '--property=RuntimeMaxUSec,TimeoutStopUSec', '--', cleanup.unit), ('RuntimeMaxUSec', 'TimeoutStopUSec'))
                from mop.node.host_phase_manifest import duration_usec
                if duration_usec(bounds['RuntimeMaxUSec']) != 8000000 or duration_usec(bounds['TimeoutStopUSec']) != 2000000:
                    raise Refused('cleanup-only actual runtime bounds changed')
                await self.resource_readback(cleanup)
                fd = self._cgroup(cleanup)
                identity = os.fstat(fd)
                self._members(fd, cleanup)
                if read_at(fd, 'cgroup.procs').splitlines() != [str(process.pid)]:
                    raise Refused('cleanup-only cgroup is not the exact singleton MainPID')
                writer.write(encode(dict(schema_version=1, action='cleanup_approved', scope=scope.wire()))+b'\n')
                await writer.drain()
                require_frame(decode(await read_frame(reader)), dict(schema_version=1, action='cleanup_done', scope=scope.wire()))
                if peer_credentials(writer.get_extra_info('socket')) != peer or Process.read(peer[0]) != process:
                    raise Refused('cleanup-only peer/start changed before retirement')
                kill = os.open('cgroup.kill', os.O_WRONLY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=fd)
                try:
                    if os.write(kill, b'1') != 1: raise Unknown('cleanup-only cgroup kill incomplete')
                finally: os.close(kill)
                while True:
                    try:
                        if not self._populated(fd): return
                    except OSError as error:
                        if (error.errno in (errno.ENOENT, errno.ENODEV) and removed_cgroup_empty(identity,
                                os.fstat(fd), os.readlink('/proc/self/fd/'+str(fd)), cleanup)):
                            return
                        raise
                    await asyncio.sleep(.02)
        finally:
            if fd is not None: os.close(fd)
            if writer is not None: writer.close()

    async def _unit(self, scope):
        load = parse_properties(await self.command.run(TOOLS[1], 'show', '--no-pager',
            '--property=LoadState', '--', scope.unit), {'LoadState'})
        if load['LoadState'] == 'not-found':
            return None
        if load['LoadState'] != 'loaded':
            raise Refused('HOST unit is not loaded; collision refused')
        names = set(self._properties(scope)) | EXTRA_PROPERTIES
        raw = await self.command.run(TOOLS[1], 'show', '--all', '--no-pager',
                                     '--property='+','.join(sorted(names)), '--', scope.unit)
        values = parse_properties(raw, names)
        if values['LoadState'] == 'not-found':
            # Exact отсутствие unit — не доказательство исчезновения descendants.
            return None
        if (values['Id'] != scope.unit or values['LoadState'] != 'loaded'
                or values['Transient'] != 'yes'
                or values['FragmentPath'] != '/run/systemd/transient/'+scope.unit
                or values['DynamicUser'] != 'no' or values['PrivateUsers'] != 'no'
                or any(values[key] != '' for key in EMPTY_PROPERTIES)):
            raise Refused('HOST unit actual installed ownership mismatch')
        for key, expected in self._properties(scope).items():
            actual = values[key]
            if key in ('InaccessiblePaths', 'ReadWritePaths'):
                equal = _paths(actual) == _paths(expected)
            else:
                equal = actual == expected
            if not equal:
                raise Refused('HOST unit actual property mismatch: '+key)
        if values['ControlGroup'] != scope.cgroup:
            raise Refused('HOST actual ControlGroup mismatch or unavailable')
        if not values['MainPID'].isdigit() or int(values['MainPID']) >= 2**31:
            raise Refused('invalid actual HOST MainPID')
        # D-Bus object escaping задаётся systemd, не caller path.
        object_name = ''.join(c if c.isalnum() else '_'+format(ord(c), '02x') for c in scope.unit)
        for key, signature in STRUCTURED_EMPTY.items():
            raw = await self.command.run(TOOLS[2], '--system', '--json=short', 'get-property',
                'org.freedesktop.systemd1', '/org/freedesktop/systemd1/unit/'+object_name,
                'org.freedesktop.systemd1.Service', key)
            parse_empty(raw, signature)
        raw_exec = await self.command.run(TOOLS[2], '--system', '--json=short', 'get-property',
            'org.freedesktop.systemd1', '/org/freedesktop/systemd1/unit/'+object_name,
            'org.freedesktop.systemd1.Service', 'ExecStart')
        executable, argv, termination = _parse_exec_details(raw_exec, False)
        raw_ex = await self.command.run(TOOLS[2], '--system', '--json=short', 'get-property',
            'org.freedesktop.systemd1', '/org/freedesktop/systemd1/unit/'+object_name,
            'org.freedesktop.systemd1.Service', 'ExecStartEx')
        if parse_exec_ex(raw_ex) != (executable, argv):
            raise Refused('HOST ExecStartEx differs from installed ExecStart')
        # Уже прочитанные manager facts, не новый запрос и не consumer assertion.
        values['ExecStartTermination'] = termination
        return values, executable, argv

    def _cgroup(self, scope):
        return open_directory(CGROUP_ROOT / scope.cgroup.lstrip('/'), owner=0, immutable=True)

    def _populated(self, fd):
        events = {}
        for line in read_at(fd, 'cgroup.events', limit=1024).splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[0] in events or fields[1] not in ('0', '1'):
                raise Refused('invalid cgroup events')
            events[fields[0]] = fields[1]
        if 'populated' not in events:
            raise Refused('missing cgroup populated fact')
        return events['populated'] == '1'

    def _members(self, fd, scope):
        if read_at(fd, 'cgroup.type', limit=64) != 'domain' or read_at(fd, 'cgroup.subtree_control', limit=1024):
            raise Refused('nondelegated domain cgroup required')
        # Нет glob/walk по host /proc или manager units. В unit TasksMax=128.
        with os.scandir(fd) as entries:
            for i, entry in enumerate(entries):
                if i >= 256:
                    raise Unknown('cgroup enumeration exceeds bound')
                if entry.is_dir(follow_symlinks=False):
                    raise Refused('unexpected nested cgroup; delegation refused')
        pids = read_at(fd, 'cgroup.procs', limit=TASKS_MAX*12).splitlines()
        if len(pids) > TASKS_MAX or any(not p.isdecimal() or not 0 < int(p) < 2**31 for p in pids):
            raise Unknown('cgroup membership exceeds bound or changed')
        for pid in pids:
            proc = open_directory(Path('/proc')/pid)
            try:
                before = parse_proc_start(read_bytes_at(proc, 'stat'), int(pid))
                values = parse_proc_status(read_bytes_at(proc, 'status'))
                if (values.get('Uid', '').split() != [str(self.installed.body_uid)]*4
                        or values.get('Gid', '').split() != [str(self.installed.body_gid)]*4
                        or not set(values.get('Groups', '').split()) <= {str(self.installed.body_gid)}
                        or values.get('NoNewPrivs') != '1'
                        or any(values.get(k) != '0000000000000000'
                               for k in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb'))
                        or read_at(proc, 'cgroup', limit=4096) != '0::'+scope.cgroup):
                    raise Refused('HOST process actual UID/GID/cgroup/privilege mismatch')
                after = parse_proc_start(read_bytes_at(proc, 'stat'), int(pid))
                if before != after:
                    raise Unknown('HOST process changed during inspection')
            finally:
                os.close(proc)

    async def startup_inventory(self):
        """Cold bind не усыновляет даже пустой старый unit; root ничего не kills."""
        raw = await self.command.run(TOOLS[1], 'list-units', '--all', '--no-pager',
            '--plain', '--output=json', '--', 'mop-host-*.service')
        try:
            rows = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise Unknown('HOST startup manager inventory unavailable') from error
        if type(rows) is not list or len(rows) > 256:
            raise Unknown('HOST startup manager inventory exceeds bound')
        units = []
        for row in rows:
            if type(row) is not dict or 'unit' not in row:
                raise Refused('HOST startup manager inventory malformed')
            units.append(Scope.from_unit(row['unit']).unit)
        try:
            fd = open_directory(CGROUP_ROOT/'mop.slice/mop-host.slice', owner=0, immutable=True)
        except FileNotFoundError:
            return tuple(units)
        try:
            with os.scandir(fd) as entries:
                for i, entry in enumerate(entries):
                    if i >= 512:
                        raise Unknown('HOST startup cgroup inventory exceeds bound')
                    if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                        units.append(entry.name)
            if self._populated(fd) or read_at(fd, 'cgroup.procs', limit=4096):
                units.append('unresolved-kernel-membership')
            return tuple(units)
        finally:
            os.close(fd)

    async def inspect(self, scope):
        return await self._inspect(scope)

    async def _inspect(self, scope, *, launched=False):
        if type(scope) is not Scope:
            raise Refused('typed HOST scope required')
        # Consumer cwd — launch admission, не authority уже созданного scope:
        # тело может chmod/сменить/удалить его, но не своё containment ownership.
        self._runner()
        reference = None
        try:
            result = await self._unit(scope)
            if result is None:
                return None
            values, executable, argv = result
            values = values.copy()
            values.pop('ExecStartTermination', None)
            result = values, executable, argv
            # Pin первого проверенного invocation до чтения volatile membership.
            # False здесь не empty claim: snapshot не выходит из этого метода.
            facts = Facts(scope, values['InvocationID'], self.boot_id,
                int(values['User']), int(values['Group']), executable, argv,
                values['ControlGroup'], False, True)
            reference = facts.verify(self.installed, scope).reference
            fd = self._cgroup(scope)
            try:
                self._members(fd, scope)
                populated = self._populated(fd)
                again = await self._unit(scope)
                termination = None
                if again is not None:
                    observed, observed_executable, observed_argv = again
                    observed = observed.copy()
                    # Metadata нужна только exception, не меняет прежнюю equality.
                    termination = observed.pop('ExecStartTermination', None)
                    again = observed, observed_executable, observed_argv
                if again != result:
                    # Единственный доказанный lifecycle transition Type=exec /
                    # RemainAfterExit: starter штатно ушёл (CLD_EXITED=1/status=0).
                    # SIGTERM тоже systemd success, но не разрешённый normal exit.
                    # Все прочие поля и exact invocation сравниваются как раньше;
                    # нет повторного inspect, нового start или adoption incarnation.
                    exited = ({**values, 'MainPID': '0', 'SubState': 'exited'}, executable, argv)
                    if not (values.get('ActiveState') == 'active'
                            and values.get('SubState') == 'running'
                            and int(values.get('MainPID', '0')) > 0
                            and type(termination) is tuple and len(termination) == 2
                            and all(type(v) is int for v in termination)
                            and termination == (1, 0) and again == exited):
                        raise Unknown('HOST unit changed during inspection')
                if self.boot_id != read_kernel('/proc/sys/kernel/random/boot_id', 64):
                    raise Refused('HOST boot identity changed')
                facts = replace(facts, populated=populated)
                facts.verify(self.installed, scope, reference)
                return facts
            finally:
                os.close(fd)
        except (Refused, Unknown, OSError, IndexError, ValueError) as error:
            if isinstance(error, (Refused, Unknown)):
                failure = error
            else:
                failure = Unknown('HOST kernel/unit observation unavailable: '+type(error).__name__)
            if launched:
                raise StartUnknown(str(failure), reference) from error
            if failure is error:
                raise
            raise failure from error

    async def start(self, scope):
        self._body(scope)
        if await self._unit(scope) is not None:
            raise Refused('HOST unit collision; no foreign start or stop')
        # System manager env не может протащить credential в installed runner.
        environment = await self.command.run(TOOLS[1], 'show-environment')
        for line in environment.decode('ascii').splitlines():
            key, _, value = line.partition('=')
            if (key == 'PATH' and value in ('/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                                          '/usr/local/bin:/usr/bin:/bin', '/usr/bin:/bin')
                    or key in ('LANG', 'LC_ALL', 'LC_CTYPE') and value in ('C', 'C.UTF-8', 'en_US.UTF-8')):
                continue
            raise Refused('system manager environment is not consumer-only')
        # Не передаём environment root-процесса: BoundedCommand задаёт свой.
        limits = getattr(self, '_phase_limits', {}).get(scope)
        if limits is None:
            await self.command.run(*launch_argv(self.installed, scope))
        else:
            props = self._properties(scope)
            props.update(limits.properties())
            await self.command.run(TOOLS[0], '--quiet', '--unit='+scope.unit,
                *('--property='+k+'='+v for k, v in props.items()),
                '--', str(self.installed.runner), '--scope', scope.unit)
        facts = await self._inspect(scope, launched=True)
        if facts is None:
            raise StartUnknown('HOST launch acknowledged without inspectable containment')
        if limits is not None:
            self.retain_phase_cgroup(facts)
        return facts

    async def retire(self, reference):
        scope = reference.scope
        pinned = getattr(self, '_phase_cgroups', {}).get(reference)
        if pinned is not None and removed_cgroup_empty(pinned.identity, os.fstat(pinned.fd),
                os.readlink('/proc/self/fd/'+str(pinned.fd)), scope):
            pinned.facts.verify(self.installed, scope, reference)
            return replace(pinned.facts, populated=False)
        try:
            facts = await self.inspect(scope)
        except (Refused, Unknown):
            if pinned is not None and removed_cgroup_empty(pinned.identity, os.fstat(pinned.fd),
                    os.readlink('/proc/self/fd/'+str(pinned.fd)), scope):
                return replace(pinned.facts, populated=False)
            raise
        if facts is None:
            if pinned is not None and removed_cgroup_empty(pinned.identity, os.fstat(pinned.fd),
                    os.readlink('/proc/self/fd/'+str(pinned.fd)), scope):
                return replace(pinned.facts, populated=False)
            raise Unknown('HOST unit absent; retirement unknown')
        facts.verify(self.installed, scope, reference)
        fd = os.dup(pinned.fd) if pinned is not None else self._cgroup(scope)
        try:
            identity = os.fstat(fd)
            again = await self.inspect(scope)
            if again is None:
                raise Unknown('HOST containment changed before retirement')
            again.verify(self.installed, scope, reference)
            check = self._cgroup(scope)
            try:
                current = os.fstat(check)
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise Refused('HOST cgroup inode changed; no foreign kill')
            finally:
                os.close(check)
            # Pinned cgroup fd, а не PID/group/prefix или systemctl stop по имени.
            kill = os.open('cgroup.kill', os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                           dir_fd=fd)
            try:
                if os.write(kill, b'1') != 1:
                    raise Unknown('HOST cgroup kill incomplete')
            finally:
                os.close(kill)
            deadline = time.monotonic()+3.
            while self._populated(fd):
                if time.monotonic() >= deadline:
                    raise Unknown('HOST cgroup still populated after retirement deadline')
                await asyncio.sleep(.02)
            # Empty kernel subtree доказан на том же pinned inode. Unit не stop/reset:
            # так ни GC, ни name collision не становятся чужим destructive effect.
            return replace(facts, populated=False)
        except (Refused, Unknown):
            if pinned is not None and removed_cgroup_empty(pinned.identity, os.fstat(fd),
                    os.readlink('/proc/self/fd/'+str(fd)), scope):
                return replace(pinned.facts, populated=False)
            raise
        except OSError as error:
            # MainPID killed -> manager GC может удалить уже пустой cgroup до
            # следующего open. Только unlink именно pinned directory inode —
            # kernel empty proof; отсутствие name/reference этим не становится.
            if (error.errno in (errno.ENOENT, errno.ENODEV)
                    and removed_cgroup_empty(identity, os.fstat(fd),
                        os.readlink('/proc/self/fd/'+str(fd)), scope)):
                return replace(facts, populated=False)
            raise Unknown('HOST cgroup retirement unavailable: '+type(error).__name__) from error
        finally:
            os.close(fd)

    async def aclose(self):
        try:
            await self.command.aclose()
        finally:
            for pinned in getattr(self, '_phase_cgroups', {}).values(): os.close(pinned.fd)
            self._phase_cgroups = {}
            for pinned in self._native_sockets.values(): pinned.close()
            self._native_sockets = {}
