"""Личность оператора и её источник (#205, эпик #36). Данные, без печати.

Личность -- пользователь, прошедший проверку на шине: логин, человеческое
имя, почта, роль и проекты. До #205 её не было как значения: была строка
MOP_OPERATORS, разобранная в словарь {role, projects} (mop/operators.py), и
пароль, сгенерированный deploy'ем в secrets/nats-op-<логин>.pass, -- ни имени,
ни почты, ни места, куда подключить LDAP.

Источник личностей -- провайдер (AuthProvider): проверить пароль и найти по
логину. Первый -- плоский файл на сервере (PlainFileProvider); LDAP -- #208,
auth callout NATS, который будет их спрашивать, -- #206. Какой провайдер
работает, решает настройка MOP_AUTH_PROVIDER.

Файл операторов -- строка на человека, поля через двоеточие, как в
/etc/passwd:

    логин:роль:проекты:имя:почта:хеш
    anton:admin::Антон Ермак:anton@example.dev:scrypt$16384$8$1$<соль>$<ключ>
    ivan:user:rugent,cloudpub:Иван::scrypt$...

Роль и проекты -- по правилам MOP_OPERATORS (operators.entry): admin без
проектов, user с проектами или `*`. Строки с `#` и пустые пропускаются.

Хеш -- hashlib.scrypt (N=2**14, r=8, p=1, соль 16 байт): он в stdlib, как и
pbkdf2_hmac, но требует памяти (~16 МБ на попытку), и перебор на видеокарте
ему дороже; параметры записаны в самом хеше, и поднять их позже можно, не
ломая старые записи.

Переход: провайдер читает и сегодняшний MOP_OPERATORS с паролями из
secrets/nats-op-<логин>.pass -- каждый нынешний логин работает как был. Логин
и в файле, и в настройке (или дважды в файле) -- отказ: у человека одно
определение, и два пароля на один вход -- это вход, которого никто не
выбирал. Отказ бьёт по этому логину, остальные входят; громкий отказ всего
-- у mop deploy, до плейбука.
"""
import base64
import dataclasses
import hashlib
import hmac
import os
import secrets
import typing

from . import operators

SECRETS = os.path.expanduser("~/.config/mop/secrets")
OPERATORS_FILE = "operators"
# Настройки, из которых provider() собирает провайдера.
SETTINGS = ("MOP_AUTH_PROVIDER", "MOP_OPERATORS", "MOP_OPERATORS_FILE")
FIELDS = 6
SCHEME = "scrypt"
# Параметры scrypt для интерактивного входа; память -- 128 * N * r байт.
N, R, P, SALT = 2 ** 14, 8, 1, 16


@dataclasses.dataclass(frozen=True)
class Identity:
    """Кто вошёл. projects -- кортеж: значение неизменяемое целиком."""
    login: str
    role: str
    projects: tuple
    name: str = ""
    email: str = ""


class Refused(Exception):
    """Отказ во входе; текст -- причина. Что из неё показать входящему,
    решает вызывающий: «нет такого логина» подсказывает, кто есть."""


@typing.runtime_checkable
class AuthProvider(typing.Protocol):
    def authenticate(self, login: str, password: str) -> Identity:
        """-> Identity, или Refused с причиной."""

    def lookup(self, login: str) -> "Identity | None":
        """-> Identity, или None -- такого логина нет."""


# ─── чистое: хеш, запись файла, настройка ────────────────────────────────
def _b64(raw):
    return base64.b64encode(raw).decode()


def hash_password(password):
    """Пароль -> `scrypt$N$r$p$соль$ключ` со случайной солью."""
    salt = secrets.token_bytes(SALT)
    key = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"{SCHEME}${N}${R}${P}${_b64(salt)}${_b64(key)}"


def verify_password(password, hashed):
    """Сравнение за постоянное время. Чужая схема, пустой или битый хеш --
    не вход: неизвестное не проверяется «как-нибудь»."""
    try:
        scheme, n, r, p, salt, key = hashed.split("$")
        if scheme != SCHEME:
            return False
        key = base64.b64decode(key)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(key))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, key)


def _identity(login, fields, name="", email=""):
    """Логин и поля MOP_OPERATORS -> Identity по правилам operators.parse:
    те же отказы, одна проверка на оба источника."""
    op = operators.parse(":".join([login, *fields]))[login]
    return Identity(login, op["role"], tuple(op["projects"]), name, email)


def parse_line(line):
    """Строка файла операторов -> (Identity, хеш). Отказ громкий."""
    fields = line.rstrip("\n").split(":")
    if len(fields) != FIELDS:
        raise ValueError(f"operators file: expected login:role:projects:name:email:hash, "
                         f"got {line!r}")
    login, role, projects, name, email, hashed = (f.strip() for f in fields)
    if ";" in login:
        # Разделитель людей в MOP_OPERATORS: логин с ним разобрался бы в двоих.
        raise ValueError(f"operators file: {login!r} is not a login")
    if not hashed:
        # Запись без хеша -- не «без пароля», а вход, который не проверить.
        raise ValueError(f"operators file: {login or line!r} has no password hash")
    # Пустое поле проектов у admin -- «весь пул»; у user operators откажет.
    rights = [role] + ([projects] if projects else [])
    return _identity(login, rights, name, email), hashed


