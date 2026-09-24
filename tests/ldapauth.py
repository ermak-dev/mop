#!/usr/bin/env python3
"""Провайдер личности LDAP: python3 tests/ldapauth.py

Второй источник личностей после плоского файла (#205, эпик #36): требование
оператора -- личность есть пользователь, прошедший проверку в NATS, через
провайдера с плоским файлом и LDAP (#208). Шину он не трогает: спрашивать
провайдера будет auth callout (#206).

Сети нет. Логика провайдера -- над каталогом-заглушкой (Directory: найти
пользователя, его группы, проверить пароль); переходник на ldap3 -- на его
MOCK_SYNC, если ldap3 есть на машине, иначе пропуск hermetic.skip (#229):
строка в выводе, а при MOP_TESTS_STRICT=1 -- провал файла.

HYPOTHESIS: подключить каталог организации некуда -- личности есть только в
файле операторов и MOP_OPERATORS.
SOLUTION: mop/ldapauth.py -- LdapProvider (search-then-bind: служебная
учётка находит запись по атрибуту логина, пароль проверяет bind под DN
пользователя), имя и почта из настраиваемых атрибутов, роль и проекты из
групп (группа admin по DN, проектные -- по шаблону cn). MOP_AUTH_PROVIDER=ldap
выбирает его; настройки -- в .env, пароль служебной учётки не едет
плейбукам. deploy отказывает до плейбука, если настройки LDAP неполны.
STATUS: FIXED — see #208
"""
import dataclasses
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import identity, ldapauth  # noqa: E402
from mop.identity import Identity  # noqa: E402

BASE = "dc=example,dc=dev"
ADMINS = "cn=mop-admins,ou=groups,dc=example,dc=dev"
SETTINGS = {"MOP_AUTH_PROVIDER": "ldap",
            "MOP_LDAP_URL": "ldaps://ldap.example.dev",
            "MOP_LDAP_BIND_DN": "cn=mop,ou=services,dc=example,dc=dev",
            "MOP_LDAP_BIND_PASSWORD": "svc-pw",
            "MOP_LDAP_BASE": BASE,
            "MOP_LDAP_ADMIN_GROUP": ADMINS}


class Stub:
    """Каталог в памяти: {логин: (dn, атрибуты, пароль)}, {dn: [группы]}."""

    def __init__(self, users, groups):
        self.users, self.groups, self.checked = users, groups, []

    def find_user(self, login):
        return [(dn, attrs) for l, (dn, attrs, _) in self.users.items() if l == login]

    def groups_of(self, dn, login):
        return self.groups.get(dn, [])

    def check(self, dn, password):
        self.checked.append(dn)
        return any(d == dn and pw == password for d, _, pw in self.users.values())


def group(cn, ou="groups"):
    return (f"cn={cn},ou={ou},{BASE}", {"cn": [cn]})


def udn(login):
    return f"uid={login},ou=people,{BASE}"


USERS = {
    "anton": (udn("anton"), {"displayName": ["Антон Ермак"], "cn": ["anton"],
                             "mail": ["anton@example.dev"]}, "a-pw"),
    "ivan": (udn("ivan"), {"cn": ["Иван Петров"], "mail": ["ivan@example.dev"]}, "i-pw"),
    "olga": (udn("olga"), {"cn": ["Olga"]}, "o-pw"),
    "service": (udn("service"), {"cn": ["svc"]}, "s-pw"),
}
GROUPS = {
    # Admin по DN, регистр и пробелы у запятых -- не другая группа.
    udn("anton"): [("CN=mop-admins, OU=groups,DC=example,DC=dev", {"cn": ["mop-admins"]}),
                   group("mop-rugent")],
    udn("ivan"): [group("mop-rugent"), group("mop-cloudpub"), group("staff")],
    udn("olga"): [group("staff")],
    udn("service"): [group("mop-rugent")],
}


def provider(**over):
    cfg = ldapauth.settings({**SETTINGS, **over})
    return ldapauth.LdapProvider(cfg, Stub(dict(USERS), dict(GROUPS)))


