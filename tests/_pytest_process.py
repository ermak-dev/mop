"""Владение subprocess legacy check: lifecycle, не collection/verdict runner.

Отдельный subreaper сохраняет происхождение double-fork/setsid потомков.
Перечисляются только children его собственного дерева, никогда /proc целиком.
Pytest fixture и HOME slot держат owner до communicate и полного wait/reap.
"""
import ctypes
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time


COOPERATIVE_SECONDS = 2
TERM_SECONDS = .5
KILL_SECONDS = 2
OWNER_SECONDS = 6
CLEANUP_FAILURE = 125
IDENTITY_FIELDS = ('pid', 'start', 'group', 'namespace')


def snapshot(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start': fields[19], 'group': int(fields[2]),
                'namespace': os.readlink(f'/proc/{pid}/ns/pid'),
                'parent': int(fields[1])}
    except FileNotFoundError:
        return None


class OwnedPid:
    """PID/start/group/namespace + pidfd: переиспользованный PID не сигналим."""
    def __init__(self, pid, parent):
        before = snapshot(pid)
        self.fd = os.pidfd_open(pid)
        self.row = snapshot(pid)
        if (self.row is None or before != self.row or self.row['parent'] != parent
                or self.row['namespace'] != os.readlink('/proc/self/ns/pid')):
            os.close(self.fd)
            raise RuntimeError(f'process ownership changed during registration: {pid}')

    def current(self):
        row = snapshot(self.row['pid'])
        if row is None:
            return None
        # Parent меняется при adoption subreaper-ом; прочие поля — evidence.
        if any(row[key] != self.row[key] for key in IDENTITY_FIELDS):
            if select.select([self.fd], [], [], 0)[0]:
                return None  # Исходный процесс уже умер, новый PID не наш.
            raise RuntimeError(f'process ownership changed: {self.row}')
        return row

    def send(self, sig):
        if self.current() is not None:
            try:
                signal.pidfd_send_signal(self.fd, sig)
            except ProcessLookupError:
                pass

    def close(self):
        os.close(self.fd)


def children(pid):
    """Только children конкретного уже принадлежащего нам процесса/его threads."""
    result = set()
    try:
        tasks = list(Path(f'/proc/{pid}/task').iterdir())
    except FileNotFoundError:
        return result
    for task in tasks:
        try:
            result.update(map(int, (task / 'children').read_text().split()))
        except FileNotFoundError:
            pass
    return result


def supervise(argv, owner_fd=None):
    """Пассивный владелец одного entrypoint; результат утверждений не толкует."""
    channel = None
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'test process subreaper unavailable')
    cancelled = False

    def cancel(signum, frame):
        nonlocal cancelled
        cancelled = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancel)
    if owner_fd is not None:
        channel = socket.socket(fileno=owner_fd)
        channel.set_inheritable(False)
        channel.settimeout(OWNER_SECONDS)
        # Check не стартует, пока pytest/slot не зарегистрировал этого owner;
        # parent не получает готовый объект до установки signal handlers.
        channel.send(b'ready')
        if channel.recv(16) != b'start':
            channel.close()
            return CLEANUP_FAILURE
    process = subprocess.Popen(argv, start_new_session=True)
    ledger = {}
    errors = set()
    own = os.getpid()

    def discover():
        # Только прямые/adopted дети subreaper-а. Не обходим дерево по голому
        # PID предка: тот мог умереть и переиспользоваться между двумя чтениями.
        # После остановки parent его дети становятся нашими и попадают сюда.
        for pid in children(own):
            if pid not in ledger:
                try:
                    ledger[pid] = OwnedPid(pid, own)
                except ProcessLookupError:
                    pass
                except (OSError, RuntimeError) as error:
                    errors.add(f'{type(error).__name__}: {error}')

    def reap():
        # Сначала Popen: его returncode обязан сохраниться, а не потеряться
        # в waitpid(-1). Остальные ждём только по зарегистрированным PID.
        process.poll()
        for pid, handle in list(ledger.items()):
            row = handle.current()
            if row is not None and pid != process.pid and row['parent'] == own:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    errors.add(f'owned child was not waitable: {handle.row}')
            if handle.current() is None:
                handle.close()
                del ledger[pid]

    def send_all(sig):
        for handle in list(ledger.values()):
            try:
                handle.send(sig)
                if sig == signal.SIGTERM:
                    handle.send(signal.SIGCONT)
            except (OSError, RuntimeError) as error:
                errors.add(f'{type(error).__name__}: {error}')

    try:
        # До отмены не обходим работающий check. Его прямой ребёнок ещё не
        # reaped, поэтому PID/start невозможно переиспользовать при capture.
        ledger[process.pid] = OwnedPid(process.pid, own)
        while process.poll() is None and not cancelled:
            time.sleep(.02)
        if cancelled and process.returncode is None:
            # Только entrypoint: scheduler наконец попадает в свой finally и
            # может cancel/join своих четырёх отдельно запущенных owners.
            try:
                ledger[process.pid].send(signal.SIGINT)
            except (OSError, RuntimeError) as error:
                errors.add(f'{type(error).__name__}: {error}')
            until = time.monotonic() + COOPERATIVE_SECONDS
            while process.poll() is None and time.monotonic() < until:
                time.sleep(.02)
        # Успешный parent exit не доказывает отсутствие detached потомков.
        # Adoption даёт точную ancestry даже для уже закончившегося parent.
        for sig, budget in ((signal.SIGTERM, TERM_SECONDS), (signal.SIGKILL, KILL_SECONDS)):
            until = time.monotonic() + budget
            while True:
                discover()
                reap()
                if not ledger and not children(own):
                    break
                send_all(sig)
                if time.monotonic() >= until:
                    break
                time.sleep(.02)
            if not ledger and not children(own):
                break
        discover()
        reap()
        if ledger or children(own):
            errors.add('pending owned children: ' + json.dumps([h.row for h in ledger.values()]))
        elif channel is not None:
            # Receipt передаётся приватным FD, не stdout/файлом check-а. Body
            # его не наследует. Exit 0 без этого сообщения не доказывает drain.
            row = snapshot(own)
            channel.send(json.dumps({'quiescent': True, 'errors': sorted(errors),
                'owner': {key: row[key] for key in IDENTITY_FIELDS}}).encode())
        if errors:
            print('owned process cleanup refused: ' + '; '.join(sorted(errors)), file=sys.stderr, flush=True)
            return CLEANUP_FAILURE
        code = process.returncode
        return code if code is not None and code >= 0 else 128 - code if code is not None else 1
    finally:
        for handle in ledger.values():
            handle.close()
        if channel is not None:
            channel.close()


