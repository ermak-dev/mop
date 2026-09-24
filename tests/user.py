#!/usr/bin/env python3
"""Учётки людей в файле операторов: python3 tests/user.py

HYPOTHESIS (#218): у файла операторов (#205) нет правящей его команды:
человека в нём заводят руками, хеш -- кодом из питона, и MOP_OPERATORS
убрать нельзя, пока в файл нечем перенести её логины с их паролями.
SOLUTION: группа `mop user` -- add, passwd, delete и переходный import
(MOP_OPERATORS с нынешними паролями -> файл). Только на сервере, где лежит
файл; при провайдере ldap -- отказ, люди там в LDAP. Правка файла --
атомарно, 0600; копию для сервисов сервера (/etc/nats/identity) команда
обновляет сама, когда может, иначе одной строкой просит mop deploy.
STATUS: FIXED — see #218
"""
import contextlib
import io
import os
import stat
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import importlib  # noqa: E402

from mop import identity, natsconf  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="mop-test-user-")
FILE = os.path.join(identity.SECRETS, identity.OPERATORS_FILE)
INVENTORY = os.path.join(ROOT, "inventory.yaml")
NATS = os.path.join(ROOT, "nats")
COPY = os.path.join(NATS, "identity", identity.OPERATORS_FILE)
failed = []


def verb(name):
    return importlib.import_module(f"mop.cli.user.{name}")


def run(name, argv, stdin="", typed=()):
    """Командлет в процессе. -> (код, stdout, stderr). typed -- что человек
    набирает в getpass, по очереди."""
    module = verb(name)
    answers = list(typed)
    keep = (sys.stdin, getattr(module, "getpass", None))
    out, err = io.StringIO(), io.StringIO()
    sys.stdin = io.StringIO(stdin)
    if keep[1] is not None:
        module.getpass = lambda prompt="": answers.pop(0)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = module.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if not isinstance(e.code, int) and e.code:
                    err.write(str(e.code))
    finally:
        sys.stdin = keep[0]
        if keep[1] is not None:
            module.getpass = keep[1]
    return code, out.getvalue(), err.getvalue()


def check(what, ok, detail=""):
    if not ok:
        failed.append(f"{what}" + (f": {detail}" if detail else ""))


def provider(setting=""):
    return identity.PlainFileProvider(FILE, setting, identity.SECRETS)


def content():
    return open(FILE).read() if os.path.exists(FILE) else None


def authenticates(login, password):
    try:
        return provider().authenticate(login, password)
    except identity.Refused:
        return None