def refused(call, *args):
    try:
        call(*args)
    except identity.Refused as e:
        return str(e)
    return None


def check_authenticate():
    out = []
    p = provider()
    if not isinstance(p, identity.AuthProvider):
        out.append("LdapProvider must satisfy AuthProvider")
    got = p.authenticate("anton", "a-pw")
    if got != Identity("anton", "admin", ("*",), "Антон Ермак", "anton@example.dev"):
        out.append(f"admin group -> admin, displayName before cn, mail: {got}")
    got = p.authenticate("ivan", "i-pw")
    if got != Identity("ivan", "user", ("cloudpub", "rugent"), "Иван Петров", "ivan@example.dev"):
        out.append(f"project groups -> user of those projects, cn as name: {got}")
    for login, pw, why in (("ivan", "wrong", "wrong password"),
                           ("nobody", "x", "unknown login"),
                           # Пустой пароль в simple bind -- анонимный вход, и
                           # многие серверы отвечают на него успехом.
                           ("ivan", "", "empty password"),
                           ("olga", "o-pw", "no access"),
                           # Учётка каталога с именем роли шины -- не человек.
                           ("service", "s-pw", "role on the bus")):
        e = refused(p.authenticate, login, pw)
        if e is None or why not in e:
            out.append(f"{login}/{pw!r} must be refused with {why!r}, got {e!r}")
    stub = Stub(dict(USERS), dict(GROUPS))
    ldapauth.LdapProvider(ldapauth.settings(SETTINGS), stub).authenticate("ivan", "i-pw")
    if stub.checked != [udn("ivan")]:
        out.append(f"the password is checked by a bind as the user's own DN: {stub.checked}")
    stub = Stub(dict(USERS), dict(GROUPS))
    refused(ldapauth.LdapProvider(ldapauth.settings(SETTINGS), stub).authenticate, "ivan", "")
    if stub.checked:
        out.append("an empty password must never reach the directory")
    # Два пользователя на один логин -- отказ, а не первый попавшийся.
    twin = Stub({"ivan": USERS["ivan"]}, dict(GROUPS))
    twin.find_user = lambda login: [(udn("ivan"), {}), (f"uid=ivan,ou=other,{BASE}", {})]
    e = refused(ldapauth.LdapProvider(ldapauth.settings(SETTINGS), twin).authenticate, "ivan", "i-pw")
    if e is None or "ambiguous" not in e:
        out.append(f"two entries for one login must be refused: {e!r}")
    # Логин с символами разбора (MOP_OPERATORS, субъекты) -- отказ до каталога.
    for bad in ("a:b", "a;b", "a,b", "*", "a b", ""):
        if refused(p.authenticate, bad, "x") is None:
            out.append(f"login {bad!r} must be refused")
    return out


def check_mapping():
    out = []
    # Атрибуты и шаблон групп настраиваются.
    p = provider(MOP_LDAP_NAME_ATTR="cn", MOP_LDAP_EMAIL_ATTR="mail",
                 MOP_LDAP_PROJECT_GROUP="pool-{project}-users", MOP_LDAP_ADMIN_GROUP="")
    stub = p.directory
    stub.groups[udn("ivan")] = [group("pool-mop-users"), group("mop-rugent")]
    got = p.authenticate("ivan", "i-pw")
    if got != Identity("ivan", "user", ("mop",), "Иван Петров", "ivan@example.dev"):
        out.append(f"project pattern pool-{{project}}-users: {got}")
    # Без группы admin в настройках admin не бывает никто.
    stub.groups[udn("anton")] = [group("mop-admins")]
    if refused(p.authenticate, "anton", "a-pw") is None:
        out.append("without MOP_LDAP_ADMIN_GROUP nobody is admin, and no project is no access")
    return out


