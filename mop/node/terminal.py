"""Приватные PTY attach-клиенты узла (#598), без событий и печати.

Проверка в теле и запись в PTY узла -- две разные операции. Между ними
Claude может выйти, сменить sessionId или foreground group: syscall-atomic
гарантии здесь нет. Проверяем до каждого управления и раз в 5 секунд;
неизвестный исход записи запрещает дальнейший ввод и никогда не повторяется.
Закрываем только attach-клиента, не tmux-сервер и не Claude.

Это доверенный admin-терминал всего тела без редактирования вывода, с теми
же правами, что CLI attach + Claude. tmux prefix/run-shell не фильтруется;
фильтр клавиш не был бы границей безопасности. Контракт тела: пейн прямо
исполняет Claude и заканчивается при его выходе, не возвращается в shell.
PID/start/sessionId, socket/cwd, foreground и tty проверяются свежими
снимками; выход attach/смена identity закрывают поток. Между последней
пробой и вводом остаётся неатомарное окно, не гарантия syscall-level fencing.
"""
import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import inspect
import math
import os
import pty
import secrets
import shlex
import struct
import termios
import time
import tty

from .. import driver

RING_BYTES = 512 * 1024
READ_BYTES = 64 * 1024
INPUT_BYTES = 4 * 1024
NODE_CAP = 16
TARGET_CAP = 4
IDLE_TTL = 60
WRITER_TTL = 30
CHECK_INTERVAL = 5

_FIELDS = {
    "term_open": {"mode", "cols", "rows"},
    "term_read": {"tid", "cursor", "wait"},
    "term_write": {"tid", "seq", "data"},
    "term_resize": {"tid", "cols", "rows"},
    "term_close": {"tid"},
}


def integer(value, low, high, what):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid terminal {what}")


def dimensions(cols, rows):
    integer(cols, 2, 500, "cols")
    integer(rows, 2, 200, "rows")


def actor(name, owner):
    if not isinstance(name, str) or not driver.valid_name(name):
        raise ValueError("invalid terminal target")
    if (not isinstance(owner, str) or not owner or len(owner) > 128
            or owner.strip() != owner
            or any(c.isspace() or ord(c) < 32 or 127 <= ord(c) < 160 for c in owner)):
        raise ValueError("invalid terminal owner")


