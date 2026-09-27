"""Логин claude.ai без браузера на сервере: сам клиент `claude` в pty (#282).

Реестр кредитов (эпик #281) не переизобретает OAuth: client id, PKCE и
адреса живут внутри официального клиента, и ломаться при их смене нечему.
Сервер только ведёт с клиентом диалог: снимает адрес авторизации, отдаёт его
человеку (странице дашборда или командлету), принимает код и вводит его.

Два режима, оба проверены 26–27.09 на claude 2.1.283:

  login        `claude auth login` -- полный набор областей (user:profile,
               user:inference, ...), результат -- $HOME/.claude/.credentials.json
               в доме кредита. Экран: «Opening browser to sign in…», «If the
               browser didn't open, visit: <адрес гиперссылкой OSC 8>»,
               «Paste code here if prompted > ». Итог -- по документированному
               `claude auth status --json` (loggedIn, email, subscriptionType).
  setup-token  `claude setup-token` -- токен на год, область только
               user:inference (ручке лимитов его не хватает), печатается в
               вывод. Экран тот же, гиперссылка с id и терминатором ST.

Подводные камни, собранные первым прогоном: адрес брать из гиперссылки, а
не из текста (терминал переносит его по строкам вперемешку с управляющими
последовательностями); окно pty широкое, иначе переносится и гиперссылка;
код вводить отдельно от Enter и ждать ответа до двух минут -- код
одноразовый, убитый раньше времени клиент его сжигает.

Секреты (токен, код) наружу только через Login.result; всё, что печатается
или журналируется, проходит через mask(). Только stdlib и fsutil.
"""
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time

from ..common import fsutil

TTL = 600                     # сколько живёт логин без кода
CODE_PAUSE = 2                # пауза между кодом и Enter (#282)
SUBMIT_TIMEOUT = 120          # сколько ждать обмена кода на токен
URL_TIMEOUT = 30              # сколько ждать адреса после запуска
WIDE = 4000                   # ширина pty: гиперссылка одной строкой

MODES = {"login": ["claude", "auth", "login"],
         "setup-token": ["claude", "setup-token"]}

_AUTHORIZE = r"https://claude\.com/cai/oauth/authorize\?"
# Гиперссылка OSC 8: \x1b]8;<params>;<url>(\x07|\x1b\\). У auth login params
# пустые и терминатор BEL, у setup-token -- id=... и ST.
_LINK = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(" + _AUTHORIZE + r"[^\x07\x1b]+)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]")
_TEXT_URL = re.compile(_AUTHORIZE + r"[^\s\"'<>]+")
_TOKEN = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{20,}")
_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_ERROR = re.compile(r"(?i)\b(error|invalid|failed|expired|denied)\b")


# ─── чистое: разбор экрана ────────────────────────────────────────────────
def plain(raw):
    """Байты pty -> текст без управляющих последовательностей."""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    return _ANSI.sub("", text)


