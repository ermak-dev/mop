"""Провайдер личности LDAP (#208, эпик #36). Данные, без печати.

Вторая реализация identity.AuthProvider после плоского файла (#205):
каталог организации отвечает, кто человек и что ему можно. Спрашивать будет
auth callout шины (#206); здесь шины нет.

Вход -- search-then-bind: служебная учётка (MOP_LDAP_BIND_DN) находит запись
по атрибуту логина под MOP_LDAP_BASE, пароль проверяет bind под DN самого
пользователя. Прямой bind по шаблону DN не поддержан: служебная учётка нужна
всё равно -- для lookup без пароля и для групп, -- а поиск работает и там,
где люди разложены по разным OU, и в AD (sAMAccountName), и в OpenLDAP (uid).

Роль и проекты -- из групп. Группы ищутся фильтром MOP_LDAP_GROUP_FILTER под
MOP_LDAP_GROUP_BASE; дефолтный фильтр понимает groupOfNames и
groupOfUniqueNames (member, uniqueMember -- по DN) и posixGroup (memberUid --
по логину); вложенные группы AD не раскрываются. Член группы
MOP_LDAP_ADMIN_GROUP (DN) -- admin; группы, чей cn подходит под
MOP_LDAP_PROJECT_GROUP (`mop-{project}`), дают user на эти проекты. Ни
одной такой группы -- отказ: человек есть в каталоге, но доступа к пулу у
него нет.

Active Directory (#220): доступ там дают одной группой с вложенностью, а не
группами по проектам. Член MOP_LDAP_ACCESS_GROUP (DN) -- user на все проекты
(*). MOP_LDAP_NESTED=ad -- членство проверяется правилом in-chain
(memberOf:1.2.840.113556.1.4.1941:=<DN группы>) поиском записи пользователя,
по запросу на группу: admin, доступ, затем проектные группы, найденные по
шаблону cn. Прямой список групп вложенных не видит. Пусто -- прямое
членство, как было.

Настройки -- в .env установки. Пароль служебной учётки -- секрет: его нет в
config.SETTINGS, потому что настройки едут плейбукам --extra-vars, а ему
там делать нечего; читается он config.get, как GITLAB_TOKEN.

Шифрование обязательно: ldaps:// или ldap:// с MOP_LDAP_STARTTLS=yes;
ldap:// без StartTLS -- отказ, пароли людей шли бы по сети открытым текстом.
Сертификат проверяется всегда -- системным хранилищем или MOP_LDAP_CA_FILE.

ldap3 (чистый python, в MOP_PIP_DEPS) импортируется лениво, внутри
переходника: установке без LDAP он не нужен, а логика провайдера
проверяется над каталогом-заглушкой без него.
"""
import dataclasses
import re

from ..common import config
from . import identity, operators

# Настройки провайдера: обязательные без дефолта и все, что читает settings().
REQUIRED = ("MOP_LDAP_URL", "MOP_LDAP_BIND_DN", "MOP_LDAP_BIND_PASSWORD", "MOP_LDAP_BASE")
NAMES = REQUIRED + ("MOP_LDAP_ACCESS_GROUP", "MOP_LDAP_NESTED",
                    "MOP_LDAP_LOGIN_ATTR", "MOP_LDAP_NAME_ATTR", "MOP_LDAP_EMAIL_ATTR",
                    "MOP_LDAP_GROUP_BASE", "MOP_LDAP_GROUP_FILTER", "MOP_LDAP_ADMIN_GROUP",
                    "MOP_LDAP_PROJECT_GROUP", "MOP_LDAP_STARTTLS", "MOP_LDAP_CA_FILE")
# Логин -- имя пользователя шины и поле строки прав (operators.parse): без разделителей
# разбора (: ; ,), масок субъектов (* >) и пробелов.
LOGIN = re.compile(r"[A-Za-z0-9._@-]+")
PROJECT = r"(?P<project>[A-Za-z0-9._-]+)"
# Правило AD LDAP_MATCHING_RULE_IN_CHAIN: членство с вложенными группами.
IN_CHAIN = "1.2.840.113556.1.4.1941"
NESTED = ("", "ad")
TIMEOUT = 10


