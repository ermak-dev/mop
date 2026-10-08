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
import shutil
import subprocess
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


def put_legacy(directory, row):
    """Прежняя пара: operator.json + client.json, без connection.json."""
    creds.write_operator(directory, row[0], row[1])
    creds.write_client(directory, row[2], row[3], row[4])


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
    c.check("#536 a complete profile is complete",
            got["status"] == "complete", got)
    # Схема -- ФАКТ прочитанного файла, не константа на все случаи:
    # connection.json валидирован решателем, у него версия известна.
    c.check("#536 a validated connection names its actual schema",
            got["schema"] == creds.CONNECTION_VERSION, got)
    c.check("#536 only the selected directory is read",
            reads == [dest], reads)

    # Источник выбора не сворачивается: чем диагностика отличается от
    # угадывания -- clone против env против командной строки.
    with patched(creds, ROOT=root), \
            context.use(context.resolve({}, {"MOP_SERVER_LAN": "srv.test"}, {})):
        got = client_report()
    c.check("#536 an environment server says env, not context",
            got["source"] == "env", got)
    with patched(creds, ROOT=root), \
            context.use(context.resolve({}, {}, {"server": "srv.test"})):
        got = client_report()
    c.check("#536 a clone binding says clone",
            got["source"] == "clone", got)

    # Схема НЕ объявляется там, где файла с версией нет: пустой каталог,
    # машинный (operator.json без версии) и легаси-пара (client.json без
    # версии) не выдают себя за connection v1.
    for label, make in (("empty", lambda d: creds.make_dir(d)),
                        ("machine", lambda d: creds.write_operator(
                            d, creds.SERVICE, "service-secret")),
                        ("legacy", lambda d: put_legacy(d, ALICE))):
        # legacy-пара -- «human» классификатором (valid вход), но формат
        # её файлов -- не connection.json.
        target = os.path.join(root, f"{label}.test")
        make(target)
        with patched(creds, ROOT=root), \
                context.use(context.resolve({"server": f"{label}.test"}, {}, {})):
            got = client_report()
        c.check(f"#536 a {label} directory carries no schema claim",
                got["schema"] is None, got)
    # Валидная легаси-пара -- по-прежнему целый вход человека (snapshot
    # возвращал human и до #533), статус не «partial»:
    with patched(creds, ROOT=root), \
            context.use(context.resolve({"server": "legacy.test"}, {}, {})):
        got = client_report()
    c.check("#536 a legacy pair is a complete login of the old format",
            got["status"] == "complete" and got["format"] == "legacy pair", got)

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
    put_legacy(os.path.join(root, "old.test"), ALICE)
    creds.make_dir(os.path.join(root, "mt.test"))
    creds.write_operator(os.path.join(root, "mt.test"), creds.SERVICE, "service-secret")
    put_connection(os.path.join(root, "bad.test"), ALICE, version=2)
    for host, want_rc in (("ok.test", 0), ("old.test", 0), ("mt.test", 0), ("bad.test", 1)):
        with patched(creds, ROOT=root), patched(sys, stderr=io.StringIO(),
                                                 stdout=io.StringIO()), \
                context.use(context.resolve({"server": host}, {}, {})):
            rc = client_main([])
        c.check(f"#536 {host}: exit {want_rc}",
                rc == want_rc, rc)
    # Информационный 0 machine -- явный not-ready, не «вход состоялся».
    with patched(creds, ROOT=root), patched(sys, stderr=io.StringIO(),
                                             stdout=io.StringIO()), \
            context.use(context.resolve({"server": "mt.test"}, {}, {})):
        rc = client_main([])
        text = sys.stdout.getvalue()
    c.check("#536 a machine status is explicitly not-ready",
            rc == 0 and "machine" in text and "not-ready" in text, text)
    # Формат файла назван по факту: connection.json -- v1, легаси-пара -- не
    # выдаёт себя за схему.
    with patched(creds, ROOT=root), patched(sys, stdout=io.StringIO()), \
            context.use(context.resolve({"server": "old.test"}, {}, {})):
        rc = client_main([])
        text = sys.stdout.getvalue()
    c.check("#536 a legacy pair prints its format, not a schema claim",
            rc == 0 and "legacy pair" in text and "schema version 1" not in text, text)
    # --json: стабильная форма без секретов.
    with patched(creds, ROOT=root), patched(sys, stdout=io.StringIO()), \
            context.use(context.resolve({"server": "ok.test"}, {}, {})):
        client_main(["--json"])
        payload = json.loads(sys.stdout.getvalue())
    c.check("#536 --json is stable and secret-free",
            payload["status"] == "complete" and
            not any(s in json.dumps(payload) for s in SECRETS), payload)