def check_lookup():
    out = []
    p = provider()
    if p.lookup("ivan") != Identity("ivan", "user", ("cloudpub", "rugent"),
                                    "Иван Петров", "ivan@example.dev"):
        out.append(f"lookup -> {p.lookup('ivan')}")
    if p.lookup("nobody") is not None:
        out.append("lookup of an unknown login -> None")
    if p.lookup("olga") is not None:
        out.append("lookup of a user with no access -> None: not an operator")
    if p.directory.checked:
        out.append("lookup must not bind as the user")
    return out


def check_settings():
    """Неполные настройки -- громкий отказ с именами; TLS обязателен."""
    out = []
    try:
        ldapauth.settings({"MOP_AUTH_PROVIDER": "ldap"})
        out.append("empty ldap settings must be refused")
    except ValueError as e:
        for name in ("MOP_LDAP_URL", "MOP_LDAP_BIND_DN", "MOP_LDAP_BIND_PASSWORD", "MOP_LDAP_BASE"):
            if name not in str(e):
                out.append(f"the refusal must name {name}: {e}")
    for over, why in (({"MOP_LDAP_URL": "ldap://ldap.example.dev"}, "in clear"),
                      ({"MOP_LDAP_URL": "http://ldap.example.dev"}, "ldaps://"),
                      ({"MOP_LDAP_PROJECT_GROUP": "mop-users"}, "{project}"),
                      ({"MOP_LDAP_GROUP_FILTER": "(member=x)"}, "{dn}")):
        try:
            ldapauth.settings({**SETTINGS, **over})
            out.append(f"{over} must be refused")
        except ValueError as e:
            if why not in str(e):
                out.append(f"{over}: the refusal must say {why!r}: {e}")
    cfg = ldapauth.settings({**SETTINGS, "MOP_LDAP_URL": "ldap://ldap.example.dev",
                             "MOP_LDAP_STARTTLS": "yes"})
    if not cfg.starttls:
        out.append("ldap:// with StartTLS is accepted")
    cfg = ldapauth.settings(SETTINGS)
    if (cfg.login_attr, cfg.name_attrs, cfg.email_attr, cfg.group_base) != \
            ("uid", ("displayName", "cn"), "mail", BASE):
        out.append(f"defaults: uid, displayName then cn, mail, groups under the base: {cfg}")
    # Пароль служебной учётки -- секрет .env: плейбукам он не едет.
    from mop import config, playvars
    if "MOP_LDAP_BIND_PASSWORD" in config.SETTINGS:
        out.append("MOP_LDAP_BIND_PASSWORD must not be a setting: settings ride to the playbooks")
    if "MOP_LDAP_BIND_PASSWORD" not in identity.SETTINGS:
        out.append("the provider must be built with the LDAP password too")
    if "MOP_LDAP_BIND_PASSWORD" in playvars.playbook_vars():
        out.append("the LDAP password must not reach --extra-vars")
    return out


def check_choice():
    out = []
    p = identity.provider(SETTINGS)
    if not isinstance(p, ldapauth.LdapProvider):
        out.append(f"MOP_AUTH_PROVIDER=ldap must give LdapProvider: {p}")
    from mop.cli.pool import deploy
    got = deploy.operator_refusals({"MOP_AUTH_PROVIDER": "ldap"})
    if len(got) != 1 or "MOP_LDAP_URL" not in got[0]:
        out.append(f"deploy must refuse incomplete LDAP settings: {got}")
    if deploy.operator_refusals(SETTINGS):
        out.append(f"complete LDAP settings must not stop deploy: {deploy.operator_refusals(SETTINGS)}")
    from mop import deps
    if "ldap3" not in deps.PIP:
        out.append("ldap3 must be in MOP_PIP_DEPS")
    return out


def check_filters():
    """Фильтры строятся с экранированием (RFC 4515): логин -- не фильтр."""
    out = []
    got = ldapauth.user_filter("uid", "a*)(uid=b")
    if got != "(uid=a\\2a\\29\\28uid=b)":
        out.append(f"user filter must escape: {got}")
    got = ldapauth.group_filter("(|(member={dn})(memberUid={login}))", "uid=x\\,y,dc=z", "x")
    if got != "(|(member=uid=x\\5c,y,dc=z)(memberUid=x))":
        out.append(f"group filter must escape: {got}")
    return out