def authorize_url(raw):
    """Адрес авторизации из вывода клиента либо None. Сначала гиперссылка --
    она не переносится; откат -- склеенный текст без переносов строк."""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    m = _LINK.search(text)
    if m:
        return m.group(1)
    lines = plain(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        m = _TEXT_URL.search(line)
        if not m:
            continue
        # Продолжение адреса -- строки без пробелов; приглашение кода
        # («Paste code here…») склеивать нельзя.
        url = m.group(0)
        for nxt in lines[i + 1:]:
            nxt = nxt.strip()
            if not nxt or " " in nxt:
                break
            url += nxt
        return url
    return None


def scopes_of(url):
    """Области действия из адреса авторизации: по ним видно, годится ли
    кредит для ручки лимитов (нужна user:profile)."""
    m = re.search(r"[?&]scope=([^&]+)", url or "")
    if not m:
        return []
    from urllib.parse import unquote_plus
    return unquote_plus(m.group(1)).split()


def token_of(text):
    """Токен setup-token в выводе либо None."""
    m = _TOKEN.search(plain(text))
    return m.group(0) if m else None


def outcome(text):
    """Что говорит экран после ввода кода: "token", "error" или "pending"."""
    text = plain(text)
    if _TOKEN.search(text):
        return "token"
    if _ERROR.search(text):
        return "error"
    return "pending"


def mask(text):
    """Текст для журнала: ни одного секрета sk-ant-…"""
    return _SECRET.sub("<masked>", text or "")


def credentials_path(home):
    return os.path.join(home, ".claude", ".credentials.json")


def status_line(status):
    """`claude auth status --json` -> строка для человека либо None, если не
    вошли. Без секретов: почта и тип подписки."""
    if not (status or {}).get("loggedIn"):
        return None
    who = status.get("email") or "?"
    sub = status.get("subscriptionType")
    return f"logged in as {who} ({sub})" if sub else f"logged in as {who}"


def prepare_home(home):
    """Дом кредита: каталог 0700 и флаги первого запуска, которые пишет
    врапер папета (spec.py): онбординг пройден, вопрос о восстановлении
    длинного разговора снят -- чтобы `claude -p` в этом доме (обновление
    токена) не встал на экране, где нажать некому. Своё в файле не теряется."""
    fsutil.make_private_dir(home)
    path = os.path.join(home, ".claude.json")
    try:
        with open(path) as f:
            flags = json.load(f)
    except (OSError, ValueError):
        flags = {}
    flags.update({"hasCompletedOnboarding": True, "resumeReturnDismissed": True})
    flags.setdefault("theme", "dark")
    fsutil.write_private(path, json.dumps(flags))
    fsutil.make_private_dir(os.path.join(home, ".claude"))


def auth_status(home):
    """`claude auth status --json` в доме кредита -> dict (пустой при отказе)."""
    try:
        out = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True,
                             text=True, timeout=60, env=_env(home)).stdout
        return json.loads(out) if out.strip() else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def _env(home):
    env = dict(os.environ, HOME=home, BROWSER="/bin/false", COLUMNS=str(WIDE))
    env.pop("DISPLAY", None)
    return env


# ─── живое: клиент в pty ──────────────────────────────────────────────────
class Login:
    """Один логин: клиент в pty от старта до итога.

    start() -> объект с .url; submit(code) вводит код и ждёт итога; .result
    -- токен (setup-token) либо путь к файлу кредов (login); close() убивает
    клиента. Незавершённый логин старше TTL -- expired()."""

    def __init__(self, home, mode):
        if mode not in MODES:
            raise ValueError(f"no login mode {mode}; modes: {', '.join(MODES)}")
        self.home, self.mode = home, mode
        self.pid = self.fd = None
        self.url = self.result = self.error = None
        self.started = time.time()
        self._log = b""

    @classmethod
    def start(cls, home, mode="login"):
        self = cls(home, mode)
        prepare_home(home)
        pid, fd = pty.fork()
        if pid == 0:                                    # клиент
            os.environ.update(_env(home))
            os.execvp("claude", MODES[mode])
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, WIDE, 0, 0))
        raw = self._read(URL_TIMEOUT, until=lambda b: authorize_url(b) is not None)
        self.url = authorize_url(raw)
        if not self.url:
            self.close()
            raise RuntimeError("claude printed no authorize url: " + mask(plain(raw))[-300:])
        return self

    def expired(self):
        return self.result is None and time.time() - self.started > TTL

    def submit(self, code, timeout=SUBMIT_TIMEOUT):
        """Ввести код и дождаться итога. -> True при успехе; иначе .error."""
        code = (code or "").strip()
        if not code:
            self.error = "empty code"
            return False
        os.write(self.fd, code.encode())
        self._read(CODE_PAUSE)
        os.write(self.fd, b"\r")
        end = time.time() + timeout
        while time.time() < end:
            chunk = self._read(5)
            text = plain(self._log)
            if self.mode == "setup-token":
                tok = token_of(text)
                if tok:
                    self.result = tok
                    break
            else:
                if os.path.exists(credentials_path(self.home)) and \
                        auth_status(self.home).get("loggedIn"):
                    self.result = credentials_path(self.home)
                    break
            if not chunk and self._dead():
                break
            if outcome(chunk) == "error":
                self.error = mask(plain(chunk)).strip()[-200:]
                break
        if self.result is None and not self.error:
            self.error = "no answer from claude within %d s" % timeout
        self.close()
        return self.result is not None

    def close(self):
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            self.pid = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def log(self):
        """Экран клиента для журнала, без секретов."""
        return mask(plain(self._log))

    # ── внутреннее ──
    def _dead(self):
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid != 0
        except ChildProcessError:
            return True

    def _read(self, secs, until=None):
        buf, end = b"", time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.5)
            if not r:
                if until and until(self._log + buf):
                    break
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if until and until(self._log + buf):
                break
        self._log += buf
        return buf
