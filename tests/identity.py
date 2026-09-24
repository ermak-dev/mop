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
SOLUTION: mop/identity.py -- Identity (frozen dataclass), AuthProvider
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import identity, natsconf, operators  # noqa: E402
from mop.identity import Identity  # noqa: E402

# Все формы сегодняшней настройки, включая запись без роли (до #106).
SETTINGS = ("anton:admin; ivan:user:rugent,cloudpub; olga:user:*",
            "anton:*; ivan:mop,rugent",
            "ivan:user:b,a,c",
            "")


def check_identity():
    """Identity -- значение: неизменяемое, сравнимое, строка файла туда и обратно."""
    out = []
    ivan = Identity("ivan", "user", ("rugent", "cloudpub"), "Иван Петров", "ivan@example.dev")
    try:
        ivan.role = "admin"
        out.append("Identity must be frozen")
    except AttributeError:
        pass
    h = identity.hash_password("s3cret")
    for who in (ivan, Identity("anton", "admin", ("*",)), Identity("olga", "user", ("*",), "Olga")):
        line = identity.format_line(who, h)
        if "\n" in line:
            out.append(f"a record is one line: {line!r}")
        back = identity.parse_line(line)
        if back != (who, h):
            out.append(f"round trip {who} -> {line!r} -> {back}")
    # Запись читается и руками написанная.
    got = identity.parse_line(f"ivan:user:rugent,cloudpub:Иван Петров:ivan@example.dev:{h}")
    if got != (ivan, h):
        out.append(f"hand-written record -> {got}")
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
        out.append(f"{bad!r} must be refused")
    # Двоеточие или перевод строки в поле разорвали бы запись -- громкий отказ.
    for who in (Identity("ivan", "user", ("mop",), "a:b"),
                Identity("ivan", "user", ("mop",), "", "x\ny")):
        try:
            identity.format_line(who, h)
            out.append(f"{who} must be refused: it breaks the line")
        except ValueError:
            pass
    return out


def check_hash():
    """scrypt с солью: хеш не повторяется, проверка отличает пароль."""
    out = []
    a, b = identity.hash_password("pw"), identity.hash_password("pw")
    if not a.startswith("scrypt$"):
        out.append(f"the scheme is named in the hash: {a!r}")
    if a == b:
        out.append("two hashes of one password must differ: the salt is random")
    if not identity.verify_password("pw", a) or identity.verify_password("pW", a):
        out.append("verify must accept the password and refuse another")
    if identity.verify_password("pw", "md5$whatever") or identity.verify_password("pw", ""):
        out.append("an unknown scheme or an empty hash must never verify")
    return out


def provider(tmp, records="", setting="", passes=None):
    path = os.path.join(tmp, "operators")
    with open(path, "w") as f:
        f.write(records)
    for name, pw in (passes or {}).items():
        with open(os.path.join(tmp, operators.pass_file(name)), "w") as f:
            f.write(pw + "\n")
    return identity.PlainFileProvider(path, setting, tmp)


