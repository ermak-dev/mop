#!/usr/bin/env python3
"""Личность оператора и её источник: python3 tests/identity.py

Оператор был строкой MOP_OPERATORS (`имя:роль[:проекты]`), разобранной в
словарь {role, projects}; пароль заводил deploy в secrets/nats-op-<имя>.pass.
Ни имени человека, ни почты, ни места, куда подключить LDAP (#205, эпик #36):
требование оператора -- личность есть пользователь, прошедший проверку в
NATS, через провайдера с плоским файлом и LDAP.

HYPOTHESIS: личности как значения нет -- есть словарь из разбора настройки,
и проверка пароля живёт только в конфиге NATS; подключить второй источник
некуда.
SOLUTION: mop/server/identity.py -- Identity (frozen dataclass), AuthProvider
(Protocol: authenticate, lookup) и PlainFileProvider: файл операторов на
сервере со scrypt-хешем пароля плюс переход -- сегодняшние MOP_OPERATORS и
сгенерированные файлы паролей, чтобы каждый нынешний логин работал как был.
Провайдер выбирает MOP_AUTH_PROVIDER (дефолт file -- сегодняшнее поведение);
operators.permissions принимает Identity.
STATUS: FIXED — see #205
"""
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from _lib import Checks, patched, restored  # noqa: E402
from mop.server import identity, natsconf, operators  # noqa: E402
from mop.server.identity import Identity  # noqa: E402

# Все формы сегодняшней настройки, включая запись без роли (до #106).
SETTINGS = ("anton:admin; ivan:user:rugent,cloudpub; olga:user:*",
            "anton:*; ivan:mop,rugent",
            "ivan:user:b,a,c",
            "")


def check_identity(c):
    """Identity -- значение: неизменяемое, сравнимое, строка файла туда и обратно."""
    ivan = Identity("ivan", "user", ("rugent", "cloudpub"), "Иван Петров", "ivan@example.dev")
    try:
        ivan.role = "admin"
        c.fail("Identity must be frozen")
    except AttributeError:
        pass
    h = identity.hash_password("s3cret")
    for who in (ivan, Identity("anton", "admin", ("*",)), Identity("olga", "user", ("*",), "Olga")):
        line = identity.format_line(who, h)
        c.check("a record is one line", not ("\n" in line), repr(line))
        back = identity.parse_line(line)
        c.expect(f"round trip {who} -> {line!r}", back, (who, h))
    # Запись читается и руками написанная.
    got = identity.parse_line(f"ivan:user:rugent,cloudpub:Иван Петров:ivan@example.dev:{h}")
    c.expect("hand-written record", got, (ivan, h))
    # Права по роли и проектам -- те же правила, что у MOP_OPERATORS: admin
    # без проектов, user с проектами, человек не носит имя роли шины.
    for bad in ("ivan:user::::" + h,              # user без проектов
                "anton:admin:mop:::" + h,        # admin с проектами
                "anton:owner:mop:::" + h,        # нет такой роли
                "service:admin::::" + h,         # имя роли шины
                "ivan:user:mop:::",              # нет хеша
                "ivan:user:mop::",               # полей не хватает
                ":user:mop:::" + h):             # нет логина
        try:
            identity.parse_line(bad)
        except ValueError:
            continue
        c.fail(f"{bad!r} must be refused")
    # Двоеточие или перевод строки в поле разорвали бы запись -- громкий отказ.
    for who in (Identity("ivan", "user", ("mop",), "a:b"),
                Identity("ivan", "user", ("mop",), "", "x\ny")):
        try:
            identity.format_line(who, h)
            c.fail(f"{who} must be refused: it breaks the line")
        except ValueError:
            pass


def check_hash(c):
    """scrypt с солью: хеш не повторяется, проверка отличает пароль."""
    a, b = identity.hash_password("pw"), identity.hash_password("pw")
    c.check("the scheme is named in the hash", a.startswith("scrypt$"), repr(a))
    c.check("two hashes of one password must differ: the salt is random", not (a == b))
    c.check("verify must accept the password and refuse another",
            not (not identity.verify_password("pw", a) or identity.verify_password("pW", a)))
    c.check("an unknown scheme or an empty hash must never verify",
            not (identity.verify_password("pw", "md5$whatever")
                 or identity.verify_password("pw", "")))


