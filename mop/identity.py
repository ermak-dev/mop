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

from . import busnames, fsutil, operators

SECRETS = os.path.expanduser("~/.config/mop/secrets")
OPERATORS_FILE = "operators"
# Настройки, из которых provider() собирает провайдера.
# Пароль LDAP среди них (#208): он не настройка config.SETTINGS и плейбукам
# не едет, но провайдеру нужен.
SETTINGS = ("MOP_AUTH_PROVIDER", "MOP_OPERATORS", "MOP_OPERATORS_FILE",
            "MOP_LDAP_URL", "MOP_LDAP_BIND_DN", "MOP_LDAP_BIND_PASSWORD", "MOP_LDAP_BASE",
            "MOP_LDAP_LOGIN_ATTR", "MOP_LDAP_NAME_ATTR", "MOP_LDAP_EMAIL_ATTR",
            "MOP_LDAP_GROUP_BASE", "MOP_LDAP_GROUP_FILTER", "MOP_LDAP_ADMIN_GROUP",
            "MOP_LDAP_PROJECT_GROUP", "MOP_LDAP_STARTTLS", "MOP_LDAP_CA_FILE")
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


def profile(source, login):
    """Логин -> {login, name, email}: git identity владельца задания (#167).

    Только имя и почта -- роль, проекты и хеш наружу не идут: глагол
    identity спрашивает узел, и ему нужна подпись коммита, не права. Нет
    логина, нет имени или нет почты -- {login} без них: половина identity
    коммит не спасает, а «нет» отличимо от отказа. Логин, определённый
    дважды, -- Refused, как у lookup."""
    who = source.lookup(login)
    if who and who.name and who.email:
        return {"login": login, "name": who.name, "email": who.email}
    return {"login": login}


# Пароль служебной учётки LDAP на сервере (#214): файлом в копии личностей,
# не окружением юнита -- юнит читаем всем.
BIND_PASSWORD_FILE = "ldap-bind.pass"
BIND_PASSWORD = "MOP_LDAP_BIND_PASSWORD"


def service_settings(get, secrets_dir):
    """Настройки провайдера для сервиса сервера. -> {настройка: значение}.

    get -- config.get: несекретное приходит окружением юнита
    (config.IDENTITY_SCOPED). Пароль LDAP -- из get, если он там есть (на
    контроллере, из .env), иначе из файла secrets_dir/ldap-bind.pass, который
    кладёт deploy. Провайдер ldap без обоих -- отказ с путём файла: сервис
    без каталога -- вход, которого нет."""
    out = {k: get(k) for k in SETTINGS}
    if (out.get("MOP_AUTH_PROVIDER") or "file") != "ldap" or out.get(BIND_PASSWORD):
        return out
    path = os.path.join(secrets_dir, BIND_PASSWORD_FILE)
    try:
        with open(path) as f:
            out[BIND_PASSWORD] = f.read().strip()
    except FileNotFoundError:
        out[BIND_PASSWORD] = ""
    if not out[BIND_PASSWORD]:
        raise ValueError(f"MOP_AUTH_PROVIDER=ldap, but {path} has no bind password "
                         f"-- set MOP_LDAP_BIND_PASSWORD in .env and run mop deploy")
    return out


# ─── правка файла операторов (#218) ──────────────────────────────────────
# Команда `mop user` на сервере. Файл правится строкой на человека: чужие
# строки и комментарии остаются как были, запись -- атомарно и 0600. Отказы
# -- ValueError с причиной, до записи: наполовину правленого файла не бывает.
def operators_path(settings, secrets_dir=SECRETS):
    """Где файл операторов: MOP_OPERATORS_FILE, иначе secrets/operators."""
    return settings.get("MOP_OPERATORS_FILE") or os.path.join(secrets_dir, OPERATORS_FILE)


def person(login, role, projects="", name="", email=""):
    """Поля человека -> Identity по правилам MOP_OPERATORS (operators.parse):
    роль, проекты, имена ролей шины. Отказ громкий."""
    if not busnames.valid_login(login) or any(c in login for c in ":;"):
        # Двоеточие и точка с запятой -- разделители строки файла и настройки.
        raise ValueError(f"{login!r} is not a login")
    if role not in operators.ROLES:
        # Строго: прежняя запись MOP_OPERATORS без роли (`имя:проекты`)
        # прочла бы `root` как проект.
        raise ValueError(f"{login}: role {role or 'missing'}: "
                         f"--role admin, or --role user --projects <p,...>")
    who = _identity(login, [role] + ([projects] if projects else []), name, email)
    format_line(who, "")      # двоеточие в имени или почте -- отказ здесь
    return who


def _lines(path):
    try:
        with open(path) as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def _index(lines, login):
    """Номер строки логина или None."""
    for i, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#") \
                and parse_line(line)[0].login == login:
            return i
    return None


def _save(path, lines):
    fsutil.write_private(path, "".join(l if l.endswith("\n") else l + "\n" for l in lines))


def _in_setting(login, setting):
    if login in operators.parse(setting):
        return (f"{login} is in MOP_OPERATORS, not in the file: "
                f"mop user import moves it there with its password")
    return None


