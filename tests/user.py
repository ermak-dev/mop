#!/usr/bin/env python3
"""Учётки людей в файле операторов: python3 tests/user.py

HYPOTHESIS (#218): у файла операторов (#205) нет правящей его команды:
человека в нём заводят руками, хеш -- кодом из питона, и MOP_OPERATORS
убрать нельзя, пока в файл нечем перенести её логины с их паролями.
SOLUTION: группа `mop server user` -- add, passwd, delete и переходный import
(MOP_OPERATORS с нынешними паролями -> файл). Только на сервере, где лежит
файл; при провайдере ldap -- отказ, люди там в LDAP. Правка файла --
атомарно, 0600; копию для сервисов сервера (/etc/nats/identity) команда
обновляет сама, когда может, иначе одной строкой просит mop server deploy.
STATUS: FIXED — see #218
"""
import os
import stat
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import importlib  # noqa: E402

from mop.server import identity, natsconf  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="mop-test-user-")
FILE = os.path.join(identity.SECRETS, identity.OPERATORS_FILE)
INVENTORY = os.path.join(ROOT, "inventory.yaml")
NATS = os.path.join(ROOT, "nats")
COPY = os.path.join(NATS, "identity", identity.OPERATORS_FILE)


def verb(name):
    return importlib.import_module(f"mop.cli.server.user.{name}")


def run(name, argv, stdin="", typed=()):
    """Командлет в процессе. -> (код, stdout, stderr). typed -- что человек
    набирает в getpass, по очереди. Выход текстом (sys.exit("...")) --
    код 1 и этот текст в stderr, как у настоящего запуска."""
    module = verb(name)
    out, err, code = run_command(module.main, argv, stdin=stdin, typed=typed,
                                 module=module)
    if not isinstance(code, int):
        if code:
            err += str(code)
        code = 1
    return code, out, err


def provider():
    # Файл -- единственный источник людей (#219).
    return identity.PlainFileProvider(FILE)


def content():
    return open(FILE).read() if os.path.exists(FILE) else None


def authenticates(login, password):
    try:
        return provider().authenticate(login, password)
    except identity.Refused:
        return None