def provider(tmp, records=""):
    path = os.path.join(tmp, "operators")
    with open(path, "w") as f:
        f.write(records)
    return identity.PlainFileProvider(path)


def check_provider(c):
    """authenticate и lookup: файл операторов -- единственный источник (#219)."""
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    olga = Identity("olga", "user", ("mop",), "Olga", "olga@example.dev")
    anton = Identity("anton", "admin", ("*",))
    p = provider(tmp, "# операторы\n\n"
                 + identity.format_line(olga, identity.hash_password("olga-pw")) + "\n"
                 + identity.format_line(anton, identity.hash_password("anton-pw")) + "\n")
    c.check("PlainFileProvider must satisfy AuthProvider", isinstance(p, identity.AuthProvider))
    c.check("the right password must give the identity from the file",
            not (p.authenticate("olga", "olga-pw") != olga
                 or p.authenticate("anton", "anton-pw") != anton))
    c.check("lookup: the identity, or None for an unknown login",
            not (p.lookup("olga") != olga or p.lookup("nobody") is not None))
    for login, pw, why in (("olga", "wrong", "wrong password"),
                           ("anton", "", "wrong password"),
                           ("nobody", "x", "unknown login")):
        try:
            p.authenticate(login, pw)
            c.fail(f"{login}/{pw!r} must be refused")
        except identity.Refused as e:
            c.check(f"{login}/{pw!r}: the reason must say {why!r}", why in str(e), f"got {e}")
    # Один человек -- одно определение; отказ бьёт по логину, не по провайдеру.
    tmp2 = tempfile.mkdtemp(prefix="mop-test-identity-")
    ivan = Identity("ivan", "user", ("mop",))
    dup = provider(tmp2, identity.format_line(olga, identity.hash_password("x")) + "\n"
                   + identity.format_line(ivan, identity.hash_password("i1")) + "\n"
                   + identity.format_line(ivan, identity.hash_password("i2")) + "\n")
    for call, args in (("authenticate", ("ivan", "i1")), ("lookup", ("ivan",))):
        try:
            getattr(dup, call)(*args)
            c.fail(f"{call}(ivan) defined twice must be refused")
        except identity.Refused as e:
            c.check(f"{call}(ivan): the refusal must name ivan and {dup.path}",
                    not ("ivan" not in str(e) or dup.path not in str(e)), str(e))
    c.expect("a duplicate must not take the other logins down", dup.authenticate("olga", "x"), olga)
    c.expect("conflicts", dup.conflicts(), [f"login ivan is defined twice in {dup.path}"])
    c.check("no duplicates, no conflicts", not p.conflicts(), repr(p.conflicts()))
    # Нет файла -- нет людей: второго источника больше нет (#219).
    bare = identity.PlainFileProvider(os.path.join(tmp, "absent"))
    c.check("without an operators file there is nobody",
            not (bare.identities() or bare.lookup("anton") is not None))


def check_one_source_219(c):
    """HYPOTHESIS (#219): людей два источника -- файл операторов и переходный
    MOP_OPERATORS с паролями deploy'я (#205); оператор хочет один.
    SOLUTION: люди только из провайдера (файл через `mop server user`, или LDAP);
    MOP_OPERATORS уходит из настроек, провайдера и плейбуков.
    STATUS: FIXED — see #219"""
    from mop.common import config
    from mop.server import playvars
    for where, names in (("config.SETTINGS", config.SETTINGS), ("identity.SETTINGS", identity.SETTINGS),
                         ("config.IDENTITY_SCOPED", config.IDENTITY_SCOPED)):
        c.check(f"MOP_OPERATORS must be gone from {where}", not ("MOP_OPERATORS" in names))
    for gone in ("from_setting", "subjects"):
        c.check(f"identity.{gone} reads MOP_OPERATORS: it must be gone",
                not hasattr(identity, gone))
    c.check("the playbooks must not get people's subjects any more",
            not ("MOP_OPERATOR_SUBJECTS" in playvars.playbook_vars()))
    try:
        identity.PlainFileProvider("/nonexistent", "anton:admin")
        c.fail("PlainFileProvider must not take MOP_OPERATORS any more")
    except TypeError:
        pass