def check_ldap3():
    """Переходник на ldap3 -- на MOCK_SYNC: поиск, группы, bind."""
    try:
        import ldap3
    except ImportError:
        hermetic.skip("the ldap3 adapter", "no ldap3 on this machine -- "
                      "only the provider over a stub directory is checked")
        return []
    out = []
    server = ldap3.Server("mock")
    cfg = ldapauth.settings(SETTINGS)
    d = ldapauth.Ldap3Directory(cfg, server=server, strategy=ldap3.MOCK_SYNC)
    seed = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    add = seed.strategy.add_entry
    add(SETTINGS["MOP_LDAP_BIND_DN"], {"objectClass": "person", "userPassword": "svc-pw"})
    add(udn("ivan"), {"objectClass": "inetOrgPerson", "uid": "ivan", "cn": "Иван Петров",
                      "mail": "ivan@example.dev", "userPassword": "i-pw"})
    add(udn("anton"), {"objectClass": "inetOrgPerson", "uid": "anton", "cn": "anton",
                       "displayName": "Антон Ермак", "userPassword": "a-pw"})
    add(f"cn=mop-rugent,ou=groups,{BASE}", {"objectClass": "groupOfNames", "cn": "mop-rugent",
                                             "member": [udn("ivan")]})
    add(f"cn=mop-cloudpub,ou=groups,{BASE}", {"objectClass": "posixGroup", "cn": "mop-cloudpub",
                                               "memberUid": "ivan"})
    add(ADMINS, {"objectClass": "groupOfNames", "cn": "mop-admins", "member": [udn("anton")]})
    p = ldapauth.LdapProvider(cfg, d)
    got = p.authenticate("ivan", "i-pw")
    if got != Identity("ivan", "user", ("cloudpub", "rugent"), "Иван Петров", "ivan@example.dev"):
        out.append(f"ldap3: ivan -> {got}")
    if p.authenticate("anton", "a-pw") != Identity("anton", "admin", ("*",), "Антон Ермак"):
        out.append(f"ldap3: anton -> {p.lookup('anton')}")
    for login, pw in (("ivan", "wrong"), ("nobody", "x")):
        if refused(p.authenticate, login, pw) is None:
            out.append(f"ldap3: {login}/{pw} must be refused")
    if p.lookup("ivan") is None or p.lookup("nobody") is not None:
        out.append("ldap3: lookup")
    # Служебная учётка с чужим паролем -- отказ с причиной, не «нет логина».
    bad = ldapauth.LdapProvider(cfg, ldapauth.Ldap3Directory(
        dataclasses.replace(cfg, bind_password="no"), server=server, strategy=ldap3.MOCK_SYNC))
    e = refused(bad.authenticate, "ivan", "i-pw")
    if e is None or "service" not in e:
        out.append(f"a broken service bind must say so: {e!r}")
    return out