class OwnedProcess:
    """Один синхронный PIPE reader; cancel из scheduler не теряет его thread."""
    def __init__(self, script, *, cwd, env):
        helper = Path(__file__).with_name('_pytest_legacy.py')
        self.channel, child = socket.socketpair(type=socket.SOCK_SEQPACKET)
        self.process = None
        self.handle = None
        started = False
        try:
            self.process = subprocess.Popen([sys.executable, str(helper), '--owner-fd',
                str(child.fileno()), str(script)], cwd=cwd, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True,
                pass_fds=(child.fileno(),))
            self.handle = OwnedPid(self.process.pid, os.getpid())
            self.channel.settimeout(OWNER_SECONDS)
            if self.channel.recv(16) != b'ready':
                raise RuntimeError('owned process startup refused: no ready receipt')
            started = True
            self.channel.send(b'start')
        except BaseException:
            self.channel.close()
            child.close()
            if started:
                self.handle.send(signal.SIGINT)
            if self.process is not None:
                self.process.communicate(timeout=OWNER_SECONDS)
            if self.handle is not None:
                self.handle.close()
            raise
        finally:
            child.close()
        self.lock = threading.Lock()
        self.until = None
        self.output = None
        self.closed = False
        self.cleanup_error = None

    @property
    def returncode(self):
        return self.process.returncode

    def cancel(self):
        with self.lock:
            if not self.closed and self.until is None:
                self.until = time.monotonic() + OWNER_SECONDS
                self.handle.send(signal.SIGINT)

    def communicate(self):
        if self.output is None:
            while True:
                try:
                    self.output = self.process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired as error:
                    # EOF может держать ещё живой body после смерти owner.
                    # Только его зарегистрированный pidfd, не обход чужих PID.
                    exited = bool(select.select([self.handle.fd], [], [], 0)[0])
                    with self.lock:
                        if exited and self.until is None:
                            self.until = time.monotonic() + OWNER_SECONDS
                    if self.until is not None and time.monotonic() >= self.until:
                        if exited:
                            # Reap именно owner; потомки от этого не quiescent.
                            # Больше не читаем удерживаемый ими PIPE, но close
                            # всё равно требует приватный quiescence receipt.
                            self.process.poll()
                            text = error.output or b''
                            if isinstance(text, bytes):
                                text = text.decode(self.process.stdout.encoding, errors='replace')
                            self.output = text, None
                            self.process.stdout.close()
                        self.cleanup_error = RuntimeError('owned process cleanup deadline exceeded; '
                            f'quiescence receipt unproven; owner={self.handle.row}, exit={self.returncode}')
                        raise self.cleanup_error from None
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return self.output

    def close(self):
        if not self.closed:
            if self.output is None:
                self.cancel()
                self.communicate()
            try:
                self.channel.setblocking(False)
                receipt = json.loads(self.channel.recv(4096))
                expected = {key: self.handle.row[key] for key in IDENTITY_FIELDS}
                if (receipt.get('quiescent') is not True or receipt.get('owner') != expected
                        or not isinstance(receipt.get('errors'), list)):
                    raise ValueError('invalid owner/quiescence evidence')
                if receipt['errors']:
                    self.cleanup_error = RuntimeError('owned process cleanup refused: ' + '; '.join(receipt['errors']))
            except (OSError, ValueError, TypeError, AttributeError) as error:
                self.cleanup_error = RuntimeError(f'owned process cleanup refused: missing or invalid quiescence receipt; '
                    f'owner={self.handle.row}, exit={self.returncode}: {error}')
            finally:
                with self.lock:
                    self.channel.close()
                    self.handle.close()
                    self.closed = True
        if self.cleanup_error is not None:
            raise self.cleanup_error

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