def check_choice(c):
    """Провайдер выбирает настройка; дефолт -- файл операторов."""
    from mop.common import config
    c.expect("MOP_AUTH_PROVIDER must default to file",
             config.SETTINGS.get("MOP_AUTH_PROVIDER"), "file")
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    path = os.path.join(tmp, "operators")
    with open(path, "w") as f:
        f.write(identity.format_line(Identity("anton", "admin", ("*",)),
                                     identity.hash_password("x")) + "\n")
    p = identity.provider({"MOP_AUTH_PROVIDER": "file", "MOP_OPERATORS_FILE": path}, tmp)
    c.check("file must give the plain file provider over the operators file",
            not (not isinstance(p, identity.PlainFileProvider) or p.lookup("anton") is None),
            str(p))
    p = identity.provider({"MOP_AUTH_PROVIDER": "file"}, tmp)
    c.expect("without MOP_OPERATORS_FILE the file is secrets/operators", p.path, path)
    # nope -- нет такого провайдера; ldap без настроек каталога (#208) --
    # тоже отказ, а не провайдер, который откажет каждому входу.
    for bad in ("ldap", "nope"):
        try:
            identity.provider({"MOP_AUTH_PROVIDER": bad}, tmp)
            c.fail(f"{bad!r} must be refused: no such provider or no settings for it")
        except ValueError as e:
            c.check("the refusal must name the provider", bad in str(e), str(e))


def check_deploy(c):
    """mop server deploy отказывает до плейбука: логин определён дважды, провайдер не
    читается, людей нет вовсе (#219: войти было бы некому) или в .env ещё
    лежит MOP_OPERATORS (#219: людей переносят `mop server user import`)."""
    from mop.cli.server import deploy
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    path = os.path.join(tmp, "operators")
    with open(path, "w") as f:
        f.write(identity.format_line(Identity("olga", "user", ("mop",)),
                                     identity.hash_password("x")) + "\n")
    base = {"MOP_AUTH_PROVIDER": "file", "MOP_OPERATORS_FILE": path}
    fn = getattr(deploy, "operator_refusals", None)
    if not c.check("mop server deploy has operator_refusals", fn is not None):
        return
    c.check("one person, no duplicates: deploy goes on", not fn(base, tmp), repr(fn(base, tmp)))
    gone = "MOP_OPERATORS is gone: move people with `mop server user import` on the server, then remove the line"
    c.expect("MOP_OPERATORS left in .env must stop deploy",
             fn(base, tmp, leftover="anton:admin"), [gone])
    empty = os.path.join(tmp, "empty")
    open(empty, "w").close()
    got = fn({**base, "MOP_OPERATORS_FILE": empty}, tmp)
    c.check("nobody in the operators file must stop deploy",
            not (len(got) != 1 or "mop server user add" not in got[0] or empty not in got[0]),
            repr(got))
    c.check("ldap without its settings must stop deploy",
            fn({**base, "MOP_AUTH_PROVIDER": "ldap"}, tmp))
    with open(path, "a") as f:
        f.write(identity.format_line(Identity("olga", "user", ("mop",)),
                                     identity.hash_password("y")) + "\n")
    c.expect("a duplicate must stop deploy", fn(base, tmp),
             [f"login olga is defined twice in {path}"])
    with open(path, "a") as f:
        f.write("broken line\n")
    c.check("a broken operators file must stop deploy", fn(base, tmp))