# HYPOTHESIS (#214): с MOP_AUTH_PROVIDER=ldap и callout сервис на сервере
# (учётка пула) видит только окружение юнита (SERVER_SCOPED) и копию
# личностей; MOP_LDAP_* до него не доезжают, а пароль служебной учётки
# намеренно вне config.SETTINGS -- провайдер в сервисе не собирается.
# SOLUTION: несекретные MOP_LDAP_* -- одним списком config.IDENTITY_SCOPED в
# окружение mop-callout и mop-bootstrap; пароль -- файлом 0600 в
# /etc/nats/identity (пишет deploy из .env), и identity.service_settings
# берёт его оттуда, когда окружение его не несёт. На контроллере источник --
# по-прежнему config.get.
# STATUS: FIXED — see #214
def check_service_settings_214():
    import tempfile
    from mop import config
    out = []
    fn = getattr(identity, "service_settings", None)
    if fn is None:
        return ["identity.service_settings is missing"]
    unit = {k: v for k, v in SETTINGS.items() if k != "MOP_LDAP_BIND_PASSWORD"}
    root = tempfile.mkdtemp(prefix="mop-test-ldap-")
    path = os.path.join(root, identity.BIND_PASSWORD_FILE)
    try:
        fn(unit.get, root)
        out.append("ldap without the bind password file must be refused")
    except ValueError as e:
        if path not in str(e):
            out.append(f"the refusal must name the file {path}: {e}")
    with open(path, "w") as f:
        f.write("svc-pw\n")
    got = fn(unit.get, root)
    if ldapauth.settings(got).bind_password != "svc-pw":
        out.append(f"the provider must take the bind password from the file: {got}")
    if not isinstance(identity.provider(got, root), ldapauth.LdapProvider):
        out.append("the service must build an LdapProvider from the unit env and the file")
    on_controller = dict(SETTINGS, MOP_LDAP_BIND_PASSWORD="from-env")
    if fn(on_controller.get, root)["MOP_LDAP_BIND_PASSWORD"] != "from-env":
        out.append("where config.get has the password (the controller), it wins over the file")
    plain = {"MOP_AUTH_PROVIDER": "file", "MOP_OPERATORS_FILE": "/srv/operators"}
    try:
        got = fn(plain.get, tempfile.mkdtemp())
        if got.get("MOP_OPERATORS_FILE") != "/srv/operators":
            out.append(f"the file provider's settings must pass through: {got}")
    except ValueError as e:
        out.append(f"the file provider needs no LDAP password file: {e}")
    # Один список несекретных настроек личности на оба сервиса сервера.
    scoped = getattr(config, "IDENTITY_SCOPED", ())
    # MOP_OPERATORS ушёл (#219): людей даёт только провайдер.
    want = {"MOP_AUTH_PROVIDER"} | (set(ldapauth.NAMES) - {"MOP_LDAP_BIND_PASSWORD"})
    if set(scoped) != want:
        out.append(f"config.IDENTITY_SCOPED must be the provider's non-secret settings: "
                   f"{sorted(set(scoped) ^ want)} differ")
    for unit_name in ("mop-callout", "mop-bootstrap"):
        missing = set(scoped) - set(config.SERVER_SCOPED.get(unit_name, ()))
        if not scoped or missing:
            out.append(f"{unit_name} must get every identity setting: missing {sorted(missing)}")
    for unit_name, names in config.SERVER_SCOPED.items():
        if "MOP_LDAP_BIND_PASSWORD" in names:
            out.append(f"{unit_name}: the LDAP password must never be in a unit's env")
    # Как пароль едет прогону: окружением процесса ansible, не аргументом,
    # и только когда провайдер -- ldap.
    from mop.cli import lib
    penv = getattr(lib, "play_env", None)
    if penv is None:
        out.append("lib.play_env is missing")
    else:
        if penv(SETTINGS.get).get("MOP_LDAP_BIND_PASSWORD") != "svc-pw":
            out.append("with ldap, the play's env must carry the bind password")
        if "MOP_LDAP_BIND_PASSWORD" in penv({"MOP_AUTH_PROVIDER": "file",
                                             "MOP_LDAP_BIND_PASSWORD": "x"}.get):
            out.append("without ldap, the bind password must not reach the play at all")
    return out


# ── #220: AD -- группа доступа ко всем проектам и вложенные группы ────────
# HYPOTHESIS: на AD установки rumop доступ даёт одна группа (полным DN) с
# вложенностью, групп mop-<проект> нет; провайдер понимал только admin по DN,
# проектные группы по шаблону cn и прямое членство -- каждому «no access».
# SOLUTION: MOP_LDAP_ACCESS_GROUP (DN) -> user на все проекты (*);
# MOP_LDAP_NESTED=ad -> членство проверяет правило in-chain
# (memberOf:1.2.840.113556.1.4.1941:=<DN>) поиском записи пользователя, по
# группе на запрос; проектные группы -- их DN, найденные по шаблону cn.
# Дефолт (пусто) -- поведение #208. MOCK_SYNC ldap3 правило in-chain не
# исполняет (поиск с ним молча пуст), поэтому фильтр и переходник проверены
# на заглушке, не на mock.
# STATUS: FIXED — see #220
ACCESS = "CN=Pool Users,OU=Access,DC=corp,DC=example"
AD_ADMINS = "CN=Pool Admins,OU=Access,DC=corp,DC=example"


