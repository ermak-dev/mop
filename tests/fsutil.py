#!/usr/bin/env python3
"""Файловые примитивы без пула: python3 tests/fsutil.py

Запись приватного файла была скопирована пять раз, KEY=VALUE разбирался
тремя правилами (#153). Здесь -- одна реализация (mop/common/fsutil.py) и ответы
прежних разборщиков, снятые до правки: рефакторинг обязан их сохранить.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, ROOT)

from mop.common import config, creds, project_secrets, projects  # noqa: E402
from mop.server import natsconf  # noqa: E402

try:
    from mop.common import fsutil  # noqa: E402
except ImportError:
    fsutil = None

# HYPOTHESIS: пять копий записи расходятся: creds и реестр проектов пишут
# не атомарно (обрыв посреди записи -- усечённый файл кредов), creds не
# чинит права уже лежащего файла, driver кладёт временный файл по umask.
# Разборщиков KEY=VALUE три, и правила у них разные.
# SOLUTION: mop/common/fsutil.py -- write_private/write_atomic (временный рядом,
# fsync, rename; 0600 до первого байта), make_private_dir, read_kv, write_kv.
# Диалекта KEY=VALUE два, и оба нужны: .env (комментарии, кавычки, пробелы)
# и сырой -- vars.env, который мы пишем сами и обязаны прочитать байт в байт.
# RESULT: таблицы прежних ответов держатся целиком.
# STATUS: FIXED — see #153

# Диалект .env: config._parse (.env, node.env, secrets.env). Снято до правки.
ENV_TEXT = """# комментарий
  # комментарий с отступом
A=1
 B = spaced value