# ── #232: цепочка провайдеров -- файл операторов поверх LDAP ─────────────
# HYPOTHESIS: у установки ровно один провайдер (identity.provider выбирает
# file или ldap); на rumop оператору нужен прежний локальный логин ermak из
# файла, а всем остальным -- AD, и одно исключает другое.
# SOLUTION: MOP_AUTH_PROVIDER -- список через запятую, порядок -- старшинство
# (identity.links); ChainProvider спрашивает звенья по порядку, и первое,
# которое ЗНАЕТ логин (knows), решает за всех: неверный локальный пароль до
# LDAP не доходит, одноимённая учётка AD второго пароля не даёт.
# STATUS: FIXED — see #232
LDAP = {"MOP_LDAP_URL": "ldaps://ldap.example.dev",
        "MOP_LDAP_BIND_DN": "cn=mop,ou=services,dc=example,dc=dev",
        "MOP_LDAP_BIND_PASSWORD": "svc-pw", "MOP_LDAP_BASE": "dc=example,dc=dev",
        "MOP_LDAP_ADMIN_GROUP": "cn=mop admins,ou=groups,dc=example,dc=dev"}


class Untouchable:
    """Звено, которое нельзя спрашивать: любой вызов -- провал проверки."""

    def __getattr__(self, name):
        def called(*a, **kw):
            raise AssertionError(f"LDAP was asked: {name}{a}")
        return called


class Directory:
    """Звено-заглушка каталога: {логин: (Identity, пароль)}; пишет, кого спросили."""

    def __init__(self, people):
        self.people, self.asked = people, []

    def knows(self, login):
        self.asked.append(("knows", login))
        return login in self.people

    def lookup(self, login):
        self.asked.append(("lookup", login))
        return self.people[login][0] if login in self.people else None

    def authenticate(self, login, password):
        self.asked.append(("authenticate", login))
        if login not in self.people:
            raise identity.Refused(f"{login}: unknown login")
        who, pw = self.people[login]
        if pw != password:
            raise identity.Refused(f"{login}: wrong password")
        return who


