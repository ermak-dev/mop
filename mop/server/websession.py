"""Серверная HTTP-сессия дашборда: вход file/LDAP, TTL, отзыв (#415).

Провайдер -- identity.server_provider: тот же список людей и тот же пароль,
что у входа в шину (callout, #206), одно место. Пароль не возвращается и не
логируется: у сессии -- логин и роль, у cookie -- только токен.

Сессия живёт на сервере: cookie -- случайный токен сессий (secrets), не
подпись полезной нагрузки; TTL один на всех, отзыв -- удалением из таблицы.
Роль перечитывается поставщиком при каждом взгляде: сессия пережила
`mop server user` -- роль могла уйти.
"""
import ipaddress
import json
import os
import secrets
import threading
import time

from . import identity
from ..common import fsutil


# TTL сессии, с. Абсолютный, рабочий день: брошенная вкладка не ходит
# админом до утра.
TTL = 8 * 3600
# Неудачных входов с одного адреса в окно RATE_WINDOW -- дальше 429.
RATE_LIMIT = 5
RATE_WINDOW = 15 * 60
COOKIE = "mop_session"


class SessionStore:
    """Токен -> (login, role) с TTL и отзывом. Чистая от сети.

    Один замок на таблицу: вход -- редкое событие, а GET /api/session ходит
    часто; спорить о таблице незачем, счётчиков попыток она не касается.

    Таблица переживает рестарт сервера (#485): раскатка перезапускает
    сервис, и выходить каждому админу после каждого deploy -- не жизнь.
    Файл в приватном каталоге сервера, рядом с журналом операций (#476):
    атомарная запись (временный + os.replace) на open/close, загрузка на
    старте с вычисткой просроченных. В файле -- токены и дедлайны; роль не
    пишется: её всё равно перечитывает провайдер на каждом взгляде (#415),
    и украденный файл даёт ровно то, что даёт украденная cookie. Битый
    файл -- старт с пустой таблицы: сессии сбрасываются честно, сервер
    поднимается.
    """

    def __init__(self, now=None, path=None):
        from ..common import paths
        self._now = now or time.time
        self._lock = threading.Lock()
        self._sessions = {}      # токен -> (login, role, отозвать после)
        self._path = path or paths.local("sessions.json")
        self._load()

    # ── устойчивость (#485) ────────────────────────────────────────────
    def _load(self):
        """Файл -> таблица. Просроченные не поднимаются; битый файл --
        пустая таблица: молча терять живые сессии плохо, но падать сервису
        ещё хуже, а потеря -- ровно прежнее поведение без файла."""
        try:
            with open(self._path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        now = self._now()
        for token, row in raw.items():
            if isinstance(row, list) and len(row) == 3 and row[2] >= now:
                self._sessions[token] = tuple(row)

    def _save(self):
        """Таблица -> файл, атомарно. Пишется под замком таблицы."""
        try:
            fsutil.write_atomic(self._path, json.dumps(self._sessions))
        except OSError:
            # Каталог недоступен -- сессии живут в памяти, как до #485:
            # вход не должен падать из-за хранилища.
            pass

    def open(self, who):
        """Личность -> токен cookie."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (who.login, who.role, self._now() + TTL)
            self._save()
        return token

    def get(self, token, now=None):
        """Токен -> (login, role) | None: истёкшая или отозванная -- None."""
        now = self._now() if now is None else now
        with self._lock:
            got = self._sessions.get(token)
            if not got or got[2] < now:
                self._sessions.pop(token, None)
                return None
        # Личность перечитывается провайдером на каждом взгляде (#415):
        # удалённый из file/LDAP -- и admin, и user -- теряет сессию сразу,
        # а не в конце TTL; роль берётся сегодняшняя, какой бы она ни была.
        who = provider().lookup(got[0])
        if who is None:
            return None
        return who.login, who.role

    def close(self, token):
        """Отзыв. -> True, если сессия была."""
        with self._lock:
            had = self._sessions.pop(token, None) is not None
            if had:
                self._save()
            return had


def provider():
    """Провайдер входа -- тот же, что у шины (#206): file/LDAP по настройкам
    установки, файлы -- из копии /etc/nats. Одна точка для подмены в
    проверках."""
    return identity.server_provider()


def origin_ok(headers, origin):
    """Origin строго равен публичному https origin установки.

    Без Origin -- отказ: браузер обязан его слать на POST, а отсутствие
    здесь -- не браузер. «Ограничено проверенным безопасным исключением»
    (curl с того же сервера) -- дело вызывающего, тут его нет."""
    return headers.get("Origin") == origin


def client_addr(headers, peer):
    """Ключ rate limit: чей это браузер.

    Через nginx одноранговый адрес сокета -- 127.0.0.1 у всех, и пять неудач
    одного запирали бы вход всей конторе. Прокси-контракт нынешнего nginx
    (deploy/roles/proxy/templates/mop.conf.j2): X-Forwarded-For
    $proxy_add_x_forwarded_for -- последний элемент ставит сам nginx своим
    $remote_addr, клиентский префикс может быть подделан и не читается.
    X-Real-IP nginx не ставит и не срезает: доверять ему нельзя вовсе.
    Заголовки читаются только с петли: запрос мимо прокси несёт свои."""
    if peer[0] in ("127.0.0.1", "::1"):
        chain = (headers.get("X-Forwarded-For") or "").split(",")
        got = chain[-1].strip() if chain else ""
        try:
            if got:
                return str(ipaddress.ip_address(got))
        except ValueError:
            pass
    return peer[0]


def public_origin(config_get):
    """Публичный https origin: https://<MOP_SERVER_LAN>[:порт], порт 443
    не пишется -- тот, что браузер показывает в адресной строке."""
    host = config_get("MOP_SERVER_LAN")
    port = config_get("MOP_HTTPS_PORT")
    suffix = "" if port in ("", "443", None) else f":{port}"
    return f"https://{host}{suffix}"


class RateLimiter:
    """Неудачные входы с одного адреса в скользящем окне.

    Успешный вход не считается, адрес нормализован как строка. Один замок:
    таблица крошечная, а спорить о ней -- дороже, чем подождать.
    """

    def __init__(self, now=None, limit=RATE_LIMIT, window=RATE_WINDOW):
        self._now = now or time.time
        self._limit, self._window = limit, window
        self._lock = threading.Lock()
        self._fails = {}        # адрес -> [моменты неудач]

    def allowed(self, addr):
        at = self._now()
        with self._lock:
            self._fails[addr] = [t for t in self._fails.get(addr, []) if t > at - self._window]
            return len(self._fails[addr]) < self._limit

    def note_failure(self, addr):
        at = self._now()
        with self._lock:
            self._fails.setdefault(addr, []).append(at)

    def note_success(self, addr):
        with self._lock:
            self._fails.pop(addr, None)