def format_line(who, hashed):
    """(Identity, хеш) -> строка файла. Двоеточие или перевод строки в поле
    разорвали бы запись -- отказ, а не порча."""
    projects = "" if who.role == operators.ADMIN else ",".join(who.projects)
    fields = [who.login, who.role, projects, who.name, who.email, hashed]
    for f in fields:
        if ":" in f or "\n" in f:
            raise ValueError(f"operators file: {who.login}: {f!r} would break the line")
    return ":".join(fields)


def from_setting(setting):
    """MOP_OPERATORS -> [Identity] в порядке настройки."""
    return [Identity(login, op["role"], tuple(op["projects"]))
            for login, op in operators.parse(setting).items()]


def subjects(setting):
    """MOP_OPERATORS -> {логин: права субъектами}: то, что едет плейбукам."""
    return {i.login: operators.permissions(i) for i in from_setting(setting)}


def _conflict(login, sources):
    """Логин и его источники -> строка отказа."""
    if len(set(sources)) == 1:
        return f"login {login} is defined twice in {sources[0]}"
    # Настройка первой: её правят в .env, файл -- на сервере.
    return f"login {login} is defined both in MOP_OPERATORS and in {sources[0]}"


# ─── плоский файл ────────────────────────────────────────────────────────
class PlainFileProvider:
    """Файл операторов плюс переход с MOP_OPERATORS.

    Файл читается на каждый вызов: правка списка людей действует без
    перезапуска того, кто спрашивает. Файла нет -- установка как сегодня,
    только MOP_OPERATORS."""

    def __init__(self, path, setting="", secrets_dir=SECRETS):
        self.path, self.setting, self.secrets_dir = path, setting, secrets_dir

    def _records(self):
        """-> ({логин: (Identity, хеш или None)}, {логин: [источники]}).
        Хеш None -- пароль в файле deploy'я. Логин, определённый дважды,
        уходит во второй словарь и ни в первом, ни в выдаче не участвует."""
        seen = {}
        try:
            with open(self.path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            who, hashed = parse_line(line)
            seen.setdefault(who.login, []).append((self.path, (who, hashed)))
        for who in from_setting(self.setting):
            seen.setdefault(who.login, []).append(("MOP_OPERATORS", (who, None)))
        records = {login: got[0][1] for login, got in seen.items() if len(got) == 1}
        twice = {login: [src for src, _ in got] for login, got in seen.items() if len(got) > 1}
        return records, twice

    def _one(self, login):
        """-> (Identity, хеш) или None. Логин, определённый дважды, -- Refused:
        отказ бьёт по нему одному, а не по провайдеру (#205). При переносе
        людей из MOP_OPERATORS в файл один дубль иначе выключил бы вход всем;
        громкий отказ всего -- в mop deploy, по conflicts()."""
        records, twice = self._records()
        if login in twice:
            raise Refused(_conflict(login, twice[login]))
        return records.get(login)

    def conflicts(self):
        """-> [строка на логин, определённый дважды]; для mop deploy."""
        return [_conflict(login, sources) for login, sources in sorted(self._records()[1].items())]

    def identities(self):
        return [who for who, _ in self._records()[0].values()]

    def lookup(self, login):
        got = self._one(login)
        return got[0] if got else None

    def authenticate(self, login, password):
        got = self._one(login)
        if got is None:
            raise Refused(f"{login}: unknown login")
        who, hashed = got
        if hashed is not None:
            ok = verify_password(password, hashed)
        else:
            # Переход: пароль, который сгенерировал deploy (lookup('password')).
            path = os.path.join(self.secrets_dir, operators.pass_file(login))
            try:
                with open(path) as f:
                    stored = f.read().strip()
            except FileNotFoundError:
                stored = ""
            if not stored:
                raise Refused(f"{login}: no password yet -- run mop deploy")
            ok = hmac.compare_digest(password.encode(), stored.encode())
        if not ok:
            raise Refused(f"{login}: wrong password")
        return who


# ─── выбор провайдера ────────────────────────────────────────────────────
def provider(settings, secrets_dir=SECRETS):
    """Настройки -> провайдер по MOP_AUTH_PROVIDER. Неизвестное имя -- отказ:
    молча упасть на file значило бы пустить по другому списку людей."""
    kind = settings.get("MOP_AUTH_PROVIDER") or "file"
    if kind == "file":
        path = settings.get("MOP_OPERATORS_FILE") or os.path.join(secrets_dir, OPERATORS_FILE)
        return PlainFileProvider(path, settings.get("MOP_OPERATORS", ""), secrets_dir)
    raise ValueError(f"MOP_AUTH_PROVIDER={kind!r}: no such provider; "
                     f"there is only 'file' (LDAP is #208)")