def check_chain_232(c):
    Chain = getattr(identity, "ChainProvider", None)
    links = getattr(identity, "links", None)
    if not c.check("identity.ChainProvider and identity.links are present",
                   not (Chain is None or links is None)):
        return
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    ermak = Identity("ermak", "admin", ("*",), "Local Ermak")
    local = provider(tmp, identity.format_line(ermak, identity.hash_password("local-pw")) + "\n")
    # Файл знает логин -- решает он один, LDAP не спрошен вовсе: ни при
    # верном пароле, ни при неверном (ни bind'а, ни счётчика неудач AD).
    chain = Chain([local, Untouchable()])
    c.check("ChainProvider must satisfy AuthProvider", isinstance(chain, identity.AuthProvider))
    try:
        c.expect("a file hit with the right password must give the file's identity",
                 chain.authenticate("ermak", "local-pw"), ermak)
    except AssertionError as e:
        c.fail("right local password", str(e))
    try:
        chain.authenticate("ermak", "ad-pw")
        c.fail("a wrong local password must be refused")
    except identity.Refused as e:
        c.check("wrong local password: the reason must say so", "wrong password" in str(e), str(e))
    except AssertionError as e:
        c.fail("wrong local password", str(e))
    try:
        c.expect("lookup of a file login must give the file's identity",
                 chain.lookup("ermak"), ermak)
    except AssertionError as e:
        c.fail("lookup of a file login", str(e))
    # Одноимённая учётка каталога -- не второй пароль локальной.
    ad_ermak = Identity("ermak", "admin", ("*",), "AD Ermak")
    olga = Identity("olga", "user", ("*",), "Olga")
    ad = Directory({"ermak": (ad_ermak, "ad-pw"), "olga": (olga, "olga-pw")})
    chain = Chain([local, ad])
    try:
        chain.authenticate("ermak", "ad-pw")
        c.fail("the AD password of a same-named account must not open the local one")
    except identity.Refused:
        pass
    c.check("a login the file knows must never reach LDAP",
            not any(login == "ermak" for _, login in ad.asked), repr(ad.asked))
    # Логин, которого файл не знает, -- к LDAP.
    c.check("a login unknown to the file must go to LDAP",
            not (chain.authenticate("olga", "olga-pw") != olga or chain.lookup("olga") != olga))
    try:
        chain.authenticate("olga", "nope")
        c.fail("LDAP's wrong password must be refused")
    except identity.Refused:
        pass
    c.check("a login nobody knows: lookup -> None", not (chain.lookup("nobody") is not None))
    try:
        chain.authenticate("nobody", "x")
        c.fail("a login nobody knows must be refused")
    except identity.Refused as e:
        c.check("a login nobody knows: the reason must say so", "unknown login" in str(e), str(e))
    # Обратный порядок -- старшинство у LDAP: ermak решает каталог.
    back = Chain([ad, local])
    c.expect("ldap,file: the directory decides a login it knows",
             back.authenticate("ermak", "ad-pw"), ad_ermak)
    try:
        back.authenticate("ermak", "local-pw")
        c.fail("ldap,file: the local password must not open a login the directory knows")
    except identity.Refused:
        pass
    c.expect("ldap,file: lookup of a login the directory knows", back.lookup("ermak"), ad_ermak)
    # Разбор настройки: порядок -- старшинство; одно имя -- как было.
    for value, want in (("file,ldap", ("file", "ldap")), ("ldap, file", ("ldap", "file")),
                        ("file", ("file",)), ("ldap", ("ldap",)), ("", ("file",)),
                        (None, ("file",))):
        try:
            c.expect(f"links({value!r})", links(value), want)
        except ValueError as e:
            c.fail(f"links({value!r}) refused", str(e))
    for bad, name in (("file,nope", "nope"), ("file,ldap,file", "file"),
                      ("ldap,ldap", "ldap"), ("file,", "''")):
        try:
            links(bad)
            c.fail(f"{bad!r} must be refused")
        except ValueError as e:
            c.check(f"{bad!r}: the refusal must name {name}",
                    not (name not in str(e) or "MOP_AUTH_PROVIDER" not in str(e)), str(e))
        try:
            identity.provider({"MOP_AUTH_PROVIDER": bad, **LDAP}, tmp)
            c.fail(f"provider must refuse {bad!r}")
        except ValueError:
            pass
    # provider(): одно имя -- само звено; список -- цепочка в его порядке.
    from mop.server import ldapauth
    p = identity.provider({"MOP_AUTH_PROVIDER": "file,ldap", "MOP_OPERATORS_FILE": local.path,
                           **LDAP}, tmp)
    if c.check("file,ldap must give a chain of the file, then LDAP",
               not (not isinstance(p, Chain) or [type(l) for l in p.links] != [
                   identity.PlainFileProvider, ldapauth.LdapProvider]), str(p)):
        c.expect("the chain's file link reads MOP_OPERATORS_FILE", p.links[0].path, local.path)
    c.check("a single ldap stays a plain LdapProvider",
            isinstance(identity.provider({"MOP_AUTH_PROVIDER": "ldap", **LDAP}, tmp),
                       ldapauth.LdapProvider))
    # Звено ldap -- как одно ldap: без настроек каталога отказ.
    try:
        identity.provider({"MOP_AUTH_PROVIDER": "file,ldap"}, tmp)
        c.fail("file,ldap without LDAP settings must be refused")
    except ValueError:
        pass