def main():
    for k in ("MOP_OPERATORS", "MOP_OPERATORS_FILE", "MOP_AUTH_PROVIDER"):
        os.environ.pop(k, None)
    os.environ["INVENTORY"] = INVENTORY
    natsconf.IDENTITY_DIR = os.path.join(NATS, "identity")

    # ── не сервер: ни secrets/, ни инвентаря ─────────────────────────────
    code, out, err = run("add", ["alice", "--role", "admin", "--stdin"], "pw\n")
    check("off the server: refused", code != 0 and "server" in err and content() is None,
          (code, out, err))
    os.makedirs(identity.SECRETS)
    code, out, err = run("add", ["alice", "--role", "admin", "--stdin"], "pw\n")
    check("secrets/ but no inventory: still not the server",
          code != 0 and "server" in err and content() is None, (code, out, err))
    open(INVENTORY, "w").write("all: {}\n")
    os.makedirs(NATS)

    # ── add ──────────────────────────────────────────────────────────────
    code, out, err = run("add", ["alice", "--role", "user", "--projects", "mop,web",
                                 "--name", "Alice Liddell", "--email", "alice@example.dev",
                                 "--stdin"], "pw-a1\n")
    who = authenticates("alice", "pw-a1")
    check("add: silent success", code == 0 and not out and not err, (code, out, err))
    check("add: the login authenticates with its password",
          who == identity.Identity("alice", "user", ("mop", "web"), "Alice Liddell",
                                   "alice@example.dev"), who)
    check("add: the file is 0600", content() is not None
          and stat.S_IMODE(os.stat(FILE).st_mode) == 0o600)
    check("add: the services' copy follows",
          os.path.exists(COPY) and open(COPY).read() == content()
          and stat.S_IMODE(os.stat(COPY).st_mode) == 0o600)
    with open(FILE, "a") as f:
        f.write("# a comment the operator wrote\n")
    before = content()
    code, out, err = run("add", ["alice", "--role", "admin", "--stdin"], "x\n")
    check("add: an existing login is refused", code != 0 and "alice" in err
          and content() == before, (code, err))

    code, out, err = run("add", ["bob", "--role", "admin"], typed=("pw-b", "pw-b"))
    check("add: the password asked twice", code == 0 and authenticates("bob", "pw-b")
          and authenticates("bob", "pw-b").role == "admin", (code, out, err))
    code, out, err = run("add", ["carol", "--role", "admin"], typed=("one", "two"))
    check("add: two different passwords are refused",
          code != 0 and provider().lookup("carol") is None, (code, err))
    code, out, err = run("add", ["carol", "--role", "admin", "--stdin"], "\n")
    check("add: an empty password is refused",
          code != 0 and provider().lookup("carol") is None, (code, err))
    code, out, err = run("add", ["dora", "--role", "user", "--projects", "*", "--stdin"], "pw\n")
    check("add: user on every project", code == 0
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
        check(f"add: {why} is refused", code != 0 and content() == before, (code, out, err))
    check("add: the comment survives", "# a comment the operator wrote" in content())

    # ── passwd ───────────────────────────────────────────────────────────
    code, out, err = run("passwd", ["alice", "--stdin"], "pw-a2\n")
    check("passwd: silent success", code == 0 and not out and not err, (code, out, err))
    check("passwd: the new password works, the old one does not",
          authenticates("alice", "pw-a2") and not authenticates("alice", "pw-a1"))
    check("passwd: the rest of the line stays",
          provider().lookup("alice") == identity.Identity(
              "alice", "user", ("mop", "web"), "Alice Liddell", "alice@example.dev"))
    code, out, err = run("passwd", ["bob"], typed=("pw-b2", "pw-b2"))
    check("passwd: asked twice", code == 0 and authenticates("bob", "pw-b2"), (code, err))
    code, out, err = run("passwd", ["nobody", "--stdin"], "x\n")
    check("passwd: an unknown login is refused", code != 0 and "nobody" in err, (code, err))

    # ── delete ───────────────────────────────────────────────────────────
    code, out, err = run("delete", ["bob"])
    check("delete: silent success", code == 0 and not out and not err, (code, out, err))
    check("delete: the login is gone", provider().lookup("bob") is None
          and provider().lookup("alice") is not None)
    check("delete: the copy follows", open(COPY).read() == content())
    code, out, err = run("delete", ["bob"])
    check("delete: an unknown login is refused", code != 0 and "bob" in err, (code, err))

    # ── import: MOP_OPERATORS с нынешними паролями ───────────────────────
    for login, pw in (("dan", "old-d"), ("eve", "old-e")):
        with open(os.path.join(identity.SECRETS, f"nats-op-{login}.pass"), "w") as f:
            f.write(pw + "\n")
    os.environ["MOP_OPERATORS"] = "dan:admin; eve:user:mop,web; zed:admin"
    before = content()
    code, out, err = run("import", [])
    check("import: a login without a password refuses everything",
          code != 0 and "zed" in err and content() == before, (code, err))
    os.environ["MOP_OPERATORS"] = "dan:admin; eve:user:mop,web"
    code, out, err = run("add", ["dan", "--role", "admin", "--stdin"], "x\n")
    check("add: a login of MOP_OPERATORS is refused", code != 0 and "MOP_OPERATORS" in err
          and content() == before, (code, err))
    code, out, err = run("import", [])
    check("import: says to drop MOP_OPERATORS, in one line",
          code == 0 and "MOP_OPERATORS" in out and len(out.strip().splitlines()) == 1
          and not err, (code, out, err))
    code, out, err = run("import", [])
    check("import: logins already in the file are refused", code != 0 and "dan" in err,
          (code, err))
    del os.environ["MOP_OPERATORS"]
    dan, eve = authenticates("dan", "old-d"), authenticates("eve", "old-e")
    check("import: the old password works, role and projects kept",
          dan == identity.Identity("dan", "admin", ("*",))
          and eve == identity.Identity("eve", "user", ("mop", "web")), (dan, eve))
    check("import: the others stay", provider().lookup("alice") is not None)

    # ── копию не обновить -- одна строка про deploy ──────────────────────
    natsconf.IDENTITY_DIR = os.path.join(ROOT, "absent", "identity")
    code, out, err = run("add", ["fay", "--role", "admin", "--stdin"], "pw\n")
    check("add: no way to the copy -- one line asking for mop deploy",
          code == 0 and "mop deploy" in out and len(out.strip().splitlines()) == 1
          and authenticates("fay", "pw"), (code, out, err))
    natsconf.IDENTITY_DIR = os.path.join(NATS, "identity")

    # ── ldap: люди в LDAP ────────────────────────────────────────────────
    os.environ["MOP_AUTH_PROVIDER"] = "ldap"
    before = content()
    for name, argv, stdin in (("add", ["gil", "--role", "admin", "--stdin"], "pw\n"),
                              ("passwd", ["alice", "--stdin"], "pw\n"),
                              ("delete", ["alice"], ""), ("import", [], "")):
        code, out, err = run(name, argv, stdin)
        check(f"{name}: refused with the ldap provider",
              code != 0 and "LDAP" in err and content() == before, (code, err))
    del os.environ["MOP_AUTH_PROVIDER"]

    # ── MOP_OPERATORS_FILE -- файл там, где его назвали ──────────────────
    elsewhere = os.path.join(ROOT, "ops")
    os.environ["MOP_OPERATORS_FILE"] = elsewhere
    code, out, err = run("add", ["hal", "--role", "admin", "--stdin"], "pw\n")
    check("add: MOP_OPERATORS_FILE names the file", code == 0 and os.path.exists(elsewhere)
          and provider().lookup("hal") is None, (code, err))
    del os.environ["MOP_OPERATORS_FILE"]

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("user: FAILED" if failed else "user: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
