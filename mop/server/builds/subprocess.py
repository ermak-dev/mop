"""Ограниченный subprocess adapter. Invocation создаёт только trusted root.

Process group принадлежит runner до quiescence; отмена ожидания не равна stop.
Private logs — ограниченная диагностика, никогда не журнал исполнения операции.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
from uuid import uuid4


@dataclass(frozen=True)
class TrustedInvocation:
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if (type(self.argv) is not tuple or not self.argv or not os.path.isabs(self.argv[0])
                or any(type(a) is not str or '\0' in a for a in self.argv)
                or not os.path.isabs(self.cwd) or type(self.environment) is not tuple):
            raise ValueError('root-composed absolute invocation required')


@dataclass(frozen=True)
class ProcessResult:
    outcome: str
    quiescent: bool
    reason: str
    returncode: int | None
    tail: str


class SecretRedactor:
    """Не выпускает prefix секрета на границе очередного pipe chunk."""
    def __init__(self, secrets):
        self.secrets = tuple(sorted({s.encode() for s in secrets if s}, key=len, reverse=True))
        self.width = max((len(s) for s in self.secrets), default=1)
        self.pending = b''

    def feed(self, data, *, final=False):
        self.pending += data
        cut = len(self.pending) if final else max(0, len(self.pending)-self.width+1)
        for secret in self.secrets:
            start = self.pending.find(secret)
            while start >= 0 and start < cut:
                if start+len(secret) > cut:
                    cut = start
                    break
                start = self.pending.find(secret, start+1)
        emitted, self.pending = self.pending[:cut], self.pending[cut:]
        for secret in self.secrets:
            emitted = emitted.replace(secret, b'[redacted]')
        return emitted


class SafeAnsibleSubprocess:
    def __init__(self, *, private_directory, known_secrets, max_log_bytes=4096, retention_files=8, retention_seconds=86400):
        directory = Path(private_directory)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or not 1024 <= max_log_bytes <= 65536 or not 1 <= retention_files <= 16
                or not 60 <= retention_seconds <= 86400):
            raise ValueError('private owned log directory and bounded retention required')
        self.directory, self.secrets = directory, tuple(known_secrets)
        if any(type(s) is not str or len(s) > 8192 for s in self.secrets):
            raise ValueError('bounded actual secret redaction inventory required')
        self.maximum, self.files, self.age = max_log_bytes, retention_files, retention_seconds

    def _prune(self):
        files = []
        for path in self.directory.glob('build-*.log'):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('private log retention encountered an unowned file')
            files.append((info.st_mtime, path))
        for index, (modified, path) in enumerate(sorted(files, reverse=True)):
            if index >= self.files-1 or time.time()-modified > self.age:
                path.unlink()

    @staticmethod
    def _group_alive(group):
        # killpg(0) считает zombie живым. На Linux /proc отличает уже умерших
        # потомков; неизвестность/permission refusal никогда не означает stop.
        if Path('/proc').is_dir():
            try:
                for entry in Path('/proc').iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        value = (entry/'stat').read_text()
                        fields = value[value.rfind(')')+2:].split()
                        if int(fields[2]) == group and fields[0] != 'Z':
                            return True
                    except FileNotFoundError:
                        continue
                return False
            except (PermissionError, ValueError, IndexError):
                return True
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return False
        return True

    def _stop(self, process):
        for sig, budget in ((signal.SIGTERM, .3), (signal.SIGKILL, 1)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            deadline = time.monotonic()+budget
            while time.monotonic() < deadline:
                process.poll()
                if not self._group_alive(process.pid):
                    return True
                time.sleep(.01)
        return not self._group_alive(process.pid)

    def run(self, invocation, *, timeout, cancelled=None, on_progress=None):
        if type(invocation) is not TrustedInvocation or not 0 < timeout <= 7200:
            raise ValueError('bounded root-composed invocation required')
        if any(secret and (any(secret in arg for arg in invocation.argv)
                or any(secret in value for _, value in invocation.environment)) for secret in self.secrets):
            raise ValueError('secrets must be private file references, not subprocess arguments/environment')
        self._prune()
        tail, redactor = b'', SecretRedactor(self.secrets)
        path = self.directory/('build-'+uuid4().hex+'.log')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        process, reason, quiescent = None, '', False
        try:
            process = subprocess.Popen(invocation.argv, cwd=invocation.cwd, env=dict(invocation.environment),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True)
            start, last_progress = time.monotonic(), 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    now = time.monotonic()
                    if now-start >= timeout or cancelled is not None and cancelled.is_set():
                        reason = 'build process timeout' if now-start >= timeout else 'build process cancelled'
                        quiescent = self._stop(process)
                        break
                    for key, _ in selector.select(timeout=min(.05, timeout)):
                        data = os.read(key.fd, 4096)
                        if not data:
                            selector.unregister(key.fileobj)
                        else:
                            tail = (tail+redactor.feed(data))[-self.maximum:]
                    if on_progress is not None and now-last_progress >= .5:
                        on_progress(tail.decode('utf-8', errors='replace'))
                        last_progress = now
                tail = (tail+redactor.feed(b'', final=True))[-self.maximum:]
            if not reason:
                remaining = max(.01, timeout-(time.monotonic()-start))
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    reason, quiescent = 'build process timeout', self._stop(process)
                else:
                    quiescent = not self._group_alive(process.pid)
                    if not quiescent:
                        reason, quiescent = 'build process left live descendants', self._stop(process)
            process.poll()
            reason = reason or ('build process confirmed' if process.returncode == 0 else 'build process failed (rc '+str(process.returncode)+')')
            outcome = 'unknown' if not quiescent else 'succeeded' if process.returncode == 0 and reason == 'build process confirmed' else 'failed'
            # Не append без границы: один bounded redacted diagnostic tail.
            os.write(fd, tail)
            return ProcessResult(outcome, quiescent, reason, process.returncode, tail.decode('utf-8', errors='replace'))
        finally:
            if process is not None:
                if self._group_alive(process.pid):
                    self._stop(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                process.stdout.close()
            os.close(fd)