@dataclasses.dataclass(frozen=True)
class Settings:
    url: str
    bind_dn: str
    bind_password: str
    base: str
    login_attr: str
    name_attrs: tuple
    email_attr: str
    group_base: str
    group_filter: str
    admin_group: str
    access_group: str
    nested: str
    project_group: str
    starttls: bool
    ca_file: str


def settings(values):
    """{настройка: значение} -> Settings. Пустое -- дефолт config.SETTINGS.
    Отказ громкий и с перечнем: провайдер без каталога -- вход, которого нет."""
    def get(name):
        return (values.get(name) or config.SETTINGS.get(name, "")).strip()

    missing = [n for n in REQUIRED if not get(n)]
    if missing:
        raise ValueError(f"LDAP (MOP_AUTH_PROVIDER=ldap) needs {', '.join(missing)} in .env")
    url, starttls = get("MOP_LDAP_URL"), get("MOP_LDAP_STARTTLS").lower() in ("yes", "true", "1", "on")
    if url.startswith("ldap://") and not starttls:
        raise ValueError(f"MOP_LDAP_URL={url}: passwords would cross the network in clear; "
                         f"use ldaps:// or set MOP_LDAP_STARTTLS=yes")
    if not url.startswith(("ldaps://", "ldap://")):
        raise ValueError(f"MOP_LDAP_URL={url}: expected ldaps://host[:port] or ldap://host[:port]")
    pattern = get("MOP_LDAP_PROJECT_GROUP")
    if pattern.count("{project}") != 1:
        raise ValueError(f"MOP_LDAP_PROJECT_GROUP={pattern!r} must hold {{project}} once, "
                         f"e.g. mop-{{project}}")
    nested = get("MOP_LDAP_NESTED").lower()
    if nested not in NESTED:
        raise ValueError(f"MOP_LDAP_NESTED={nested!r}: expected empty (direct membership) "
                         f"or ad (nested groups, Active Directory)")
    group_filter = get("MOP_LDAP_GROUP_FILTER")
    if "{dn}" not in group_filter and "{login}" not in group_filter:
        raise ValueError(f"MOP_LDAP_GROUP_FILTER={group_filter!r} must name the member "
                         f"by {{dn}} or {{login}}")
    return Settings(
        url=url, bind_dn=get("MOP_LDAP_BIND_DN"), bind_password=get("MOP_LDAP_BIND_PASSWORD"),
        base=get("MOP_LDAP_BASE"), login_attr=get("MOP_LDAP_LOGIN_ATTR"),
        name_attrs=tuple(a.strip() for a in get("MOP_LDAP_NAME_ATTR").split(",") if a.strip()),
        email_attr=get("MOP_LDAP_EMAIL_ATTR"),
        group_base=get("MOP_LDAP_GROUP_BASE") or get("MOP_LDAP_BASE"),
        group_filter=group_filter, admin_group=get("MOP_LDAP_ADMIN_GROUP"),
        access_group=get("MOP_LDAP_ACCESS_GROUP"), nested=nested,
        project_group=pattern, starttls=starttls, ca_file=get("MOP_LDAP_CA_FILE"))


# ─── чистое: фильтры, атрибуты, группы ───────────────────────────────────
def escape(value):
    """Значение в фильтре LDAP (RFC 4515): логин -- не кусок фильтра."""
    out = value.replace("\\", "\\5c")
    for ch, code in (("*", "\\2a"), ("(", "\\28"), (")", "\\29"), ("\0", "\\00")):
        out = out.replace(ch, code)
    return out


def user_filter(attr, login):
    return f"({attr}={escape(login)})"


def group_filter(template, dn, login):
    return template.replace("{dn}", escape(dn)).replace("{login}", escape(login))


def first(attrs, name):
    """Первое непустое значение атрибута; имя без учёта регистра, как в LDAP."""
    for key, value in attrs.items():
        if key.lower() == name.lower():
            values = value if isinstance(value, (list, tuple)) else [value]
            for v in values:
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                if str(v).strip():
                    return str(v).strip()
    return ""


