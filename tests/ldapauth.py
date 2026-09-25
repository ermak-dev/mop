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
SOLUTION: mop/server/ldapauth.py -- LdapProvider (search-then-bind: служебная
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
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import identity, ldapauth  # noqa: E402
from mop.server.identity import Identity  # noqa: E402

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


def check_authenticate(c):
    p = provider()
    c.check("LdapProvider must satisfy AuthProvider", isinstance(p, identity.AuthProvider))
    c.expect("admin group -> admin, displayName before cn, mail", p.authenticate("anton", "a-pw"),
             Identity("anton", "admin", ("*",), "Антон Ермак", "anton@example.dev"))
    c.expect("project groups -> user of those projects, cn as name",
             p.authenticate("ivan", "i-pw"),
             Identity("ivan", "user", ("cloudpub", "rugent"), "Иван Петров", "ivan@example.dev"))
    for login, pw, why in (("ivan", "wrong", "wrong password"),
                           ("nobody", "x", "unknown login"),
                           # Пустой пароль в simple bind -- анонимный вход, и
                           # многие серверы отвечают на него успехом.
                           ("ivan", "", "empty password"),
                           ("olga", "o-pw", "no access"),
                           # Учётка каталога с именем роли шины -- не человек.
                           ("service", "s-pw", "role on the bus")):
        e = refused(p.authenticate, login, pw)
        c.check(f"{login}/{pw!r} must be refused with {why!r}", not (e is None or why not in e),
                f"got {e!r}")
    stub = Stub(dict(USERS), dict(GROUPS))
    ldapauth.LdapProvider(ldapauth.settings(SETTINGS), stub).authenticate("ivan", "i-pw")
    c.expect("the password is checked by a bind as the user's own DN", stub.checked, [udn("ivan")])
    stub = Stub(dict(USERS), dict(GROUPS))
    refused(ldapauth.LdapProvider(ldapauth.settings(SETTINGS), stub).authenticate, "ivan", "")
    c.check("an empty password must never reach the directory", not (stub.checked))
    # Два пользователя на один логин -- отказ, а не первый попавшийся.
    twin = Stub({"ivan": USERS["ivan"]}, dict(GROUPS))
    twin.find_user = lambda login: [(udn("ivan"), {}), (f"uid=ivan,ou=other,{BASE}", {})]
    e = refused(ldapauth.LdapProvider(ldapauth.settings(SETTINGS), twin).authenticate, "ivan", "i-pw")
    c.check("two entries for one login must be refused", not (e is None or "ambiguous" not in e),
            repr(e))
    # Логин с символами разбора (MOP_OPERATORS, субъекты) -- отказ до каталога.
    for bad in ("a:b", "a;b", "a,b", "*", "a b", ""):
        c.check(f"login {bad!r} must be refused", not (refused(p.authenticate, bad, "x") is None))


def check_mapping(c):
    # Атрибуты и шаблон групп настраиваются.
    p = provider(MOP_LDAP_NAME_ATTR="cn", MOP_LDAP_EMAIL_ATTR="mail",
                 MOP_LDAP_PROJECT_GROUP="pool-{project}-users", MOP_LDAP_ADMIN_GROUP="")
    stub = p.directory
    stub.groups[udn("ivan")] = [group("pool-mop-users"), group("mop-rugent")]
    c.expect("project pattern pool-{project}-users", p.authenticate("ivan", "i-pw"),
             Identity("ivan", "user", ("mop",), "Иван Петров", "ivan@example.dev"))
    # Без группы admin в настройках admin не бывает никто.
    stub.groups[udn("anton")] = [group("mop-admins")]
    c.check("without MOP_LDAP_ADMIN_GROUP nobody is admin, and no project is no access",
            not (refused(p.authenticate, "anton", "a-pw") is None))


def check_lookup(c):
    p = provider()
    c.expect("lookup", p.lookup("ivan"), Identity("ivan", "user", ("cloudpub", "rugent"),
                                                  "Иван Петров", "ivan@example.dev"))
    c.check("lookup of an unknown login -> None", not (p.lookup("nobody") is not None))
    c.check("lookup of a user with no access -> None: not an operator",
            not (p.lookup("olga") is not None))
    c.check("lookup must not bind as the user", not (p.directory.checked))


def check_settings(c):
    """Неполные настройки -- громкий отказ с именами; TLS обязателен."""
    try:
        ldapauth.settings({"MOP_AUTH_PROVIDER": "ldap"})
        c.fail("empty ldap settings must be refused")
    except ValueError as e:
        for name in ("MOP_LDAP_URL", "MOP_LDAP_BIND_DN", "MOP_LDAP_BIND_PASSWORD", "MOP_LDAP_BASE"):
            c.check(f"the refusal must name {name}", not (name not in str(e)), e)
    for over, why in (({"MOP_LDAP_URL": "ldap://ldap.example.dev"}, "in clear"),
                      ({"MOP_LDAP_URL": "http://ldap.example.dev"}, "ldaps://"),
                      ({"MOP_LDAP_PROJECT_GROUP": "mop-users"}, "{project}"),
                      ({"MOP_LDAP_GROUP_FILTER": "(member=x)"}, "{dn}")):
        try:
            ldapauth.settings({**SETTINGS, **over})
            c.fail(f"{over} must be refused")
        except ValueError as e:
            c.check(f"{over}: the refusal must say {why!r}", not (why not in str(e)), e)
    cfg = ldapauth.settings({**SETTINGS, "MOP_LDAP_URL": "ldap://ldap.example.dev",
                             "MOP_LDAP_STARTTLS": "yes"})
    c.check("ldap:// with StartTLS is accepted", not (not cfg.starttls))
    cfg = ldapauth.settings(SETTINGS)
    c.expect("defaults: uid, displayName then cn, mail, groups under the base",
             (cfg.login_attr, cfg.name_attrs, cfg.email_attr, cfg.group_base),
             ("uid", ("displayName", "cn"), "mail", BASE))
    # Пароль служебной учётки -- секрет .env: плейбукам он не едет.
    from mop.common import config
    from mop.server import playvars
    c.check("MOP_LDAP_BIND_PASSWORD must not be a setting: settings ride to the playbooks",
            not ("MOP_LDAP_BIND_PASSWORD" in config.SETTINGS))
    c.check("the provider must be built with the LDAP password too",
            not ("MOP_LDAP_BIND_PASSWORD" not in identity.SETTINGS))
    c.check("the LDAP password must not reach --extra-vars",
            not ("MOP_LDAP_BIND_PASSWORD" in playvars.playbook_vars()))


def check_choice(c):
    p = identity.provider(SETTINGS)
    c.check("MOP_AUTH_PROVIDER=ldap must give LdapProvider",
            not (not isinstance(p, ldapauth.LdapProvider)), p)
    from mop.cli.server import deploy
    got = deploy.operator_refusals({"MOP_AUTH_PROVIDER": "ldap"})
    c.check("deploy must refuse incomplete LDAP settings",
            not (len(got) != 1 or "MOP_LDAP_URL" not in got[0]), got)
    c.check("complete LDAP settings must not stop deploy",
            not (deploy.operator_refusals(SETTINGS)), deploy.operator_refusals(SETTINGS))
    from mop.common import deps
    c.check("ldap3 must be in MOP_PIP_DEPS", not ("ldap3" not in deps.PIP))


def check_filters(c):
    """Фильтры строятся с экранированием (RFC 4515): логин -- не фильтр."""
    c.expect("user filter must escape", ldapauth.user_filter("uid", "a*)(uid=b"),
             "(uid=a\\2a\\29\\28uid=b)")
    got = ldapauth.group_filter("(|(member={dn})(memberUid={login}))", "uid=x\\,y,dc=z", "x")
    c.expect("group filter must escape", got, "(|(member=uid=x\\5c,y,dc=z)(memberUid=x))")


def check_ldap3(c):
    """Переходник на ldap3 -- на MOCK_SYNC: поиск, группы, bind."""
    try:
        import ldap3
    except ImportError:
        hermetic.skip("the ldap3 adapter", "no ldap3 on this machine -- "
                      "only the provider over a stub directory is checked")
        return
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
    c.expect("ldap3: ivan", p.authenticate("ivan", "i-pw"),
             Identity("ivan", "user", ("cloudpub", "rugent"), "Иван Петров", "ivan@example.dev"))
    c.expect("ldap3: anton", p.authenticate("anton", "a-pw"),
             Identity("anton", "admin", ("*",), "Антон Ермак"))
    for login, pw in (("ivan", "wrong"), ("nobody", "x")):
        c.check(f"ldap3: {login}/{pw} must be refused",
                not (refused(p.authenticate, login, pw) is None))
    c.check("ldap3: lookup", not (p.lookup("ivan") is None or p.lookup("nobody") is not None))
    # Служебная учётка с чужим паролем -- отказ с причиной, не «нет логина».
    bad = ldapauth.LdapProvider(cfg, ldapauth.Ldap3Directory(
        dataclasses.replace(cfg, bind_password="no"), server=server, strategy=ldap3.MOCK_SYNC))
    e = refused(bad.authenticate, "ivan", "i-pw")
    c.check("a broken service bind must say so", not (e is None or "service" not in e), repr(e))


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
def check_service_settings_214(c):
    import tempfile
    from mop.common import config
    fn = getattr(identity, "service_settings", None)
    if not c.check("identity.service_settings exists", fn is not None):
        return
    unit = {k: v for k, v in SETTINGS.items() if k != "MOP_LDAP_BIND_PASSWORD"}
    root = tempfile.mkdtemp(prefix="mop-test-ldap-")
    path = os.path.join(root, identity.BIND_PASSWORD_FILE)
    try:
        fn(unit.get, root)
        c.fail("ldap without the bind password file must be refused")
    except ValueError as e:
        c.check(f"the refusal must name the file {path}", not (path not in str(e)), e)
    with open(path, "w") as f:
        f.write("svc-pw\n")
    got = fn(unit.get, root)
    c.check("the provider must take the bind password from the file",
            not (ldapauth.settings(got).bind_password != "svc-pw"), got)
    c.check("the service must build an LdapProvider from the unit env and the file",
            not (not isinstance(identity.provider(got, root), ldapauth.LdapProvider)))
    on_controller = dict(SETTINGS, MOP_LDAP_BIND_PASSWORD="from-env")
    c.expect("where config.get has the password (the controller), it wins over the file",
             fn(on_controller.get, root)["MOP_LDAP_BIND_PASSWORD"], "from-env")
    plain = {"MOP_AUTH_PROVIDER": "file", "MOP_OPERATORS_FILE": "/srv/operators"}
    try:
        got = fn(plain.get, tempfile.mkdtemp())
        c.check("the file provider's settings must pass through",
                not (got.get("MOP_OPERATORS_FILE") != "/srv/operators"), got)
    except ValueError as e:
        c.fail("the file provider needs no LDAP password file", e)
    # Один список несекретных настроек личности на оба сервиса сервера.
    scoped = getattr(config, "IDENTITY_SCOPED", ())
    # MOP_OPERATORS ушёл (#219): людей даёт только провайдер.
    want = {"MOP_AUTH_PROVIDER"} | (set(ldapauth.NAMES) - {"MOP_LDAP_BIND_PASSWORD"})
    c.check("config.IDENTITY_SCOPED must be the provider's non-secret settings",
            not (set(scoped) != want), f"{sorted(set(scoped) ^ want)} differ")
    for unit_name in ("mop-callout", "mop-bootstrap"):
        missing = set(scoped) - set(config.SERVER_SCOPED.get(unit_name, ()))
        c.check(f"{unit_name} must get every identity setting", not (not scoped or missing),
                f"missing {sorted(missing)}")
    for unit_name, names in config.SERVER_SCOPED.items():
        c.check(f"{unit_name}: the LDAP password must never be in a unit's env",
                not ("MOP_LDAP_BIND_PASSWORD" in names))
    # Как пароль едет прогону: окружением процесса ansible, не аргументом,
    # и только когда провайдер -- ldap.
    from mop.cli import lib
    from mop.cli.server import _play
    penv = getattr(_play, "play_env", None)
    if c.check("_play.play_env exists", not (penv is None)):
        c.expect("with ldap, the play's env must carry the bind password",
                 penv(SETTINGS.get).get("MOP_LDAP_BIND_PASSWORD"), "svc-pw")
        c.check("without ldap, the bind password must not reach the play at all",
                not ("MOP_LDAP_BIND_PASSWORD" in penv({"MOP_AUTH_PROVIDER": "file",
                                                       "MOP_LDAP_BIND_PASSWORD": "x"}.get)))


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


def check_ad_groups_220(c):
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
        c.expect(f"direct: {login}", (got.role, got.projects), want)
    # Без группы доступа в настройках -- как в #208: olga без доступа.
    c.check("without MOP_LDAP_ACCESS_GROUP, staff alone is still no access",
            not (refused(provider().authenticate, "olga", "o-pw") is None))

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
            c.expect(f"ad: {login}", (got.role, got.projects), want)
        except Exception as e:  # noqa: BLE001
            c.fail(f"ad: {login}", f"{type(e).__name__}: {e}")
    e = refused(pa.authenticate, "olga", "o-pw")
    c.check("ad: in no group is no access", not (e is None or "no access" not in e), f"got {e!r}")
    c.check("ad: lookup follows the same membership",
            not (pa.lookup("ivan") is None or pa.lookup("olga") is not None))
    # admin решает первым: при admin группу доступа не спрашиваем.
    stub.asked.clear()
    pa.authenticate("anton", "a-pw")
    c.expect("ad: admin first, nothing after it: asked", stub.asked, [AD_ADMINS])

    # Настройки: неизвестный режим вложенности -- отказ с именем значения.
    try:
        ldapauth.settings({**SETTINGS, "MOP_LDAP_NESTED": "deep"})
        c.fail("MOP_LDAP_NESTED=deep must be refused")
    except ValueError as e:
        c.check("the refusal must name the value and the choice",
                not ("deep" not in str(e) or "ad" not in str(e)), e)
    cfg = ldapauth.settings(SETTINGS)
    c.expect("defaults: no access group, direct membership", (cfg.access_group, cfg.nested),
             ("", ""))

    # Фильтры: in-chain по записи пользователя, DN и логин экранированы.
    got = ldapauth.in_chain_filter("sAMAccountName", "a*b", "CN=mop (all),OU=G,DC=x")
    want = "(&(sAMAccountName=a\\2ab)(memberOf:1.2.840.113556.1.4.1941:=CN=mop \\28all\\29,OU=G,DC=x))"
    c.expect("in_chain_filter", got, want)
    for pattern, want in (("mop-{project}", "(&(objectClass=group)(cn=mop-*))"),
                          ("pool-{project}-users", "(&(objectClass=group)(cn=pool-*-users))"),
                          ("a*{project}", "(&(objectClass=group)(cn=a\\2a*))")):
        c.expect(f"project_groups_filter({pattern})", ldapauth.project_groups_filter(pattern), want)

    # Переходник: какие поиски он шлёт (mock правило in-chain не исполняет).
    acfg = ldapauth.settings({**ad, "MOP_LDAP_GROUP_BASE": f"OU=G,{BASE}"})
    d = ldapauth.Ldap3Directory(acfg)
    sent = []
    d._search = lambda base, filt, attrs: sent.append((base, filt)) or (
        [("CN=x", {})] if "Pool Users" in filt else [])
    c.check("adapter member_of: an entry found is membership, none is not",
            not (d.member_of("ivan", ACCESS) is not True
                 or d.member_of("ivan", AD_ADMINS) is not False))
    c.expect("adapter member_of searches the user under the base", sent[0],
             (BASE, ldapauth.in_chain_filter("sAMAccountName", "ivan", ACCESS)))
    sent.clear()
    d.project_groups()
    c.expect("adapter project_groups searches the group base", sent,
             [(f"OU=G,{BASE}", "(&(objectClass=group)(cn=mop-*))")])


def check_knows_232(c):
    """Звено цепочки (#232): знает логин -- в каталоге есть его запись, даже
    без доступа (иначе одноимённая учётка ниже стала бы вторым паролем).
    STATUS: FIXED — see #232"""
    p = provider()
    for login, want in (("anton", True), ("olga", True), ("nobody", False), ("a*", False)):
        c.check(f"knows({login!r}) must be {want}", not (p.knows(login) is not want))
    c.check("knows must never bind as the user", not (p.directory.checked), p.directory.checked)


def main():
    c = Checks()
    for fn in (check_authenticate, check_mapping, check_lookup, check_settings,
               check_choice, check_filters, check_ldap3, check_service_settings_214,
               check_ad_groups_220, check_knows_232):
        try:
            fn(c)
        except Exception as e:  # noqa: BLE001 -- падение проверки -- тоже провал
            c.fail(fn.__name__, f"raised {type(e).__name__}: {e}")
    return c.report("ldapauth")


if __name__ == "__main__":
    sys.exit(main())