def _found(path, login, setting):
    """-> (строки, номер) логина в файле, иначе ValueError с причиной."""
    lines = _lines(path)
    i = _index(lines, login)
    if i is None:
        raise ValueError(_in_setting(login, setting) or f"{login}: no such login in {path}")
    return lines, i


def add_person(path, who, password, setting=""):
    """Новый человек в файл. Логин уже в файле или в MOP_OPERATORS -- отказ:
    два определения одного логина провайдер отвергает (#205)."""
    if not password:
        raise ValueError("empty password")
    why = _in_setting(who.login, setting)
    if why:
        raise ValueError(why)
    lines = _lines(path)
    if _index(lines, who.login) is not None:
        raise ValueError(f"{who.login} is already in {path}")
    _save(path, lines + [format_line(who, hash_password(password))])


def set_password(path, login, password, setting=""):
    """Новый пароль; остальное в строке как было."""
    if not password:
        raise ValueError("empty password")
    lines, i = _found(path, login, setting)
    who, _ = parse_line(lines[i])
    lines[i] = format_line(who, hash_password(password))
    _save(path, lines)


def remove_person(path, login, setting=""):
    lines, i = _found(path, login, setting)
    _save(path, lines[:i] + lines[i + 1:])


def import_setting(path, setting, secrets_dir=SECRETS):
    """ПЕРЕХОД: MOP_OPERATORS -> файл, с нынешними паролями
    (secrets/nats-op-<логин>.pass -> хеш), ролью и проектами; имени и почты у
    настройки нет. -> [логины]. Всё или ничего: логин уже в файле или без
    пароля -- отказ до записи. Уходит вместе с MOP_OPERATORS (#219): после
    переноса оператор убирает настройку из .env, и прежний operator.json на
    машинах работает без нового mop join -- пароль тот же."""
    people = from_setting(setting)
    if not people:
        raise ValueError("MOP_OPERATORS is empty: nothing to import")
    lines = _lines(path)
    there = [w.login for w in people if _index(lines, w.login) is not None]
    if there:
        raise ValueError(f"already in {path}: {', '.join(there)}")
    add, missing = [], []
    for who in people:
        try:
            with open(os.path.join(secrets_dir, operators.pass_file(who.login))) as f:
                password = f.read().strip()
        except FileNotFoundError:
            password = ""
        if not password:
            missing.append(who.login)
            continue
        add.append(format_line(who, hash_password(password)))
    if missing:
        raise ValueError(f"no password in {secrets_dir} for {', '.join(missing)}: "
                         f"their password is made by mop deploy")
    _save(path, lines + add)
    return [w.login for w in people]


def refresh_copy(path, folder=None):
    """Копия файла операторов для сервисов сервера (natsconf.IDENTITY_DIR,
    её же кладёт deploy). -> записана ли. Каталог над копией -- /etc/nats
    пользователя пула: писать туда может только он; не может (не сервер
    шины, другой пользователь) -- False, копию обновит mop deploy. Переходные
    пароли копии -- дело deploy'я: они из MOP_OPERATORS, а не из файла."""
    from . import natsconf   # лениво: как у server_provider
    folder = folder or natsconf.IDENTITY_DIR
    parent = os.path.dirname(folder)
    if not os.path.isdir(parent) or not os.access(parent, os.W_OK | os.X_OK):
        return False
    try:
        fsutil.make_private_dir(folder)
        with open(path, "rb") as f:
            fsutil.write_private(os.path.join(folder, OPERATORS_FILE), f.read())
    except OSError:
        return False
    return True


# ─── выбор провайдера ────────────────────────────────────────────────────
def provider(settings, secrets_dir=SECRETS):
    """Настройки -> провайдер по MOP_AUTH_PROVIDER. Неизвестное имя -- отказ:
    молча упасть на file значило бы пустить по другому списку людей."""
    kind = settings.get("MOP_AUTH_PROVIDER") or "file"
    if kind == "file":
        return PlainFileProvider(operators_path(settings, secrets_dir),
                                 settings.get("MOP_OPERATORS", ""), secrets_dir)
    if kind == "ldap":
        # Лениво: ldapauth -- поверх identity, а ldap3 -- только при вызове.
        from . import ldapauth
        cfg = ldapauth.settings(settings)
        return ldapauth.LdapProvider(cfg, ldapauth.Ldap3Directory(cfg))
    raise ValueError(f"MOP_AUTH_PROVIDER={kind!r}: no such provider; known: file, ldap")


def server_provider(folder=None):
    """Провайдер сервисов сервера (callout #206, глагол identity #167): по
    настройкам установки, но файлы -- из копии natsconf.IDENTITY_DIR. Файл
    операторов и переходные пароли лежат у контроллера (secrets/), куда
    пользователю пула хода нет; deploy кладёт копию туда, 0600. Секреты
    провайдера -- файлами там же, не окружением юнита: юнит читаем всем.
    Одно место на оба сервиса: иначе callout и identity читали бы разные
    списки людей."""
    from . import config, natsconf   # лениво: identity читают и без них
    folder = folder or natsconf.IDENTITY_DIR
    # Пароль LDAP -- файлом из той же копии (#214), не окружением юнита.
    settings = service_settings(config.get, folder)
    settings["MOP_OPERATORS_FILE"] = os.path.join(folder, OPERATORS_FILE)
    return provider(settings, folder)