def norm_dn(dn):
    """DN для сравнения: регистр и пробелы вокруг , и = -- не другая запись."""
    return ",".join("=".join(p.strip() for p in rdn.split("=", 1))
                    for rdn in dn.lower().split(","))


def in_chain_filter(login_attr, login, group_dn):
    """Запись пользователя, если он член группы с учётом вложенности (AD)."""
    return f"(&({login_attr}={escape(login)})(memberOf:{IN_CHAIN}:={escape(group_dn)}))"


def project_groups_filter(pattern):
    """Группы AD, чей cn подходит под шаблон проекта: кандидаты для in-chain."""
    prefix, suffix = pattern.split("{project}")
    return f"(&(objectClass=group)(cn={escape(prefix)}*{escape(suffix)}))"


def decide(cfg, member, candidates):
    """Кто человек пулу. member(DN группы) -> bool; candidates -- группы
    [(dn, атрибуты)], чей cn может назвать проект. -> поля роли
    ([admin] | [user, проекты] | [user, *]) или None -- доступа нет.

    Порядок -- от широкого к узкому, и первое да решает: admin, затем
    группа доступа (#220, все проекты), затем проектные группы."""
    if cfg.admin_group and member(cfg.admin_group):
        return [operators.ADMIN]
    if cfg.access_group and member(cfg.access_group):
        return [operators.USER, operators.ALL]
    prefix, suffix = cfg.project_group.split("{project}")
    shape = re.compile(re.escape(prefix) + PROJECT + re.escape(suffix))
    projects = sorted({m.group("project") for dn, attrs in candidates
                       for m in [shape.fullmatch(first(attrs, "cn"))] if m and member(dn)})
    return [operators.USER, ",".join(projects)] if projects else None


def rights(cfg, groups):
    """Прямое членство (#208): группы [(dn, атрибуты)] -> поля или None."""
    mine = {norm_dn(dn) for dn, _ in groups}
    return decide(cfg, lambda dn: norm_dn(dn) in mine, groups)


# ─── провайдер ───────────────────────────────────────────────────────────
class LdapProvider:
    """AuthProvider над каталогом: find_user, groups_of, check (Directory)."""

    def __init__(self, cfg, directory):
        self.cfg, self.directory = cfg, directory

    def _entry(self, login):
        """-> (dn, атрибуты) или None. Две записи на логин -- отказ, а не
        первая попавшаяся: вошёл бы не тот человек."""
        found = self.directory.find_user(login)
        if len(found) > 1:
            raise identity.Refused(f"{login}: ambiguous login, {len(found)} entries in LDAP")
        return found[0] if found else None

    def _identity(self, login, dn, attrs):
        """-> Identity или Refused: нет групп пула, имя роли шины."""
        if self.cfg.nested == "ad":
            # Вложенность AD (#220): членство -- правилом in-chain, по группе
            # на запрос; прямой список групп вложенных не видит.
            d = self.directory
            fields = decide(self.cfg, lambda group: d.member_of(login, group),
                            d.project_groups())
        else:
            fields = rights(self.cfg, self.directory.groups_of(dn, login))
        if fields is None:
            raise identity.Refused(f"{login}: no access -- in none of the pool's LDAP groups")
        name = next((v for v in (first(attrs, a) for a in self.cfg.name_attrs) if v), "")
        try:
            return identity._identity(login, fields, name, first(attrs, self.cfg.email_attr))
        except ValueError as e:
            raise identity.Refused(str(e)) from None

    def authenticate(self, login, password):
        if not LOGIN.fullmatch(login or ""):
            raise identity.Refused(f"{login!r}: not a login")
        if not password:
            # Simple bind с пустым паролем -- анонимный, и многие серверы
            # отвечают на него успехом: до каталога такой вход не доходит.
            raise identity.Refused(f"{login}: empty password")
        entry = self._entry(login)
        if entry is None:
            raise identity.Refused(f"{login}: unknown login")
        dn, attrs = entry
        if not self.directory.check(dn, password):
            raise identity.Refused(f"{login}: wrong password")
        return self._identity(login, dn, attrs)

    def knows(self, login):
        """Есть ли в каталоге запись логина -- для цепочки (#232). Запись без
        доступа -- тоже знает: решать ей, и она откажет; иначе одноимённая
        учётка ниже по цепочке стала бы вторым паролем. Две записи -- знает."""
        if not LOGIN.fullmatch(login or ""):
            return False
        try:
            return self._entry(login) is not None
        except identity.Refused:
            return True

    def lookup(self, login):
        """Без пароля, служебной учёткой. Нет доступа -- не оператор: None."""
        if not LOGIN.fullmatch(login or ""):
            return None
        entry = self._entry(login)
        if entry is None:
            return None
        try:
            return self._identity(login, *entry)
        except identity.Refused:
            return None

    def conflicts(self):
        """Дублей между источниками у каталога нет: один источник (#205)."""
        return []