# ── диспетчер: политика клиента на живом bin/mop, ДО main ───────────────
def check_dispatcher_policy_536(c):
    """RED-фаза #536 (поправка 4): свойство НЕ helper'а, а диспетчера --
    main() накладывает client_sources ДО main командлета core. Реальный
    bin/mop в копии репозитория: установка с враждебным .env (существующий
    файл с чужим MOP_SERVER_LAN и ReadError-ловушкой), у человека --
    единственный joined-каталог. Правильный dispatch: .env не читается
    вовсе, диагностика называет joined-сервер; чтение файла -- отказ #535.

    Контроль отключённой политики: в TEMP-копии client_command() для
    mop.cli.core.diagnose выключен -> диспетчер читает .env -> ReadError
    (ловушка -- нечитаемый существующий файл). Так проверка ловит и
    «политика не доехала», и «политика не на ту команду».
    """
    from hermetic import child_env as hermetic_child_env  # noqa: F401
    # python -c обрабатывает stdout/stderr... используем прямой запуск.
    root = os.path.join(tempfile.mkdtemp(prefix="mop-diag-disp-"), "repo")
    shutil.copytree(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                    root, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".env",
                                                  "node_modules"))
    home = tempfile.mkdtemp(prefix="mop-diag-home-")
    servers = os.path.join(home, ".config", "mop", "servers")
    os.makedirs(os.path.join(servers, "joined.test"))
    put_connection(os.path.join(servers, "joined.test"), ALICE)
    # Ловушка .env: существует, но не читается -> ReadError #535, если
    # диспетчер его откроет. Клиентская политика обязана его НЕ тронуть.
    trap = os.path.join(root, ".env")
    with open(trap, "w", encoding="utf-8") as f:
        f.write("MOP_SERVER_LAN=hostile-from-dotenv.test\n")
    os.chmod(trap, 0)
    base = {k: v for k, v in os.environ.items() if not k.startswith("MOP_")}
    base["HOME"] = home
    base["PYTHONPATH"] = root
    try:
        # Правильный dispatch: политика клиента на mop diagnose, .env не
        # читается, сервер -- единственный joined.
        r = subprocess.run([sys.executable, "-m", "mop.cli", "diagnose"],
                           env=base, capture_output=True, text=True, cwd=root)
        c.check("#536 the dispatcher keeps the hostile .env away from mop diagnose",
                r.returncode == 0 and "joined.test" in r.stdout and
                "hostile" not in r.stdout + r.stderr,
                (r.returncode, r.stdout, r.stderr))
        # Тот же запуск без политики (server-глагол -- она не положена):
        # диспетчер открывает .env установки и натыкается на ловушку.
        r = subprocess.run([sys.executable, "-m", "mop.cli", "server", "diagnose"],
                           env=base, capture_output=True, text=True, cwd=root)
        c.check("#536 a server verb hits the ReadError trap in the .env",
                r.returncode != 0 and "cannot read settings" in r.stderr, r.stderr)
    finally:
        os.chmod(trap, 0o600)


# ── сервер: локальный реестр и источник настроек ─────────────────────────
def check_server_side_536(c):
    root = tempfile.mkdtemp(prefix="mop-diag-reg-")
    # Пути реестра -- от ВЫБРАННОГО каталога, не глобального SNAPSHOT:
    # каждый формат называет свои настоящие файлы.
    v1 = os.path.join(root, "v1")
    os.makedirs(v1)
    put_registry_v1(v1, ("https://git.test/opus.git",))
    with patched(projects, SNAPSHOT_DIR=v1), patched(sys, stdout=io.StringIO()), \
            context.use(context.resolve({"server": "v1.test"}, {}, {})):
        rc = server_main(["--json"])
        payload = json.loads(sys.stdout.getvalue())
    reg = payload["registry"]
    c.check("#536 a v1 registry names its own snapshot path",
            rc == 0 and reg["source"] == "snapshot" and
            reg["file"] == os.path.join(v1, "projects.json"), reg)
    c.check("#536 a v1 registry counts its visible projects",
            reg["projects"] == 1, reg)

    # Легаси-пара: два файла, оба названы; видимые имена -- включая голые
    # легаси-имена (snapshot_names), не только origin-записи.
    legacy = os.path.join(root, "legacy")
    os.makedirs(legacy)
    put_registry_legacy(legacy, ("https://git.test/opus.git", "bare-name"))
    before = sorted(os.listdir(legacy))
    with patched(projects, SNAPSHOT_DIR=legacy), patched(sys, stdout=io.StringIO()), \
            context.use(context.resolve({"server": "legacy.test"}, {}, {})):
        rc = server_main(["--json"])
        payload = json.loads(sys.stdout.getvalue())
    reg = payload["registry"]
    c.check("#536 a legacy pair names both its files",
            rc == 0 and reg["source"] == "legacy pair" and
            sorted(reg["files"]) == sorted(os.path.join(legacy, n)
                                           for n in ("projects", "limits.json")), reg)
    c.check("#536 visible projects include bare legacy names",
            reg["projects"] == 2, reg)   # opus (из origin) + bare-name
    c.check("#536 a legacy registry is not migrated by diagnostics",
            sorted(os.listdir(legacy)) == before, os.listdir(legacy))

    # Пустая установка: ожидаемые пути помечены, не выданы за существующие.
    absent = os.path.join(root, "absent")
    os.makedirs(absent)
    with patched(projects, SNAPSHOT_DIR=absent), patched(sys, stdout=io.StringIO()), \
            context.use(context.resolve({"server": "absent.test"}, {}, {})):
        rc = server_main(["--json"])
        payload = json.loads(sys.stdout.getvalue())
    reg = payload["registry"]
    c.check("#536 an absent registry is empty with expected paths",
            rc == 0 and reg["source"] == "empty" and
            reg["file"] == os.path.join(absent, "projects.json") and
            not os.path.exists(reg["file"]), reg)
    c.check("#536 an absent registry has no projects",
            reg["projects"] == 0, reg)

    # Глобальный путь не просачивается при подменённом каталоге.
    c.check("#536 an injected registry dir does not report the global path",
            reg["file"] != projects.SNAPSHOT, reg["file"])

    dir_ = os.path.join(root, "bad")
    os.makedirs(dir_)
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
    check_dispatcher_policy_536(c)
    return c.report("diagnose")


if __name__ == "__main__":
    sys.exit(main())