class AdStub(Stub):
    """Каталог AD: членство -- с вложенностью, как его видит in-chain."""

    def __init__(self, users, chain, projects):
        super().__init__(users, {})
        self.chain, self.projects, self.asked = chain, projects, []

    def groups_of(self, dn, login):
        raise AssertionError("nested=ad must not read direct membership")

    def member_of(self, login, group):
        self.asked.append(group)
        return any(ldapauth.norm_dn(group) == ldapauth.norm_dn(g)
                   for g in self.chain.get(login, ()))

    def project_groups(self):
        return list(self.projects)


def check_ad_groups_220():
    out = []
    # Прямое членство (дефолт): группа доступа -- все проекты; с admin --
    # admin; с проектными -- всё равно все.
    p = provider(MOP_LDAP_ACCESS_GROUP=ACCESS)
    p.directory.groups[udn("olga")] = [(ACCESS.lower(), {"cn": ["Pool Users"]})]
    p.directory.groups[udn("ivan")] = [(ACCESS, {"cn": ["Pool Users"]}), group("mop-rugent")]
    p.directory.groups[udn("anton")] += [(ACCESS, {"cn": ["Pool Users"]})]
    for login, pw, want in (("olga", "o-pw", ("user", ("*",))),
                            ("ivan", "i-pw", ("user", ("*",))),
                            ("anton", "a-pw", ("admin", ("*",)))):
        got = p.authenticate(login, pw)
        if (got.role, got.projects) != want:
            out.append(f"direct: {login} -> {got.role} {got.projects}, wanted {want}")
    # Без группы доступа в настройках -- как в #208: olga без доступа.
    if refused(provider().authenticate, "olga", "o-pw") is None:
        out.append("without MOP_LDAP_ACCESS_GROUP, staff alone is still no access")

    # Вложенность AD: членство спрашивается in-chain, прямой список не читается.
    ad = {**SETTINGS, "MOP_LDAP_NESTED": "ad", "MOP_LDAP_ACCESS_GROUP": ACCESS,
          "MOP_LDAP_ADMIN_GROUP": AD_ADMINS, "MOP_LDAP_LOGIN_ATTR": "sAMAccountName"}
    chain = {"anton": [AD_ADMINS, ACCESS], "ivan": [ACCESS],
             "petr": [f"CN=mop-rugent,OU=G,{BASE}"], "olga": []}
    projects = [(f"CN=mop-rugent,OU=G,{BASE}", {"cn": ["mop-rugent"]}),
                (f"CN=mop-cloudpub,OU=G,{BASE}", {"cn": ["mop-cloudpub"]})]
    users = dict(USERS, petr=(udn("petr"), {"cn": ["Petr"]}, "p-pw"))
    stub = AdStub(users, chain, projects)
    pa = ldapauth.LdapProvider(ldapauth.settings(ad), stub)
    for login, pw, want in (("anton", "a-pw", ("admin", ("*",))),
                            ("ivan", "i-pw", ("user", ("*",))),
                            ("petr", "p-pw", ("user", ("rugent",)))):
        try:
            got = pa.authenticate(login, pw)
            if (got.role, got.projects) != want:
                out.append(f"ad: {login} -> {got.role} {got.projects}, wanted {want}")
        except Exception as e:  # noqa: BLE001
            out.append(f"ad: {login} -> {type(e).__name__}: {e}")
    e = refused(pa.authenticate, "olga", "o-pw")
    if e is None or "no access" not in e:
        out.append(f"ad: in no group is no access, got {e!r}")
    if pa.lookup("ivan") is None or pa.lookup("olga") is not None:
        out.append("ad: lookup follows the same membership")
    # admin решает первым: при admin группу доступа не спрашиваем.
    stub.asked.clear()
    pa.authenticate("anton", "a-pw")
    if stub.asked != [AD_ADMINS]:
        out.append(f"ad: admin first, nothing after it: asked {stub.asked}")

    # Настройки: неизвестный режим вложенности -- отказ с именем значения.
    try:
        ldapauth.settings({**SETTINGS, "MOP_LDAP_NESTED": "deep"})
        out.append("MOP_LDAP_NESTED=deep must be refused")
    except ValueError as e:
        if "deep" not in str(e) or "ad" not in str(e):
            out.append(f"the refusal must name the value and the choice: {e}")
    cfg = ldapauth.settings(SETTINGS)
    if (cfg.access_group, cfg.nested) != ("", ""):
        out.append(f"defaults: no access group, direct membership: {cfg}")

    # Фильтры: in-chain по записи пользователя, DN и логин экранированы.
    got = ldapauth.in_chain_filter("sAMAccountName", "a*b", "CN=mop (all),OU=G,DC=x")
    want = "(&(sAMAccountName=a\\2ab)(memberOf:1.2.840.113556.1.4.1941:=CN=mop \\28all\\29,OU=G,DC=x))"
    if got != want:
        out.append(f"in_chain_filter -> {got}, wanted {want}")
    for pattern, want in (("mop-{project}", "(&(objectClass=group)(cn=mop-*))"),
                          ("pool-{project}-users", "(&(objectClass=group)(cn=pool-*-users))"),
                          ("a*{project}", "(&(objectClass=group)(cn=a\\2a*))")):
        if ldapauth.project_groups_filter(pattern) != want:
            out.append(f"project_groups_filter({pattern}) -> "
                       f"{ldapauth.project_groups_filter(pattern)}, wanted {want}")

    # Переходник: какие поиски он шлёт (mock правило in-chain не исполняет).
    acfg = ldapauth.settings({**ad, "MOP_LDAP_GROUP_BASE": f"OU=G,{BASE}"})
    d = ldapauth.Ldap3Directory(acfg)
    sent = []
    d._search = lambda base, filt, attrs: sent.append((base, filt)) or (
        [("CN=x", {})] if "Pool Users" in filt else [])
    if d.member_of("ivan", ACCESS) is not True or d.member_of("ivan", AD_ADMINS) is not False:
        out.append("adapter member_of: an entry found is membership, none is not")
    if sent[0] != (BASE, ldapauth.in_chain_filter("sAMAccountName", "ivan", ACCESS)):
        out.append(f"adapter member_of searches the user under the base: {sent[0]}")
    sent.clear()
    d.project_groups()
    if sent != [(f"OU=G,{BASE}", "(&(objectClass=group)(cn=mop-*))")]:
        out.append(f"adapter project_groups searches the group base: {sent}")
    return out


def check_knows_232():
    """Звено цепочки (#232): знает логин -- в каталоге есть его запись, даже
    без доступа (иначе одноимённая учётка ниже стала бы вторым паролем).
    STATUS: FIXED — see #232"""
    out = []
    p = provider()
    for login, want in (("anton", True), ("olga", True), ("nobody", False), ("a*", False)):
        if p.knows(login) is not want:
            out.append(f"knows({login!r}) must be {want}")
    if p.directory.checked:
        out.append(f"knows must never bind as the user: {p.directory.checked}")
    return out


def main():
    failed = []
    for check in (check_authenticate, check_mapping, check_lookup, check_settings,
                  check_choice, check_filters, check_ldap3, check_service_settings_214,
                  check_ad_groups_220, check_knows_232):
        try:
            lines = check()
        except Exception as e:  # noqa: BLE001 -- падение проверки -- тоже провал
            lines = [f"raised {type(e).__name__}: {e}"]
        failed += [f"FAIL {check.__name__}: {l}" for l in lines]
    if failed:
        print("\n".join(failed))
    print("ldapauth: FAILED" if failed else "ldapauth: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