def check_deploy_chain_232(c):
    """Отказы deploy -- по звеньям. Пустой файл операторов -- отказ, только
    когда кроме файла впускать некому: при file,ldap людей даёт LDAP. Дубль
    и битая строка -- отказ всегда: звено file сломано, в каком бы порядке
    оно ни стояло. Звено ldap без настроек -- отказ, как одно ldap.
    Пароль LDAP едет прогону и берётся сервисом, когда ldap -- звено цепочки."""
    from mop.cli import lib  # noqa: F401 -- импорт был и до #269
    from mop.cli.server import _play
    from mop.cli.server import deploy
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    empty = os.path.join(tmp, "empty")
    open(empty, "w").close()
    for chain in ("file,ldap", "ldap,file"):
        s = {"MOP_AUTH_PROVIDER": chain, "MOP_OPERATORS_FILE": empty, **LDAP}
        c.check(f"{chain}: an empty operators file must not stop deploy, LDAP admits people",
                not deploy.operator_refusals(s, tmp), repr(deploy.operator_refusals(s, tmp)))
        got = deploy.operator_refusals({"MOP_AUTH_PROVIDER": chain, "MOP_OPERATORS_FILE": empty}, tmp)
        c.check(f"{chain}: the ldap link without its settings must stop deploy",
                not (len(got) != 1 or "MOP_LDAP_URL" not in got[0]), repr(got))
        dup = os.path.join(tmp, "dup")
        line = identity.format_line(Identity("olga", "user", ("mop",)), identity.hash_password("x"))
        with open(dup, "w") as f:
            f.write(line + "\n" + line + "\n")
        got = deploy.operator_refusals({**s, "MOP_OPERATORS_FILE": dup}, tmp)
        c.expect(f"{chain}: a duplicate in the file link must stop deploy", got,
                 [f"login olga is defined twice in {dup}"])
        with open(dup, "a") as f:
            f.write("broken line\n")
        c.check(f"{chain}: a broken operators file must stop deploy",
                deploy.operator_refusals({**s, "MOP_OPERATORS_FILE": dup}, tmp))
        c.expect(f"{chain}: the play's env must carry the bind password",
                 _play.play_env({**s}.get).get("MOP_LDAP_BIND_PASSWORD"), "svc-pw")
        unit = {k: v for k, v in s.items() if k != "MOP_LDAP_BIND_PASSWORD"}
        try:
            identity.service_settings(unit.get, tmp)
            c.fail(f"{chain}: the service without the bind password file must be refused")
        except ValueError:
            pass
    got = deploy.operator_refusals({"MOP_AUTH_PROVIDER": "file,nope"}, tmp)
    c.check("an unknown link must stop deploy, naming it",
            not (len(got) != 1 or "nope" not in got[0]), repr(got))