# ─── переходник на ldap3 ─────────────────────────────────────────────────
class Ldap3Directory:
    """Каталог через ldap3; соединение на вызов -- без залежавшихся сессий.
    server и strategy -- для проверок (MOCK_SYNC)."""

    def __init__(self, cfg, server=None, strategy=None):
        self.cfg, self._server_obj, self._strategy = cfg, server, strategy

    def _ldap3(self):
        try:
            import ldap3
        except ImportError:
            raise identity.Refused("LDAP provider needs ldap3: pip install --user "
                                   "--break-system-packages ldap3 (mop server setup)") from None
        return ldap3

    def _server(self, ldap3):
        if self._server_obj is None:
            import ssl
            tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=self.cfg.ca_file or None)
            self._server_obj = ldap3.Server(self.cfg.url, use_ssl=self.cfg.url.startswith("ldaps://"),
                                            tls=tls, get_info=ldap3.NONE, connect_timeout=TIMEOUT)
        return self._server_obj

    def _bind(self, dn, password):
        """-> связанное соединение или None (неверные креды). Каталог
        недоступен или TLS не сошёлся -- Refused с причиной."""
        ldap3 = self._ldap3()
        from ldap3.core.exceptions import LDAPException
        try:
            conn = ldap3.Connection(self._server(ldap3), user=dn, password=password,
                                    client_strategy=self._strategy or ldap3.SYNC,
                                    receive_timeout=TIMEOUT, raise_exceptions=False)
            if self.cfg.starttls and self._strategy is None:
                conn.open()
                if not conn.start_tls():
                    raise identity.Refused(f"LDAP StartTLS failed: {conn.result}")
            return conn if conn.bind() else None
        except LDAPException as e:
            raise identity.Refused(f"LDAP {self.cfg.url}: {type(e).__name__}: {e}") from None

    def _search(self, base, filt, attributes):
        conn = self._bind(self.cfg.bind_dn, self.cfg.bind_password)
        if conn is None:
            raise identity.Refused(f"LDAP service bind as {self.cfg.bind_dn} failed: "
                                   f"check MOP_LDAP_BIND_DN and MOP_LDAP_BIND_PASSWORD")
        try:
            conn.search(base, filt, attributes=list(attributes))
            return [(e["dn"], dict(e.get("attributes") or {})) for e in conn.response or []
                    if e.get("type") == "searchResEntry"]
        finally:
            conn.unbind()

    def find_user(self, login):
        cfg = self.cfg
        return self._search(cfg.base, user_filter(cfg.login_attr, login),
                            [cfg.login_attr, *cfg.name_attrs, cfg.email_attr])

    def groups_of(self, dn, login):
        return self._search(self.cfg.group_base, group_filter(self.cfg.group_filter, dn, login),
                            ["cn"])

    def member_of(self, login, group_dn):
        """Член ли группы с учётом вложенности (AD in-chain, #220)."""
        cfg = self.cfg
        return bool(self._search(cfg.base, in_chain_filter(cfg.login_attr, login, group_dn),
                                 [cfg.login_attr]))

    def project_groups(self):
        return self._search(self.cfg.group_base, project_groups_filter(self.cfg.project_group),
                            ["cn"])

    def check(self, dn, password):
        conn = self._bind(dn, password)
        if conn is None:
            return False
        conn.unbind()
        return True
