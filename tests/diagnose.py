#!/usr/bin/env python3
"""Офлайн-диагностика конфигурации без пула: python3 tests/diagnose.py

Две половины (#536): `mop diagnose` объясняет ВЫБРАННЫЙ клиентский сервер
и состояние его профиля (под client_sources: без .env установки, как их
видит диспетчер), `mop server diagnose` -- ЛОКАЛЬНЫЕ настройки установки
(источник каждого значения) и источник её реестра проектов. Ни сети, ни
записей, ни секретов: решатели уже есть (creds.snapshot, projects.view_of,
config.effective), вторая копия их логики разъехалась бы с первой.

HYPOTHESIS (#536): объяснение «какой сервер выбран и в каком он состоянии»
требует читать каталоги руками и помнить схему; при этом typed readers уже
дают всё, кроме сводки, а их тексты отказов уже проверены на гигиену
(#533, #534).
SOLUTION: mop/client/diagnose.py и mop/server/diagnose.py возвращают
структурированные безопасные метаданные (статус, источник, путь, версия
схемы, источник реестра); два тонких commandlet'а печатают. rc 1 -- только
непригодные выбранные данные (broken/conflict/unreadable/ambiguous),
empty/partial/machine -- информационный 0 с явным not-ready.
STATUS: в работе (#536): прогон до реализации обязан быть красным.
"""
import io
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, context, creds, projects  # noqa: E402

# Секреты пробы: их появление в любом выводе/тексте -- провал гигиены.
ALICE = ("alice", "bus-alice", "443", "https://srv.test/llm", "key-alice")
SECRETS = ("bus-alice", "key-alice", "service-secret", "secret-token")


def put_connection(directory, row, version=1):
    creds.make_dir(directory)
    path = os.path.join(directory, creds.CONNECTION_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": version,
                   "bus": {"user": row[0], "password": row[1]},
                   "https_port": row[2],
                   "proxy": {"url": row[3], "key": row[4]}}, f)
    os.chmod(path, 0o600)