def decode_input(data):
    if not isinstance(data, str) or len(data) > 4 * ((INPUT_BYTES + 2) // 3):
        raise ValueError("invalid terminal input")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid terminal input") from None
    if len(raw) > INPUT_BYTES or base64.b64encode(raw).decode() != data:
        raise ValueError("invalid terminal input")
    return raw


def validate(verb, req):
    """Служебные поля ставит handle; остальное -- закрытая схема запроса.
    owner явно назван доверенным admin-сервисом на каждом запросе."""
    required = {"verb", "name", "owner"} | _FIELDS[verb]
    if set(req) - {"_project", "_caller"} != required or req.get("verb") != verb:
        raise ValueError("invalid terminal request fields")
    actor(req["name"], req["owner"])
    if "tid" in req and (not isinstance(req["tid"], str) or not 1 <= len(req["tid"]) <= 128):
        raise ValueError("invalid terminal id")
    if "cols" in req:
        dimensions(req["cols"], req["rows"])
    if verb == "term_open" and req["mode"] not in ("ro", "rw"):
        raise ValueError("invalid terminal mode")
    if verb == "term_read":
        integer(req["cursor"], 0, 2**63 - 1, "cursor")
        wait = req["wait"]
        if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 15:
            raise ValueError("invalid terminal wait")
    if verb == "term_write":
        integer(req["seq"], 1, 2**63 - 1, "seq")
        decode_input(req["data"])


@dataclass(frozen=True)
class Identity:
    pid: int
    start: str
    sessionId: str
    pane: str
    tty: str

    @classmethod
    def from_dict(cls, got):
        if not isinstance(got, dict) or set(got) != {"pid", "start", "sessionId", "pane", "tty"}:
            raise ValueError("terminal session identity unavailable")
        if (type(got["pid"]) is not int or got["pid"] <= 0
                or not isinstance(got["start"], str) or not got["start"].isdigit()
                or not isinstance(got["sessionId"], str) or not got["sessionId"]
                or not isinstance(got["pane"], str) or not got["pane"].startswith("%")
                or not got["pane"][1:].isdigit()
                or not isinstance(got["tty"], str) or not got["tty"].startswith("/dev/")):
            raise ValueError("terminal session identity unavailable")
        return cls(**got)


def resolve_body_identity(session_py, cwd, tmux_argv):
    """Исполняется внутри тела: PID namespace и tty не видны с гипервизора.
    Импорт по явному абсолютному пути, python -I не ищет код в cwd клона.
    Несколько живых сессий или пейнов -- отказ, attach не выбирает пейн."""
    import importlib.util
    import json
    import os
    import stat
    import subprocess
    import sys

    if not os.path.isabs(session_py):
        raise ValueError("session source path unavailable")
    spec = importlib.util.spec_from_file_location("_mop_terminal_session", session_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    def identity(record):
        # status/updatedAt/waitingFor меняются во время нормальной работы.
        if not record:
            return None
        return (record.get("pid"), record.get("sessionId"),
                record.get("messagingSocketPath"), os.path.realpath(record.get("cwd") or ""))

    def candidates():
        return [s for s in mod.sessions()
                if os.path.realpath(s.get("cwd") or "") == os.path.realpath(cwd)
                and mod.socket_alive(s["messagingSocketPath"])]

    # PID берём только у канонического tmux target, не из registry или RPC.
    result = subprocess.run(tmux_argv, capture_output=True, text=True, timeout=2, check=True)
    panes = result.stdout.strip().splitlines()
    if len(panes) != 1:
        raise ValueError("terminal pane ambiguous or unavailable")
    pane, path, pane_pid = panes[0].split("\t")
    if not pane_pid.isdigit() or int(pane_pid) <= 0:
        raise ValueError("terminal pane identity unavailable")
    pid = int(pane_pid)
    with open(f"/proc/{pid}/stat") as f:
        before = f.read()
    fields = before[before.rfind(")") + 2:].split()

    def process_config():
        import pwd
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            if uid != os.getuid() or os.path.realpath(os.readlink(f"/proc/{pid}/cwd")) != os.path.realpath(cwd):
                raise ValueError
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read()
            env = {}
            for entry in raw.split(b"\0"):
                key, sep, value = entry.partition(b"=")
                if sep and key in (b"HOME", b"CLAUDE_CONFIG_DIR"):
                    if key in env:
                        raise ValueError
                    env[key] = os.fsdecode(value)
            home = env.get(b"HOME") or pwd.getpwuid(uid).pw_dir
            config = env.get(b"CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
            if not os.path.isabs(config):
                config = os.path.join(cwd, config)
            return os.path.realpath(config)
        except Exception:
            # Ни содержимое environ, ни пути/ключи не доходят до вызывающего.
            raise ValueError("terminal process configuration unavailable") from None

    config = process_config()
    # Только этот импорт в короткоживущем -I процессе; модуль агента не меняем.
    mod.SESSIONS = os.path.join(config, "sessions")
    live = mod.live_session_for_cwd(cwd)
    matches = candidates()
    if not live or len(matches) != 1 or identity(live) != identity(matches[0]):
        raise ValueError("session identity ambiguous or unavailable")
    sid = live.get("sessionId")
    if type(live.get("pid")) is not int or live["pid"] != pid or not isinstance(sid, str) or not sid:
        raise ValueError("session identity unavailable")
    # Поля proc stat: state(3), pgrp(5), tty_nr(7), tpgid(8), starttime(22).
    if fields[0] == "Z" or int(fields[2]) != int(fields[5]) or int(fields[5]) <= 0:
        raise ValueError("Claude is not the terminal foreground process group")
    pane_stat = os.stat(path)
    input_stat = os.stat(f"/proc/{pid}/fd/0")
    encoded = int(fields[4]) & 0xffffffff
    dev = os.makedev((encoded >> 8) & 0xfff, (encoded & 0xff) | ((encoded >> 12) & 0xfff00))
    if (not stat.S_ISCHR(pane_stat.st_mode) or pane_stat.st_rdev != input_stat.st_rdev
            or dev != pane_stat.st_rdev):
        raise ValueError("Claude tty does not match the tmux pane")
    # tcgetpgrp чужого O_NOCTTY fd даёт ENOTTY: foreground доказан выше
    # полями pgrp/tpgid /proc целевого процесса, не tty вызывающего.
    # Повтор снимка отсекает исчезновение PID во время первой пробы.
    with open(f"/proc/{pid}/stat") as f:
        value = f.read()
        after = value[value.rfind(")") + 2:].split()
    current = mod.live_session_for_cwd(cwd)
    matches = candidates()
    final_panes = subprocess.run(tmux_argv, capture_output=True, text=True, timeout=2, check=True)
    if (process_config() != config or final_panes.stdout.strip().splitlines() != panes
            or after[0] == "Z" or any(after[i] != fields[i] for i in (2, 4, 5, 19))
            or identity(current) != identity(live) or len(matches) != 1
            or identity(matches[0]) != identity(live)
            or not mod.socket_alive(current["messagingSocketPath"])
            or os.stat(path).st_rdev != pane_stat.st_rdev
            or os.stat(f"/proc/{pid}/fd/0").st_rdev != pane_stat.st_rdev):
        raise ValueError("terminal session changed during identity probe")
    return {"pid": pid, "start": fields[19], "sessionId": sid, "pane": pane, "tty": path}


def pane_argv(name):
    actor(name, "probe")
    return shlex.split(driver.Tmux(name).base) + ["list-panes", "-s", "-t", name,
                                                 "-F", "#{pane_id}\t#{pane_tty}\t#{pane_pid}"]


def probe_script(session_py, cwd, name):
    argv = pane_argv(name)
    source = inspect.getsource(resolve_body_identity)
    source += ("\nimport json\nprint(json.dumps(resolve_body_identity("
               f"{session_py!r}, {cwd!r}, {argv!r})))\n")
    return "python3 -I -c " + shlex.quote(source)


def resolve_interrupt_delay(tmux_argv):
    """В теле читаем только server option; сырой вывод никогда не печатаем."""
    import subprocess
    try:
        result = subprocess.run(tmux_argv, capture_output=True, text=True,
                                timeout=2, check=True)
        value = result.stdout.strip()
        if not value or len(value) > 5 or any(c not in '0123456789' for c in value):
            return None
        escape_time = int(value)
        return escape_time + 100 if escape_time <= 10000 else None
    except Exception:
        return None  # Optional metadata: неизвестное значение отключает combined UI.


def interrupt_script(name):
    actor(name, 'timing')
    argv = shlex.split(driver.Tmux(name).base) + ['show-options', '-s', '-v', 'escape-time']
    source = inspect.getsource(resolve_interrupt_delay)
    source += f'\ndelay = resolve_interrupt_delay({argv!r})\nif delay is not None: print(delay)\n'
    return 'python3 -I -c ' + shlex.quote(source)


class Ring:
    def __init__(self):
        self.data = bytearray()
        self.end = 0

    def append(self, data):
        self.end += len(data)
        self.data.extend(data[-RING_BYTES:])
        if len(self.data) > RING_BYTES:
            del self.data[:-RING_BYTES]

    def read(self, cursor):
        if cursor > self.end:
            raise ValueError("terminal cursor is ahead of stream")
        start = self.end - len(self.data)
        reset = cursor < start
        cursor = max(cursor, start)
        data = bytes(self.data[cursor - start:cursor - start + READ_BYTES])
        return data, cursor + len(data), reset


class PTY:
    """Один настоящий controlling PTY для argv attach. Без pipe-эмуляции."""
    def __init__(self, fd, process, on_data, on_exit):
        self.fd, self.process = fd, process
        self.on_data, self.on_exit = on_data, on_exit
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(fd, self._read)
        self.waiter = asyncio.create_task(self._wait())

    @classmethod
    async def spawn(cls, argv, cols, rows, on_data, on_exit):
        master, slave = pty.openpty()
        process = None
        try:
            tty.setraw(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.set_blocking(master, False)
            # setsid сам делает TIOCSCTTY на stdin. --wait сохраняет код ssh/tmux.
            process = await asyncio.create_subprocess_exec(
                "setsid", "--ctty", "--wait", *argv,
                stdin=slave, stdout=slave, stderr=slave,
                env=dict(os.environ, TERM="xterm-256color"))
            return cls(master, process, on_data, on_exit)
        except BaseException:
            os.close(master)
            if process is not None:
                process.terminate()
                await process.wait()
            raise
        finally:
            os.close(slave)

    def _read(self):
        try:
            data = os.read(self.fd, READ_BYTES)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                self.on_exit(None)
            self._detach()
            return
        if data:
            self.on_data(data)
        else:
            self._detach()

    def _detach(self):
        if self.fd is not None:
            self.loop.remove_reader(self.fd)
            os.close(self.fd)
            self.fd = None

    async def _wait(self):
        code = await self.process.wait()
        # Последние байты могут быть готовы одновременно с выходом процесса.
        if self.fd is not None:
            self._read()
        self._detach()
        self.on_exit(code)

    def write(self, data):
        if self.fd is None:
            raise OSError("terminal attach closed")
        return os.write(self.fd, data)

    def resize(self, cols, rows):
        if self.fd is None:
            raise OSError("terminal attach closed")
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def close(self):
        self._detach()
        # Ни kill-session, ни сигнал группе пейна. Только локальный attach.
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(self.waiter), 2)
        except asyncio.TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.waiter


@dataclass
class Session:
    name: str
    owner: str
    mode: str
    cols: int
    rows: int
    identity: Identity
    epoch: str
    touched: float
    ring: Ring = field(default_factory=Ring)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    process: object = None
    closed: bool = False
    opening: bool = True
    exit: object = None
    seq: int = 0
    digest: str = ""
    receipt: object = None

    def output(self, data):
        self.ring.append(data)
        self.changed.set()

    def exited(self, code):
        self.closed, self.exit = True, code
        self.changed.set()


class Registry:
    def __init__(self, probe, authorize, spawn, clock=time.monotonic, interrupt_delay=None):
        self.probe, self.authorize, self.spawn = probe, authorize, spawn
        self.clock = clock
        self.interrupt_delay = interrupt_delay
        self.sessions = {}
        self.open_lock = asyncio.Lock()
        self.maintenance = None

    def writer_open(self, name):
        return any(s.name == name and s.mode == "rw" and not s.closed for s in self.sessions.values())

    async def _check(self, s):
        try:
            await self.authorize(s.name, s.owner, require_owner=(s.mode == "rw"))
        except ValueError:
            await self._close(s)
            raise  # Без второй санитизации безопасной причины ворот.
        except Exception:
            await self._close(s)
            raise ValueError("terminal ownership unavailable; reopen required") from None
        try:
            identity = Identity.from_dict(await self.probe(s.name))
        except Exception:
            await self._close(s)
            raise ValueError("terminal identity unavailable; reopen required") from None
        if identity != s.identity:
            await self._close(s)
            raise ValueError("terminal session changed; reopen required")

    async def open(self, name, owner, mode, cols, rows, prepare=None):
        actor(name, owner)
        dimensions(cols, rows)
        if mode not in ("ro", "rw"):
            raise ValueError("invalid terminal mode")
        await self._expire()
        async with self.open_lock:
            if len(self.sessions) >= NODE_CAP or sum(s.name == name for s in self.sessions.values()) >= TARGET_CAP:
                raise ValueError("terminal session limit reached")
            if mode == "rw" and self.writer_open(name):
                raise ValueError("terminal writer already open")
            # Резерв без I/O: ни другой target, ни Stop не ждут чужой профиль.
            s = Session(name, owner, mode, cols, rows, None, secrets.token_urlsafe(18), self.clock())
            tid = secrets.token_urlsafe(32)
            self.sessions[tid] = s
        receipt = None
        delay = None
        try:
            # Сначала доказать сессию, потом staged claim/identity клона.
            s.identity = Identity.from_dict(await self.probe(name))
            if prepare is None:
                await self.authorize(name, owner, claim=(mode == "rw"))
            else:
                receipt = await prepare(name, owner, claim=(mode == "rw"))
            s.process = await self.spawn(name, mode, cols, rows, s.output, s.exited)
            if self.interrupt_delay is not None:
                try:
                    delay = await self.interrupt_delay(name)
                except Exception:
                    pass  # Неизвестная optional metadata не мешает обычному attach.
            await self._check(s)
            if s.closed:
                raise ValueError("terminal attach exited before open")
            if receipt is not None:
                await receipt.commit()
            if s.closed:
                raise ValueError("terminal attach exited before open")
            s.opening = False
            s.touched = self.clock()
        except BaseException as exc:
            # До commit ввод запрещён: это definite pre-input failure.
            # Ошибка закрытия attach не должна пропустить компенсацию пары.
            failures = []
            try:
                await self._close(s)
            except Exception as close_error:
                failures.append(f"terminal attach close failed ({type(close_error).__name__})")
            finally:
                self.sessions.pop(tid, None)
                if receipt is not None:
                    try:
                        failed = await receipt.rollback()
                    except Exception as rollback_error:
                        failed = f"terminal ownership rollback failed ({type(rollback_error).__name__})"
                    if failed:
                        failures.append(failed)
            if failures:
                reason = str(exc) if isinstance(exc, ValueError) else f"terminal open failed ({type(exc).__name__})"
                raise ValueError("; ".join([reason, *failures])) from None
            raise
        if self.maintenance is None:
            self.maintenance = asyncio.create_task(self._maintain_loop())
        result = {"tid": tid, "epoch": s.epoch, "cursor": 0, "mode": mode, "cols": cols, "rows": rows}
        if type(delay) is int and 100 <= delay <= 10100:
            result['interrupt_delay_ms'] = delay
        return result

    def _get(self, name, owner, tid):
        actor(name, owner)
        s = self.sessions.get(tid)
        if s is None or s.name != name or s.owner != owner:
            raise ValueError("terminal session not found for target and owner")
        return s

    async def read(self, name, owner, tid, cursor, wait):
        s = self._get(name, owner, tid)
        integer(cursor, 0, 2**63 - 1, "cursor")
        if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 15:
            raise ValueError("invalid terminal wait")
        await self._expired(s)
        s.touched = self.clock()
        # clear до снимка: байты между снимком и wait уже поставят событие.
        s.changed.clear()
        data, next_cursor, reset = s.ring.read(cursor)
        if not data and not s.closed and wait:
            try:
                await asyncio.wait_for(s.changed.wait(), wait)
            except asyncio.TimeoutError:
                pass
            data, next_cursor, reset = s.ring.read(cursor)
        s.touched = self.clock()
        return {"data": base64.b64encode(data).decode(), "cursor": next_cursor,
                "reset": reset, "closed": s.closed, "exit": s.exit, "epoch": s.epoch}

    async def write(self, name, owner, tid, seq, data):
        actor(name, owner)
        integer(seq, 1, 2**63 - 1, "seq")
        raw = decode_input(data)
        if tid not in self.sessions:
            # TTL/перезапуск/close могли забыть ack уже введённых клавиш.
            # Отсутствие квитанции не доказывает недоставку: никогда не вводим.
            return {"written": 0, "seq": seq, "outcome": "unknown"}
        s = self._get(name, owner, tid)
        digest = hashlib.sha256(raw).hexdigest()
        async with s.lock:
            if s.opening:
                raise ValueError("terminal open not committed")
            if s.mode != "rw":
                raise ValueError("terminal is read-only")
            if seq == s.seq:
                if digest != s.digest:
                    raise ValueError("terminal sequence reused with different input")
                return dict(s.receipt)
            if seq < s.seq:
                # Более старый ack уже вытеснен: не объявляем недоставленным.
                return {"written": 0, "seq": seq, "outcome": "unknown"}
            if seq != s.seq + 1:
                raise ValueError("terminal sequence must be the next number")
            await self._expired(s)
            if s.closed:
                raise ValueError("terminal closed; reopen required")
            await self._check(s)
            s.touched = self.clock()
            # Слот потреблён до syscall; исключение/частичная запись -- не повтор.
            s.seq, s.digest = seq, digest
            s.receipt = {"written": 0, "seq": seq, "outcome": "unknown"}
            try:
                written = s.process.write(raw)
                s.receipt = {"written": written, "seq": seq,
                             "outcome": "written" if written == len(raw) else "unknown"}
            except Exception:
                pass  # Исход сохранён как unknown, текст сырого исключения приватен.
            if s.receipt["outcome"] == "unknown":
                await self._close(s)
            return dict(s.receipt)

    async def resize(self, name, owner, tid, cols, rows):
        s = self._get(name, owner, tid)
        dimensions(cols, rows)
        async with s.lock:
            await self._expired(s)
            if s.closed:
                raise ValueError("terminal closed; reopen required")
            await self._check(s)
            s.touched = self.clock()
            if s.mode == "rw":
                s.process.resize(cols, rows)
                s.cols, s.rows = cols, rows
            return {"ok": True}

    async def _close(self, s):
        s.closed = True
        s.changed.set()
        if s.process is not None:
            await s.process.close()

    async def close(self, name, owner, tid):
        actor(name, owner)
        if tid not in self.sessions:
            return {"ok": True}
        s = self._get(name, owner, tid)
        async with s.lock:
            await self._close(s)
        self.sessions.pop(tid, None)
        return {"ok": True}

    async def _expired(self, s):
        ttl = WRITER_TTL if s.mode == "rw" else IDLE_TTL
        if not s.opening and self.clock() - s.touched >= ttl:
            await self._close(s)

    async def _expire(self):
        async def expire(tid, s):
            if s.opening or s.lock.locked():
                return
            async with s.lock:
                await self._expired(s)
                ttl = WRITER_TTL if s.mode == "rw" else IDLE_TTL
                if self.clock() - s.touched >= ttl:
                    self.sessions.pop(tid, None)
        # Закрытие локального attach bounded; не 16 последовательных таймаутов.
        await asyncio.gather(*(expire(tid, s) for tid, s in list(self.sessions.items())))

    async def maintain(self):
        await self._expire()
        async def check(s):
            async with s.lock:
                if not s.closed and not s.opening:
                    try:
                        await self._check(s)
                    except ValueError:
                        pass  # Отказ уже закрыл поток, read отдаст closed.
        await asyncio.gather(*(check(s) for s in list(self.sessions.values())))

    async def _maintain_loop(self):
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            await self.maintain()

    async def shutdown(self):
        if self.maintenance is not None:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
            self.maintenance = None
        await asyncio.gather(*(self._close(s) for s in list(self.sessions.values())))
        self.sessions.clear()