def check_provider():
    """authenticate и lookup: файл операторов и переход с MOP_OPERATORS."""
    out = []
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    olga = Identity("olga", "user", ("mop",), "Olga", "olga@example.dev")
    p = provider(tmp, "# операторы\n\n" + identity.format_line(olga, identity.hash_password("olga-pw")) + "\n",
                 "anton:admin; ivan:user:rugent", {"anton": "anton-pw"})
    if not isinstance(p, identity.AuthProvider):
        out.append("PlainFileProvider must satisfy AuthProvider")
    if p.authenticate("olga", "olga-pw") != olga:
        out.append("the right password must give the identity from the file")
    if p.lookup("olga") != olga or p.lookup("nobody") is not None:
        out.append("lookup: the identity, or None for an unknown login")
    # Переход: логин из MOP_OPERATORS входит сгенерированным паролем, как сегодня.
    anton = Identity("anton", "admin", ("*",))
    if p.authenticate("anton", "anton-pw") != anton or p.lookup("anton") != anton:
        out.append("a MOP_OPERATORS login must authenticate with its generated password")
    if p.lookup("ivan") != Identity("ivan", "user", ("rugent",)):
        out.append(f"a MOP_OPERATORS login must be found: {p.lookup('ivan')}")
    for login, pw, why in (("olga", "wrong", "wrong password"),
                           ("anton", "wrong", "wrong password"),
                           ("anton", "", "wrong password"),
                           ("nobody", "x", "unknown login"),
                           # Пароль ещё не заведён deploy'ем -- отказ, не вход.
                           ("ivan", "", "no password")):
        try:
            p.authenticate(login, pw)
            out.append(f"{login}/{pw!r} must be refused")
        except identity.Refused as e:
            if why not in str(e):
                out.append(f"{login}/{pw!r}: the reason must say {why!r}, got {e}")
    # Один человек -- одно определение, но отказ бьёт по одному логину, а не
    # по провайдеру: при переносе людей из MOP_OPERATORS в файл один дубль
    # иначе выключил бы вход всем. Громкий отказ всего -- в mop deploy.
    tmp2 = tempfile.mkdtemp(prefix="mop-test-identity-")
    ivan = Identity("ivan", "user", ("mop",))
    dup = provider(tmp2, identity.format_line(olga, identity.hash_password("x")) + "\n"
                   + identity.format_line(ivan, identity.hash_password("i1")) + "\n"
                   + identity.format_line(ivan, identity.hash_password("i2")) + "\n",
                   "olga:admin; anton:admin", {"anton": "anton-pw", "olga": "x"})
    for login, pw, sources in (("olga", "x", ("MOP_OPERATORS", dup.path)),
                               ("ivan", "i1", (dup.path,))):
        for call, args in (("authenticate", (login, pw)), ("lookup", (login,))):
            try:
                getattr(dup, call)(*args)
                out.append(f"{call}({login}) defined twice must be refused")
            except identity.Refused as e:
                if login not in str(e) or not all(s in str(e) for s in sources):
                    out.append(f"{call}({login}): the refusal must name {login} and {sources}: {e}")
            except Exception as e:  # noqa: BLE001
                out.append(f"{call}({login}) must be Refused, got {type(e).__name__}: {e}")
    if dup.authenticate("anton", "anton-pw") != anton or dup.lookup("anton") != anton:
        out.append("a duplicate must not take the other logins down")
    if sorted(i.login for i in dup.identities()) != ["anton"]:
        out.append(f"a login defined twice is nobody's identity: {dup.identities()}")
    # Громкая сторона -- для mop deploy: каждый дубль строкой, с источниками.
    got = dup.conflicts()
    want = [f"login ivan is defined twice in {dup.path}",
            f"login olga is defined both in MOP_OPERATORS and in {dup.path}"]
    if got != want:
        out.append(f"conflicts -> {got}, wanted {want}")
    if p.conflicts():
        out.append(f"no duplicates, no conflicts: {p.conflicts()}")
    # Нет файла -- установка как сегодня, только MOP_OPERATORS.
    bare = identity.PlainFileProvider(os.path.join(tmp, "absent"), "anton:admin", tmp)
    if bare.authenticate("anton", "anton-pw") != anton:
        out.append("without an operators file the installation works as today")
    return out