def put_registry_v1(dir_, origins=()):
    with open(os.path.join(dir_, "projects.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "projects": [{"origin": o} for o in origins]}, f)


def put_registry_legacy(dir_, origins=()):
    with open(os.path.join(dir_, "projects"), "w", encoding="utf-8") as f:
        f.write("\n".join(origins) + ("\n" if origins else ""))


def client_report():
    from mop.client import diagnose
    return diagnose.report()


def client_main(argv):
    from mop.cli.core import diagnose
    return diagnose.main(argv)


def server_main(argv):
    from mop.cli.server import diagnose
    return diagnose.main(argv)


# ── клиент: выбранный сервер, источник, статус профиля ──────────────────
def check_client_side_536(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-diag-"), "servers")
    dest = os.path.join(root, "srv.test")
    put_connection(dest, ALICE)
    reads = []
    real_snapshot = creds.snapshot

    def counting(directory):
        reads.append(directory)
        return real_snapshot(directory)

    with patched(creds, ROOT=root, snapshot=counting), config.client_sources(), \
            context.use(context.resolve({"server": "srv.test"}, {}, {})):
        got = client_report()
    c.check("#536 the client report names the selected server",
            got["server"] == "srv.test", got)
    c.check("#536 the client report names the server directory",
            got["directory"] == dest, got)
    c.check("#536 the client report names where the server came from",
            got["source"] in ("context", "env", "joined"), got)
    c.check("#536 a complete profile is complete",
            got["status"] == "complete", got)
    c.check("#536 the schema version is named",
            got["schema"] == creds.CONNECTION_VERSION, got)
    c.check("#536 only the selected directory is read",
            reads == [dest], reads)

    # Клиент не читает .env установки: сервер без context/env берётся из
    # единственного joined-каталога (приоритеты config не меняются). .env
    # подсунут свой: будь он виден, его MOP_SERVER_LAN выиграл бы выбор.
    solo = os.path.join(tempfile.mkdtemp(prefix="mop-diag2-"), "servers")
    only = os.path.join(solo, "solo.test")
    put_connection(only, ALICE)
    dot_env = os.path.join(tempfile.mkdtemp(prefix="mop-diag-env-"), ".env")
    with open(dot_env, "w", encoding="utf-8") as f:
        f.write("MOP_SERVER_LAN=from-dotenv.test\n")
    # forget(): кэш _file один на процесс, чужой .env не должен остаться
    # в нём ни до, ни после пробы.
    config.forget()
    with patched(creds, ROOT=solo), patched(config, ENV_FILE=dot_env), \
            config.client_sources(), \
            context.use(context.resolve({}, {}, {})), \
            patched_env(MOP_SERVER_LAN=None):
        got = client_report()
    config.forget()
    c.check("#536 a sole joined server is selected without installation .env",
            got["server"] == "solo.test" and got["source"] == "joined", got)

    # Два joined-каталога без явного выбора -- отказ, не первый попавший.
    two = os.path.join(tempfile.mkdtemp(prefix="mop-diag3-"), "servers")
    for name in ("a.test", "b.test"):
        put_connection(os.path.join(two, name), ALICE)
    with patched(creds, ROOT=two), config.client_sources(), \
            context.use(context.resolve({}, {}, {})), \
            patched_env(MOP_SERVER_LAN=None):
        try:
            client_report()
            c.fail("#536 an ambiguous selection must refuse")
        except RuntimeError as e:
            c.check("#536 ambiguity names both servers",
                    "a.test" in str(e) and "b.test" in str(e), str(e))


# ── клиент: статусы и гигиена ────────────────────────────────────────────
def check_status_and_hygiene_536(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-diag-st-"), "servers")
    cases = [
        ("complete", lambda d: put_connection(d, ALICE)),
        ("partial", lambda d: creds.write_operator(d, ALICE[0], ALICE[1])),
        ("empty", lambda d: creds.make_dir(d)),
        ("machine", lambda d: creds.write_operator(d, creds.SERVICE, "service-secret")),
    ]
    for want, make in cases:
        dest = os.path.join(root, f"{want}.test")
        make(dest)
        with patched(creds, ROOT=root), \
                context.use(context.resolve({"server": f"{want}.test"}, {}, {})):
            got = client_report()
        c.check(f"#536 a {want} directory reports {want}",
                got["status"] == want, got)
        c.check(f"#536 a {want} report carries no secrets",
                not any(s in json.dumps(got) for s in SECRETS), got)

    dest = os.path.join(root, "broken.test")
    put_connection(dest, ALICE, version=2)
    with patched(creds, ROOT=root), \
            context.use(context.resolve({"server": "broken.test"}, {}, {})):
        try:
            client_report()
            c.fail("#536 an unsupported profile must refuse")
        except RuntimeError as e:
            c.check("#536 the refusal carries no secret and no raw value",
                    not any(s in str(e) for s in SECRETS), str(e))

    dest = os.path.join(root, "conflict.test")
    creds.write_operator(dest, creds.SERVICE, "service-secret")
    put_connection(dest, ALICE)
    with patched(creds, ROOT=root), \
            context.use(context.resolve({"server": "conflict.test"}, {}, {})):
        try:
            client_report()
            c.fail("#536 a machine+human conflict must refuse")
        except RuntimeError as e:
            c.check("#536 the conflict is named without taking a side",
                    "separate" in str(e), str(e))

    dest = os.path.join(root, "type.test")
    put_connection(dest, ALICE, version="secret-token")
    with patched(creds, ROOT=root), \
            context.use(context.resolve({"server": "type.test"}, {}, {})):
        try:
            client_report()
            c.fail("#536 a non-numeric version must refuse")
        except RuntimeError as e:
            c.check("#536 a bogus version value is not quoted",
                    "secret-token" not in str(e), str(e))

    dest = os.path.join(root, "perm.test")
    put_connection(dest, ALICE)
    os.chmod(os.path.join(dest, creds.CONNECTION_FILE), 0)
    try:
        with patched(creds, ROOT=root), \
                context.use(context.resolve({"server": "perm.test"}, {}, {})):
            try:
                client_report()
                c.fail("#536 an unreadable profile must refuse")
            except RuntimeError as e:
                c.check("#536 an unreadable profile names cannot-read",
                        "cannot read" in str(e).lower(), str(e))
    finally:
        os.chmod(os.path.join(dest, creds.CONNECTION_FILE), 0o600)


# ── клиентский CLI: rc и информационные статусы ─────────────────────────
def check_client_cli_536(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-diag-cli-"), "servers")
    put_connection(os.path.join(root, "ok.test"), ALICE)
    creds.make_dir(os.path.join(root, "mt.test"))
    creds.write_operator(os.path.join(root, "mt.test"), creds.SERVICE, "service-secret")
    put_connection(os.path.join(root, "bad.test"), ALICE, version=2)
    for host, want_rc in (("ok.test", 0), ("mt.test", 0), ("bad.test", 1)):
        with patched(creds, ROOT=root), patched(sys, stderr=io.StringIO(),
                                                 stdout=io.StringIO()), \
                context.use(context.resolve({"server": host}, {}, {})):
            rc = client_main([])
        c.check(f"#536 {host}: exit {want_rc}",
                rc == want_rc, rc)
    # Информационный 0machine -- явный not-ready, не «вход состоялся».
    with patched(creds, ROOT=root), patched(sys, stderr=io.StringIO(),
                                             stdout=io.StringIO()), \
            context.use(context.resolve({"server": "mt.test"}, {}, {})):
        rc = client_main([])
        text = sys.stdout.getvalue()
    c.check("#536 a machine status is explicitly not-ready",
            rc == 0 and "machine" in text and "not-ready" in text, text)
    # --json: стабильная форма без секретов.
    with patched(creds, ROOT=root), patched(sys, stdout=io.StringIO()), \
            context.use(context.resolve({"server": "ok.test"}, {}, {})):
        client_main(["--json"])
        payload = json.loads(sys.stdout.getvalue())
    c.check("#536 --json is stable and secret-free",
            payload["status"] == "complete" and
            not any(s in json.dumps(payload) for s in SECRETS), payload)


# ── сервер: локальный реестр и источник настроек ─────────────────────────
def check_server_side_536(c):
    root = tempfile.mkdtemp(prefix="mop-diag-reg-")
    for label, make, want in (
            ("v1", lambda d: put_registry_v1(d, ("https://git.test/opus.git",)), "snapshot"),
            ("legacy", lambda d: put_registry_legacy(d, ("https://git.test/opus.git",)),
             "legacy pair"),
            ("absent", lambda d: None, "empty")):
        dir_ = os.path.join(root, label)
        os.makedirs(dir_, exist_ok=True)
        make(dir_)
        before = sorted(os.listdir(dir_))
        with patched(projects, SNAPSHOT_DIR=dir_), patched(sys, stdout=io.StringIO()), \
                context.use(context.resolve({"server": f"{label}.test"}, {}, {})):
            rc = server_main(["--json"])
            payload = json.loads(sys.stdout.getvalue())
        c.check(f"#536 the local registry source is {want}",
                rc == 0 and payload["registry"]["source"] == want, payload["registry"])
        c.check(f"#536 a {label} registry is not migrated by diagnostics",
                sorted(os.listdir(dir_)) == before, os.listdir(dir_))

    dir_ = os.path.join(root, "bad")
    os.makedirs(dir_, exist_ok=True)
    with open(os.path.join(dir_, "projects.json"), "w", encoding="utf-8") as f:
        f.write("{oops")
    before = sorted(os.listdir(dir_))
    with patched(projects, SNAPSHOT_DIR=dir_), patched(sys, stderr=io.StringIO()), \
            context.use(context.resolve({"server": "bad.test"}, {}, {})):
        rc = server_main([])
        text = sys.stderr.getvalue()
    c.check("#536 a broken local registry fails loudly",
            rc == 1 and "projects.json" in text and
            not any(s in text for s in SECRETS), text)
    c.check("#536 a failed diagnostics changes nothing on disk",
            sorted(os.listdir(dir_)) == before, os.listdir(dir_))


def main():
    c = Checks()
    check_client_side_536(c)
    check_status_and_hygiene_536(c)
    check_client_cli_536(c)
    check_server_side_536(c)
    return c.report("diagnose")


if __name__ == "__main__":
    sys.exit(main())