C="double"
D='single'
E="mixed'
F=a=b
G=v # не комментарий
export H=x
I
=nokey
J=
K=""x""
A=last
"""
ENV_WANT = {"A": "last", "B": "spaced value", "C": "double", "D": "single",
            "E": "mixed", "F": "a=b", "G": "v # не комментарий",
            "export H": "x", "": "nokey", "J": "", "K": "x"}

# Сырой диалект: project_secrets._vars (vars.env) и разбор пробы в агенте
# (agent.clone_facts: dict(l.split("=", 1) ...)). Снято до правки.
RAW_TEXT = "A= spaced \n#B=not a comment\n C=lead\nD=\"q\"\nE=a=b\nnoeq\nF=\n"
RAW_WANT = {"A": " spaced ", "#B": "not a comment", " C": "lead",
            "D": '"q"', "E": "a=b", "F": ""}


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def main():
    bad = cases = 0

    def check(what, got, want):
        nonlocal bad, cases
        cases += 1
        if got != want:
            bad += 1
            print(f"FAILED  {what}\n  wanted:  {want!r}\n  got: {got!r}")

    # ── характеризация прежних потребителей: держится до и после ─────────
    with tempfile.TemporaryDirectory() as d:
        env = os.path.join(d, ".env")
        with open(env, "w") as f:
            f.write(ENV_TEXT)
        check("config.read_env: .env dialect", config.read_env(env), ENV_WANT)
        check("config.read_env: no file", config.read_env(os.path.join(d, "no")), {})

        os.makedirs(os.path.join(d, "proj"))
        with open(os.path.join(d, "proj", project_secrets.VARS), "w") as f:
            f.write(RAW_TEXT)
        check("project_secrets vars.env: raw dialect",
              project_secrets._vars(d, "proj"), RAW_WANT)
        check("agent probe: raw dialect",
              dict(l.split("=", 1) for l in RAW_TEXT.splitlines() if "=" in l),
              RAW_WANT)
        # Круг: set_var пишет, _vars читает то же, пробелы и кавычки целы.
        project_secrets.set_var(d, "rt", "Z", ' "a b" ')
        project_secrets.set_var(d, "rt", "Y", "#x")
        check("vars.env round trip", project_secrets._vars(d, "rt"),
              {"Z": ' "a b" ', "Y": "#x"})
        with open(os.path.join(d, "rt", project_secrets.VARS)) as f:
            check("vars.env layout: sorted KEY=VALUE lines", f.read(),
                  'Y=#x\nZ= "a b" \n')

    # ── права и атомарность при распахнутом umask ────────────────────────
    old_umask = os.umask(0)
    try:
        with tempfile.TemporaryDirectory() as d:
            # Потребители: каждый обязан оставить 0600 и 0700.
            sub = os.path.join(d, "srv", "addr")
            creds.write_operator(sub, "u", "p")
            check("creds.write_operator: file 0600",
                  mode(os.path.join(sub, creds.OPERATOR_FILE)), 0o600)
            check("creds.make_dir: dir 0700", mode(sub), 0o700)
            check("creds.make_dir: parent 0700", mode(os.path.dirname(sub)), 0o700)
            # Лежащий с чужими правами файл закрывается, а не наследует их.
            cert = os.path.join(sub, creds.CERT_FILE)
            with open(cert, "w") as f:
                f.write("old")
            os.chmod(cert, 0o644)
            import ssl
            der = ssl.PEM_cert_to_DER_cert(_PEM)
            creds.write_cert(sub, der)
            check("creds.write_cert over a 0644 file: 0600", mode(cert), 0o600)

            nats = os.path.join(d, "users.conf")
            natsconf.write("x", nats)
            check("natsconf.write: 0600", mode(nats), 0o600)

            project_secrets.put_file(d, "p", "a/b.key", b"k")
            check("project_secrets file: 0600",
                  mode(os.path.join(d, "p", "files", "a", "b.key")), 0o600)
            check("project_secrets dir: 0700",
                  mode(os.path.join(d, "p", "files", "a")), 0o700)

            reg = os.path.join(d, "cfg", "projects")
            projects.write({"b", "a"}, reg)
            with open(reg) as f:
                check("projects.write: content", f.read(), "a\nb\n")
            check("projects.write: temp not left behind",
                  sorted(os.listdir(os.path.dirname(reg))), ["projects"])

            if fsutil is None:
                check("mop.fsutil exists", False, True)
            else:
                p = os.path.join(d, "priv", "f")
                fsutil.make_private_dir(os.path.dirname(p))
                check("make_private_dir: 0700", mode(os.path.dirname(p)), 0o700)
                fsutil.write_private(p, "one")
                check("write_private: 0600 under umask 0", mode(p), 0o600)
                # Атомарность: пишем поверх, а открытый дескриптор старого
                # файла видит старое целиком -- файл заменён, а не усечён.
                with open(p) as old:
                    fsutil.write_private(p, b"two")
                    check("write_private: old file not truncated", old.read(), "one")
                with open(p) as f:
                    check("write_private: new content", f.read(), "two")
                check("write_private: no temp left",
                      sorted(os.listdir(os.path.dirname(p))), ["f"])

                # Обрыв посреди записи: прежний файл цел, временного нет.
                # object() не пишется ни строкой, ни байтами: падение после
                # того, как временный файл уже открыт.
                try:
                    fsutil.write_private(p, object())
                except Exception:
                    pass
                with open(p) as f:
                    check("write_private: failed write keeps old file", f.read(), "two")
                check("write_private: failed write leaves no temp",
                      sorted(os.listdir(os.path.dirname(p))), ["f"])

                # write_atomic -- права по umask, как было у реестра.
                os.umask(0o022)
                q = os.path.join(d, "priv", "reg")
                fsutil.write_atomic(q, "x")
                check("write_atomic: mode by umask", mode(q), 0o644)
    finally:
        os.umask(old_umask)

    if fsutil is not None:
        check("read_kv: .env dialect", fsutil.read_kv(ENV_TEXT), ENV_WANT)
        check("read_kv raw: vars.env dialect", fsutil.read_kv(RAW_TEXT, raw=True),
              RAW_WANT)
        check("write_kv: sorted KEY=VALUE lines",
              fsutil.write_kv({"b": "2", "a": " 1 "}), "a= 1 \nb=2\n")
        check("write_kv: empty", fsutil.write_kv({}), "")

    # Копии живут только в fsutil.
    cases += 1
    copies = []
    for top, _, files in os.walk(os.path.join(ROOT, "mop")):
        for f in files:
            if f.endswith(".py") and f != "fsutil.py":
                path = os.path.join(top, f)
                with open(path) as fh:
                    text = fh.read()
                if "os.replace(" in text or "O_TRUNC" in text or 'partition("=")' in text:
                    copies.append(os.path.relpath(path, ROOT))
    # pve.py пишет в tar-поток тела, не на диск; session.py едет исходником
    # и пакета не импортирует; parse_var -- разбор аргумента, не файла.
    allowed = {"mop/driver/pve.py", "mop/session.py", "mop/common/project_secrets.py"}
    extra = sorted(set(copies) - allowed)
    if extra:
        bad += 1
        print(f"FAILED  file primitives copied outside fsutil: {extra}")

    # HYPOTHESIS (#170): natsconf.passwords и bootstrap.store заводят каталог
    # makedirs(..., 0o700): mode действует только на НОВЫЙ каталог, и уже
    # лежащий шире (0755) остаётся как был -- пароли шины и workspace
    # проектов читаются чужими.
    # SOLUTION: оба через fsutil.make_private_dir -- он доводит и лежащий.
    # STATUS: FIXED — see #170
    from mop.server import bootstrap
    consumers = (
        ("natsconf.passwords", lambda root: natsconf.passwords(["mop"], root)),
        ("bootstrap.store", lambda root: bootstrap.store(root, "mop", "- hosts: all\n  tasks: []\n")),
    )
    for what, run in consumers:
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "old")
            os.makedirs(old)
            os.chmod(old, 0o755)
            run(old)
            check(f"{what}: an existing 0755 directory is tightened", mode(old), 0o700)
            new = os.path.join(d, "new")
            run(new)
            check(f"{what}: a new directory is 0700", mode(new), 0o700)

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


_PEM = """-----BEGIN CERTIFICATE-----
MIIBszCCAVmgAwIBAgIUQ7Fm6h0m2sQJ4o3yZ8f8m0L1XmQwCgYIKoZIzj0EAwIw
-----END CERTIFICATE-----
"""

if __name__ == "__main__":
    sys.exit(main())