def check_transition():
    """Установка только с MOP_OPERATORS: те же логины, роли и проекты, те же
    права и тот же users.conf байт в байт, что у operators.parse."""
    out = []
    for setting in SETTINGS:
        today = operators.parse(setting)
        tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
        p = identity.PlainFileProvider(os.path.join(tmp, "absent"), setting, tmp)
        got = {i.login: {"role": i.role, "projects": list(i.projects)} for i in p.identities()}
        if got != today:
            out.append(f"{setting!r}: {got} != operators.parse {today}")
            continue
        for i in p.identities():
            if operators.permissions(i) != operators.permissions(today[i.login]):
                out.append(f"{setting!r}: permissions of {i.login} differ")
        base = {"service": "svc", "nodes": {"hyper": "hy"}}
        old = natsconf.render({**base, "operators": {
            n: {"password": n, **operators.permissions(o)} for n, o in today.items()}}, {"mop": "pu"})
        new = natsconf.render({**base, "operators": {
            i.login: {"password": i.login, **operators.permissions(i)} for i in p.identities()}},
            {"mop": "pu"})
        if new != old:
            out.append(f"{setting!r}: users.conf drifted")
        if identity.subjects(setting) != {n: operators.permissions(o) for n, o in today.items()}:
            out.append(f"{setting!r}: subjects for the playbooks drifted")
    return out


def check_choice():
    """Провайдер выбирает настройка; дефолт -- сегодняшнее поведение."""
    out = []
    from mop import config
    if config.SETTINGS.get("MOP_AUTH_PROVIDER") != "file":
        out.append("MOP_AUTH_PROVIDER must default to file: today's behaviour")
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    p = identity.provider({"MOP_AUTH_PROVIDER": "file", "MOP_OPERATORS": "anton:admin",
                           "MOP_OPERATORS_FILE": os.path.join(tmp, "operators")}, tmp)
    if not isinstance(p, identity.PlainFileProvider) or p.lookup("anton") is None:
        out.append(f"file must give the plain file provider over MOP_OPERATORS: {p}")
    for bad in ("ldap", "nope"):
        try:
            identity.provider({"MOP_AUTH_PROVIDER": bad}, tmp)
            out.append(f"{bad!r} must be refused while there is no such provider")
        except ValueError as e:
            if bad not in str(e):
                out.append(f"the refusal must name the provider: {e}")
    return out


def check_deploy():
    """mop deploy отказывает до плейбука, если логин определён дважды или
    провайдер не читается: там отказ всего -- громкий и вовремя."""
    out = []
    from mop.cli.pool import deploy
    tmp = tempfile.mkdtemp(prefix="mop-test-identity-")
    path = os.path.join(tmp, "operators")
    with open(path, "w") as f:
        f.write(identity.format_line(Identity("olga", "user", ("mop",)),
                                     identity.hash_password("x")) + "\n")
    base = {"MOP_AUTH_PROVIDER": "file", "MOP_OPERATORS_FILE": path}
    fn = getattr(deploy, "operator_refusals", None)
    if fn is None:
        return ["mop deploy has no operator_refusals"]
    got = fn({**base, "MOP_OPERATORS": "olga:admin; anton:admin"}, tmp)
    if got != [f"login olga is defined both in MOP_OPERATORS and in {path}"]:
        out.append(f"a duplicate must stop deploy: {got}")
    if fn({**base, "MOP_OPERATORS": "anton:admin"}, tmp):
        out.append("no duplicates must not stop deploy")
    if not fn({**base, "MOP_AUTH_PROVIDER": "ldap", "MOP_OPERATORS": "anton:admin"}, tmp):
        out.append("an unknown provider must stop deploy")
    with open(path, "a") as f:
        f.write("broken line\n")
    if not fn({**base, "MOP_OPERATORS": "anton:admin"}, tmp):
        out.append("a broken operators file must stop deploy")
    return out


def main():
    failed = []
    for check in (check_identity, check_hash, check_provider, check_transition, check_choice,
                  check_deploy):
        try:
            lines = check()
        except Exception as e:  # noqa: BLE001 -- падение проверки -- тоже провал
            lines = [f"raised {type(e).__name__}: {e}"]
        failed += [f"FAIL {check.__name__}: {l}" for l in lines]
    if failed:
        print("\n".join(failed))
    print("identity: FAILED" if failed else "identity: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