# ── #234: копия личностей -- владельца каталога, не вызвавшего ───────────
# HYPOTHESIS: `mop server user` от root обновлял /etc/nats/identity/operators сам
# (refresh_copy, #218), и файл выходил root:root 0600 в каталоге ermak:ermak
# 0700: сервисы пула (callout, identity) его не читали, и на шину не входил
# никто, пока файлу не вернули владельца руками.
# SOLUTION: копия получает uid:gid каталога копии (его задаёт deploy); не
# выходит отдать ей этого владельца -- отказ одной строкой с путём и
# «run mop server deploy», прежняя копия остаётся как была.
# Без настоящего root владелец подменяется: identity._owner (кто владеет
# каталогом) заменён заглушкой с чужим uid:gid. Настоящий fchown от
# непривилегированного пользователя тогда падает EPERM -- это случай «не
# root и не владелец»; подменённый os.fchown/os.chown, пишущий вызовы, --
# случай root, у которого chown проходит.
# STATUS: FIXED — see #234
def check_copy_owner_234(c):
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    src = os.path.join(tmp, "operators")
    with open(src, "w") as f:
        f.write("new\n")
    nats = os.path.join(tmp, "nats")
    folder = os.path.join(nats, "identity")
    os.makedirs(folder)
    copy = os.path.join(folder, identity.OPERATORS_FILE)
    with open(copy, "w") as f:
        f.write("old\n")
    me = (os.getuid(), os.getgid())
    foreign = (me[0] + 4242, me[1] + 4242)
    if not c.check("run this check as an ordinary user: root's chown passes everywhere",
                   not (me[0] == 0)):
        return
    with restored(identity, "_owner"):
        identity._owner = lambda path: foreign
        # Не root и не владелец: отдать копию владельцу каталога нельзя.
        why = identity.refresh_copy(src, folder)
        st = os.stat(copy)
        c.check("a caller that is not the directory's owner must not leave the copy its own",
                not ((st.st_uid, st.st_gid) != foreign and open(copy).read() != "old\n"),
                f"the copy is owned by {st.st_uid}:{st.st_gid}, "
                f"the directory is {foreign[0]}:{foreign[1]}")
        c.check(f"the refusal must be one line naming {copy} and mop server deploy",
                not (not isinstance(why, str) or copy not in why or "mop server deploy" not in why
                     or "\n" in why), repr(why))
        c.expect("a refused refresh must leave the old copy as it was", open(copy).read(), "old\n")
        c.check("a refused refresh must leave no temporary file",
                not [n for n in os.listdir(folder) if n != identity.OPERATORS_FILE],
                repr(os.listdir(folder)))
        # root: chown проходит -- файл уходит владельцу каталога, атомарно, 0600.
        calls = []
        with patched(os, fchown=lambda fd, uid, gid: calls.append(("fchown", uid, gid)),
                     chown=lambda p, uid, gid, **kw: calls.append(("chown", uid, gid))):
            why = identity.refresh_copy(src, folder)
        c.check("a caller that may chown must refresh the copy", not why, repr(why))
        c.check(f"the copy must be given the directory's owner {foreign}",
                not (not calls or any((uid, gid) != foreign for _, uid, gid in calls)), repr(calls))
        c.check("the refreshed copy: the new content, 0600",
                not (open(copy).read() != "new\n" or oct(os.stat(copy).st_mode & 0o777) != "0o600"))
        # Каталога ещё нет: он тоже не остаётся за вызвавшим -- владелец
        # берётся у /etc/nats над ним.
        fresh = os.path.join(tmp, "nats2", "identity")
        os.makedirs(os.path.dirname(fresh))
        why = identity.refresh_copy(src, fresh)
        c.check("a directory made by the refresh must not stay the caller's",
                not (os.path.exists(fresh) and os.stat(fresh).st_uid != foreign[0]))
        c.check("a directory the caller cannot give away: refusal",
                not (not isinstance(why, str) or "mop server deploy" not in why), repr(why))
        # Свой каталог -- как прежде: успех, None.
        identity._owner = lambda path: me
        c.check("the directory's own owner refreshes silently: None",
                not (identity.refresh_copy(src, folder) is not None))


def check_save_makes_dir_238(c):
    """HYPOTHESIS (#238): на свежем сервере каталога secrets/ нет, и первый
    файл операторов записать некуда. SOLUTION: запись файла заводит
    недостающий каталог 0700; лежащий не трогает. STATUS: FIXED — see #238"""
    import stat
    root = tempfile.mkdtemp(prefix="mop-test-identity-")
    path = os.path.join(root, "secrets", "operators")
    line = identity.format_line(Identity("anton", "admin", ("*",)), identity.hash_password("x"))
    identity._save(path, [line])
    d = os.path.dirname(path)
    c.check("a missing directory of the operators file must be made 0700",
            not (not os.path.isdir(d) or stat.S_IMODE(os.stat(d).st_mode) != 0o700))
    c.check("the operators file must be 0600 with the person",
            not (stat.S_IMODE(os.stat(path).st_mode) != 0o600
                 or identity.PlainFileProvider(path).lookup("anton") is None))
    os.chmod(root, 0o755)
    identity._save(os.path.join(root, "other"), [line])
    c.expect("an existing directory (MOP_OPERATORS_FILE may name any) must not be chmod-ed",
             stat.S_IMODE(os.stat(root).st_mode), 0o755)


def main():
    c = Checks()
    for check in (check_identity, check_hash, check_provider, check_one_source_219, check_choice,
                  check_deploy, check_chain_232, check_deploy_chain_232,
                  check_copy_owner_234, check_save_makes_dir_238):
        try:
            check(c)
        except Exception as e:  # noqa: BLE001 -- падение проверки -- тоже провал
            c.fail(check.__name__, f"raised {type(e).__name__}: {e}")
    return c.report("identity")


if __name__ == "__main__":
    sys.exit(main())