def main():
    c = Checks()
    for k in ("MOP_OPERATORS", "MOP_OPERATORS_FILE", "MOP_AUTH_PROVIDER"):
        os.environ.pop(k, None)
    os.environ["INVENTORY"] = INVENTORY
    natsconf.IDENTITY_DIR = os.path.join(NATS, "identity")

    # ── свежий сервер: инвентарь есть, secrets/ ещё нет (#238) ───────────
    # HYPOTHESIS: refusal() узнаёт сервер по двум признакам сразу -- secrets/
    # и инвентарь. secrets/ заводит только mop server deploy, а тот без людей
    # отказывает (#219): на свежем сервере mop server user add и mop server deploy
    # отказывают друг из-за друга.
    # SOLUTION: сервер узнаётся по инвентарю; secrets/ (0700) mop server user
    # заводит сам, там же, где пишет файл операторов.
    # STATUS: FIXED — see #238
    open(INVENTORY, "w").write("all: {}\n")
    code, out, err = run("add", ["root", "--role", "admin", "--stdin"], "pw-r\n")
    c.check("fresh server: add without secrets/ succeeds", code == 0, (code, out, err))
    c.check("fresh server: secrets/ is made 0700", os.path.isdir(identity.SECRETS)
          and stat.S_IMODE(os.stat(identity.SECRETS).st_mode) == 0o700)
    c.check("fresh server: the operators file is 0600 with the person",
          content() is not None and stat.S_IMODE(os.stat(FILE).st_mode) == 0o600
          and authenticates("root", "pw-r") is not None, content())
    # Дальше проверки ждут чистый дом: ни файла, ни secrets/, ни инвентаря.
    import shutil
    shutil.rmtree(identity.SECRETS, ignore_errors=True)
    os.remove(INVENTORY)

    # ── не сервер: ни secrets/, ни инвентаря ─────────────────────────────
    code, out, err = run("add", ["alice", "--role", "admin", "--stdin"], "pw\n")
    c.check("off the server: refused", code != 0 and "server" in err and content() is None,
          (code, out, err))
    c.check("off the server: the refusal names the inventory", "inventory.yaml" in err, err)
    os.makedirs(identity.SECRETS)
    code, out, err = run("add", ["alice", "--role", "admin", "--stdin"], "pw\n")
    c.check("secrets/ but no inventory: still not the server",
          code != 0 and "server" in err and content() is None, (code, out, err))
    open(INVENTORY, "w").write("all: {}\n")
    os.makedirs(NATS)

    # ── add ──────────────────────────────────────────────────────────────
    code, out, err = run("add", ["alice", "--role", "user", "--projects", "mop,web",
                                 "--name", "Alice Liddell", "--email", "alice@example.dev",
                                 "--stdin"], "pw-a1\n")
    who = authenticates("alice", "pw-a1")
    c.check("add: silent success", code == 0 and not out and not err, (code, out, err))
    c.check("add: the login authenticates with its password",
          who == identity.Identity("alice", "user", ("mop", "web"), "Alice Liddell",
                                   "alice@example.dev"), who)
    c.check("add: the file is 0600", content() is not None
          and stat.S_IMODE(os.stat(FILE).st_mode) == 0o600)
    c.check("add: the services' copy follows",
          os.path.exists(COPY) and open(COPY).read() == content()
          and stat.S_IMODE(os.stat(COPY).st_mode) == 0o600)
    with open(FILE, "a") as f:
        f.write("# a comment the operator wrote\n")
    before = content()
    code, out, err = run("add", ["alice", "--role", "admin", "--stdin"], "x\n")
    c.check("add: an existing login is refused", code != 0 and "alice" in err
          and content() == before, (code, err))

    code, out, err = run("add", ["bob", "--role", "admin"], typed=("pw-b", "pw-b"))
    c.check("add: the password asked twice", code == 0 and authenticates("bob", "pw-b")
          and authenticates("bob", "pw-b").role == "admin", (code, out, err))
    code, out, err = run("add", ["carol", "--role", "admin"], typed=("one", "two"))
    c.check("add: two different passwords are refused",
          code != 0 and provider().lookup("carol") is None, (code, err))
    code, out, err = run("add", ["carol", "--role", "admin", "--stdin"], "\n")
    c.check("add: an empty password is refused",
          code != 0 and provider().lookup("carol") is None, (code, err))
    code, out, err = run("add", ["dora", "--role", "user", "--projects", "*", "--stdin"], "pw\n")
    c.check("add: user on every project", code == 0
          and provider().lookup("dora").projects == ("*",), (code, err))

    for argv, why in ((["carol", "--role", "root"], "an unknown role"),
                      (["carol", "--role", "admin", "--projects", "mop"], "admin with projects"),
                      (["carol", "--role", "user"], "user without projects"),
                      (["carol"], "no role"),
                      (["admin", "--role", "admin"], "a role name of the bus"),
                      (["node-x", "--role", "admin"], "a machine prefix"),
                      (["ca:rol", "--role", "admin"], "a colon in the login"),
                      (["carol", "--role", "admin", "--name", "a:b"], "a colon in the name"),
                      (["carol", "--role", "admin", "--frobnicate"], "an unknown flag")):
        before = content()
        code, out, err = run("add", argv + ["--stdin"], "pw\n")
        c.check(f"add: {why} is refused", code != 0 and content() == before, (code, out, err))
    c.check("add: the comment survives", "# a comment the operator wrote" in content())

    # ── passwd ───────────────────────────────────────────────────────────
    code, out, err = run("passwd", ["alice", "--stdin"], "pw-a2\n")
    c.check("passwd: silent success", code == 0 and not out and not err, (code, out, err))
    c.check("passwd: the new password works, the old one does not",
          authenticates("alice", "pw-a2") and not authenticates("alice", "pw-a1"))
    c.check("passwd: the rest of the line stays",
          provider().lookup("alice") == identity.Identity(
              "alice", "user", ("mop", "web"), "Alice Liddell", "alice@example.dev"))
    code, out, err = run("passwd", ["bob"], typed=("pw-b2", "pw-b2"))
    c.check("passwd: asked twice", code == 0 and authenticates("bob", "pw-b2"), (code, err))
    code, out, err = run("passwd", ["nobody", "--stdin"], "x\n")
    c.check("passwd: an unknown login is refused", code != 0 and "nobody" in err, (code, err))

    # ── delete ───────────────────────────────────────────────────────────
    code, out, err = run("delete", ["bob"])
    c.check("delete: silent success", code == 0 and not out and not err, (code, out, err))
    c.check("delete: the login is gone", provider().lookup("bob") is None
          and provider().lookup("alice") is not None)
    c.check("delete: the copy follows", open(COPY).read() == content())
    code, out, err = run("delete", ["bob"])
    c.check("delete: an unknown login is refused", code != 0 and "bob" in err, (code, err))

    # ── import: MOP_OPERATORS с нынешними паролями ───────────────────────
    for login, pw in (("dan", "old-d"), ("eve", "old-e")):
        with open(os.path.join(identity.SECRETS, f"nats-op-{login}.pass"), "w") as f:
            f.write(pw + "\n")
    os.environ["MOP_OPERATORS"] = "dan:admin; eve:user:mop,web; zed:admin"
    before = content()
    code, out, err = run("import", [])
    c.check("import: a login without a password refuses everything",
          code != 0 and "zed" in err and content() == before, (code, err))
    os.environ["MOP_OPERATORS"] = "dan:admin; eve:user:mop,web"
    code, out, err = run("add", ["dan", "--role", "admin", "--stdin"], "x\n")
    c.check("add: a login of MOP_OPERATORS is refused", code != 0 and "MOP_OPERATORS" in err
          and content() == before, (code, err))
    code, out, err = run("import", [])
    c.check("import: says to drop MOP_OPERATORS, in one line",
          code == 0 and "MOP_OPERATORS" in out and len(out.strip().splitlines()) == 1
          and not err, (code, out, err))
    code, out, err = run("import", [])
    c.check("import: logins already in the file are refused", code != 0 and "dan" in err,
          (code, err))
    del os.environ["MOP_OPERATORS"]
    dan, eve = authenticates("dan", "old-d"), authenticates("eve", "old-e")
    c.check("import: the old password works, role and projects kept",
          dan == identity.Identity("dan", "admin", ("*",))
          and eve == identity.Identity("eve", "user", ("mop", "web")), (dan, eve))
    c.check("import: the others stay", provider().lookup("alice") is not None)

    # ── копию не обновить -- одна строка про deploy ──────────────────────
    natsconf.IDENTITY_DIR = os.path.join(ROOT, "absent", "identity")
    code, out, err = run("add", ["fay", "--role", "admin", "--stdin"], "pw\n")
    c.check("add: no way to the copy -- one line asking for mop server deploy",
          code == 0 and "mop server deploy" in out and len(out.strip().splitlines()) == 1
          and authenticates("fay", "pw"), (code, out, err))
    natsconf.IDENTITY_DIR = os.path.join(NATS, "identity")

    # ── ldap: люди в LDAP ────────────────────────────────────────────────
    os.environ["MOP_AUTH_PROVIDER"] = "ldap"
    before = content()
    for name, argv, stdin in (("add", ["gil", "--role", "admin", "--stdin"], "pw\n"),
                              ("passwd", ["alice", "--stdin"], "pw\n"),
                              ("delete", ["alice"], ""), ("import", [], "")):
        code, out, err = run(name, argv, stdin)
        c.check(f"{name}: refused with the ldap provider",
              code != 0 and "LDAP" in err and content() == before, (code, err))
    # ── #232: file,ldap -- файл в цепочке, mop server user его правит ───────────
    # HYPOTHESIS: отказ «people live in LDAP» -- по одному провайдеру ldap;
    # при цепочке file,ldap локальных людей было бы нечем завести.
    # SOLUTION: отказ -- только когда file нет в цепочке. STATUS: FIXED — see #232
    for chain in ("file,ldap", "ldap,file"):
        os.environ["MOP_AUTH_PROVIDER"] = chain
        login = "ivy" if chain == "file,ldap" else "jon"
        code, out, err = run("add", [login, "--role", "admin", "--stdin"], "pw\n")
        c.check(f"add: works with {chain}", code == 0 and authenticates(login, "pw"),
              (code, out, err))
        code, out, err = run("passwd", [login, "--stdin"], "pw2\n")
        c.check(f"passwd: works with {chain}", code == 0 and authenticates(login, "pw2"),
              (code, out, err))
        code, out, err = run("delete", [login])
        c.check(f"delete: works with {chain}", code == 0 and provider().lookup(login) is None,
              (code, out, err))
    os.environ["MOP_AUTH_PROVIDER"] = "ldap,nope"
    code, out, err = run("add", ["kit", "--role", "admin", "--stdin"], "pw\n")
    c.check("add: an unknown link is refused, named", code != 0 and "nope" in err
          and provider().lookup("kit") is None, (code, out, err))
    del os.environ["MOP_AUTH_PROVIDER"]

    # ── MOP_OPERATORS_FILE -- файл там, где его назвали ──────────────────
    elsewhere = os.path.join(ROOT, "ops")
    os.environ["MOP_OPERATORS_FILE"] = elsewhere
    code, out, err = run("add", ["hal", "--role", "admin", "--stdin"], "pw\n")
    c.check("add: MOP_OPERATORS_FILE names the file", code == 0 and os.path.exists(elsewhere)
          and provider().lookup("hal") is None, (code, err))
    del os.environ["MOP_OPERATORS_FILE"]

    return c.report("user")


if __name__ == "__main__":
    sys.exit(main())
