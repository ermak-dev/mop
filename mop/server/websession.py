"""Серверная HTTP-сессия дашборда: вход file/LDAP, TTL, отзыв (#415).

Провайдер -- identity.server_provider: тот же список людей и тот же пароль,
что у входа в шину (callout, #206), одно место. Пароль не возвращается и не
логируется: у сессии -- логин и роль, у cookie -- только токен.

Сессия живёт на сервере: cookie -- случайный токен сессий (secrets), не
подпись полезной нагрузки; TTL один на всех, отзыв -- удалением из таблицы.
Роль перечитывается поставщиком при каждом взгляде: сессия пережила
`mop server user` -- роль могла уйти.
"""
import secrets
import threading
import time

from . import identity


# TTL сессии, с. Сутки: страница живёт дольше, чем чей-то рабочий день.
TTL = 24 * 3600
# Неудачных входов с одного адреса в окно RATE_WINDOW -- дальше 429.
RATE_LIMIT = 5
RATE_WINDOW = 15 * 60
COOKIE = "mop_session"


class SessionStore:
    """Токен -> (login, role) с TTL и отзывом. Чистая от сети.

    Один замок на таблицу: вход -- редкое событие, а GET /api/session ходит
    часто; спорить о таблице незачем, счётчиков попыток она не касается.
    """

    def __init__(self, now=None):
        self._now = now or time.time
        self._lock = threading.Lock()
        self._sessions = {}      # токен -> (login, role, отозвать после)

    def open(self, who):
        """Личность -> токен cookie."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (who.login, who.role, self._now() + TTL)
        return token

    def get(self, token, now=None):
        """Токен -> (login, role) | None: истёкшая или отозванная -- None."""
        now = self._now() if now is None else now
        with self._lock:
            got = self._sessions.get(token)
            if not got or got[2] < now:
                self._sessions.pop(token, None)
                return None
        login, role = got[0], got[1]
        if role == "admin":
            # Роль могла уйти с прошлой правкой операторов: сессия остаётся
            # сессией, но admin видит уже сегодняшнюю правду. Логина не стало
            # -- сессии тоже нет.
            who = provider().lookup(login)
            if who is None:
                return None
            role = who.role
        return login, role

    def close(self, token):
        """Отзыв. -> True, если сессия была."""
        with self._lock:
            return self._sessions.pop(token, None) is not None


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
