#!/usr/bin/env python3
"""Атомарный клиентский профиль connection.json без пула: python3 tests/connection.py

Вход писал каталог двумя записями: operator.json, затем client.json. Отказ
между ними оставлял смешанный каталог -- пароль одного входа с портом и
ключом другого, -- и ни один читатель не мог это заметить: bus.server_config
собирает конфиг из обоих файлов поштучно.

HYPOTHESIS (#533): последовательные записи login.complete при промежуточном
отказе смешивают несвязанные входы, а читатели (bus.server_config, session_env,
models, doctor) читают каталог по частям и принимают смесь за вход.
SOLUTION: один атомарный writer -- connection.json {version, bus, https_port,
proxy} после обеих сетевых проверок; один решатель creds.human_profile для
всех читателей; legacy-пара читается только целиком и только пока нет нового
файла; machine operator.json (service) рядом с человеческим connection.json --
явный конфликт для всех, без выбора стороны; частичный legacy восстанавливается
только полным повторным входом.
STATUS: в работе (#533): прогон до исправления обязан быть красным.
"""
import io
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.pool import login as join  # noqa: E402
from mop.common import bus, config, context, creds  # noqa: E402

# Данные проб не секретны, но тексты отказов обязаны их не раскрывать: их
# присутствие в сообщении -- провал проверки гигиены.
ALICE = ("alice", "bus-alice", "443", "https://srv.test/llm", "key-alice")
BOB = ("bob", "bus-bob", "8443", "https://srv.test:8443/llm", "key-bob")
SECRETS = ("bus-alice", "bus-bob", "key-alice", "key-bob", "service-secret")


def profile_of(row):
    """Плоская форма профиля: одна на писателя и читателей."""
    user, password, https_port, proxy_url, proxy_key = row
    return {"user": user, "password": password, "https_port": https_port,
            "proxy_url": proxy_url, "proxy_key": proxy_key}


def payload_of(row, version=1):
    """Схема файла connection.json -- как её пишет writer."""
    user, password, https_port, proxy_url, proxy_key = row
    return {"version": version, "bus": {"user": user, "password": password},
            "https_port": https_port, "proxy": {"url": proxy_url, "key": proxy_key}}


def put_connection(directory, row, version=1):
    """Положить connection.json руками пробы (внутренняя схема -- часть контракта)."""
    creds.make_dir(directory)
    path = os.path.join(directory, "connection.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload_of(row, version), f)
    os.chmod(path, 0o600)
    return path


def put_machine(directory):
    """Машинный operator.json, какой кладёт deploy на контроллере."""
    creds.make_dir(directory)
    creds.write_operator(directory, creds.SERVICE, "service-secret")


def put_legacy(directory, row):
    """Прежняя пара: human operator.json + client.json."""
    creds.write_operator(directory, row[0], row[1])
    creds.write_client(directory, row[2], row[3], row[4])


def snapshot(directory):
    """Каталог как множество (имя -> содержимое): сравнение до/после."""
    return {n: open(os.path.join(directory, n), encoding="utf-8").read()
            for n in sorted(os.listdir(directory)) if not n.endswith(".tmp")}


def quiet_complete(dest, row=None, **kw):
    """complete без сети и без вопросов: обе проверки прошли. row -- чей вход."""
    record = ({"https_port": (row or ALICE)[2], "proxy_url": (row or ALICE)[3],
               "proxy_key": (row or ALICE)[4]})
    return patched(join, _login=lambda *a: (row or ALICE)[1],
                   _fetch=lambda *a: record,
                   _probe=lambda *a: None, **kw)


def refute(c, what, fn, *needles):
    """fn поднимает ValueError, чей текст содержит needles и не содержит секретов."""
    try:
        fn()
    except ValueError as e:
        text = str(e)
        missing = [n for n in needles if n not in text]
        leak = [s for s in SECRETS if s in text]
        c.check(f"{what}: refuses naming {', '.join(needles)}", not missing,
                f"text {text!r} lacks {missing}")
        c.check(f"{what}: no secrets in the message", not leak, f"text {text!r} leaks {leak}")
        return
    except Exception as e:  # не тот отказ -- тоже провал, но с именем
        c.fail(what, f"raised {type(e).__name__}: {e}")
        return
    c.fail(what, "no refusal: the reader accepted a broken record")


# ── дефект #533: смешение входов при промежуточном отказе ───────────────
def check_mixed_write_533(c):
    write = getattr(creds, "write_connection", None)
    profile = getattr(creds, "human_profile", None)
    c.check("#533 write_connection exists", write is not None)
    c.check("#533 human_profile exists", profile is not None)
    if not (write and profile):
        # До исправления: показать сам дефект. Сегодня provал записи
        # operator.json (вторая из двух) оставляет смесь -- пароль старого
        # входа с портом и ключом нового, -- и ни один читатель не видит
        # подмены: это не отказ, а ложный вход.
        root = tempfile.mkdtemp(prefix="mop-conn-defect-")
        dest = os.path.join(root, "srv.test")
        put_legacy(dest, ALICE)
        def boom(*a, **k):
            raise RuntimeError("disk full")
        with patched(join, _login=lambda *a: BOB[1], _still_valid=lambda *a: False,
                     _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                        "proxy_key": BOB[4]},
                     _probe=lambda *a: None), \
                patched(creds, write_operator=boom):
            try:
                join.complete("srv.test", "bob", dest, "8443")
                c.fail("#533 (defect) an interrupted login must surface the failure")
            except RuntimeError as e:
                c.check("#533 (defect) the interrupted write is the operator record",
                        "disk full" in str(e), str(e))
        op, cl = creds.operator(dest), creds.client(dest)
        c.expect("#533 (defect) the bus password stays the old login",
                 (op or {}).get("user"), ALICE[0])
        c.expect("#533 (defect) the client record is the new one",
                 (cl or {}).get("proxy_key"), BOB[4])
        c.check("#533 (defect) today a mixed directory reads as a valid login",
                op is not None and cl is not None,
                f"operator {op}, client {cl}")
        return
    root = tempfile.mkdtemp(prefix="mop-conn-533-")
    dest = os.path.join(root, "srv.test")

    # Старый вход лежит, повторный вход нового человека срывается на полпути:
    # сегодня вторая запись (operator.json) не доходит, и читатель собирает
    # пароль старого с портом нового. После исправления провал промежуточной
    # записи не существует: запись одна.
    put_legacy(dest, ALICE)
    sabotaged = []
    real_operator = creds.write_operator
    with patched(join, _login=lambda *a: BOB[1],
                 _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                    "proxy_key": BOB[4]},
                 _probe=lambda *a: None), \
            patched(creds, write_operator=lambda *a: sabotaged.append(a)):
        join.complete("srv.test", "bob", dest, "8443")
    c.check("#533 a complete login needs no second record", not sabotaged, sabotaged)
    got = profile(dest)
    c.expect("#533 the reader sees one whole login, not a mix",
             got, profile_of(BOB))
    c.expect("#533 new proxy travels with its own login", got["proxy_key"], BOB[4])

    # Одна запись на успешный вход: файл connection.json, 0600, без мусора.
    path = os.path.join(dest, "connection.json")
    c.check("#533 connection.json is written", os.path.isfile(path))
    if os.path.isfile(path):
        c.expect("#533 private profile file", os.stat(path).st_mode & 0o777, 0o600)
    for d in (os.path.dirname(dest), dest):
        c.expect(f"#533 private directory {os.path.basename(d)}",
                 os.stat(d).st_mode & 0o777, 0o700)
    c.check("#533 no temporary file remains",
            not any(n.endswith(".tmp") for n in os.listdir(dest)), os.listdir(dest))

    # Неуспешная проверка не оставляет ни нового файла, ни полузаписи.
    fresh = os.path.join(root, "fresh.test")
    os.makedirs(fresh)
    with patched(join, _login=lambda *a: BOB[1],
                 _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                    "proxy_key": BOB[4]},
                 _probe=lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 401"))):
        try:
            join.complete("fresh.test", "bob", fresh, "8443")
            c.fail("#533 failed verification must refuse")
        except RuntimeError:
            pass
    c.check("#533 a failed login writes nothing",
            not os.path.exists(os.path.join(fresh, "connection.json")),
            os.listdir(fresh))


# ── решатель: новая схема, порча, неизвестная версия ────────────────────
def check_reader_new_533(c):
    profile = getattr(creds, "human_profile", None)
    if not c.check("#533 human_profile exists", profile is not None):
        return
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-reader-"), "servers")
    a, b = os.path.join(root, "a.test"), os.path.join(root, "b.test")
    put_connection(a, ALICE)
    put_connection(b, BOB)
    c.expect("#533 first server keeps its profile", profile(a), profile_of(ALICE))
    c.expect("#533 second server is independent", profile(b), profile_of(BOB))
    c.expect("#533 fresh directory has no profile", profile(os.path.join(root, "c.test")), None)

    with patched(creds, ROOT=root), context.use(context.resolve({"server": "a.test"}, {}, {})), \
            config.client_sources():
        got = bus.server_config("a.test")
    c.expect("#533 the bus runs on the profile's port and login",
             (got["url"], got["user"], got["password"]),
             ("wss://a.test:443/nats", ALICE[0], ALICE[1]))

    put_connection(a, ALICE, version=2)
    refute(c, "#533 an unknown version refuses without a fallback",
           lambda: profile(a), "version", "mop login")
    put_connection(a, ALICE, version="one")
    refute(c, "#533 a nonnumeric version refuses", lambda: profile(a), "version")
    with open(os.path.join(a, "connection.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    refute(c, "#533 a corrupted file refuses without a legacy fallback",
           lambda: profile(a), "mop login")
    # Порча не молчит и на уровне шины: server_config отказывает, а не
    # собирает конфиг из соседних остатков.
    with patched(creds, ROOT=root), context.use(context.resolve({"server": "a.test"}, {}, {})), \
            config.client_sources():
        refute(c, "#533 the bus refuses a corrupted profile",
               lambda: bus.server_config("a.test"), "mop login")
    put_connection(a, ALICE)
    broken = payload_of(ALICE)
    broken["bus"] = "alice"          # не объект: секрета в тексте быть не должно
    creds.make_dir(a)
    with open(os.path.join(a, "connection.json"), "w", encoding="utf-8") as f:
        json.dump(broken, f)
    refute(c, "#533 a wrong-shaped record refuses", lambda: profile(a), "mop login")


# ── решатель: legacy-пара и её остатки ──────────────────────────────────
def check_reader_legacy_533(c):
    profile = getattr(creds, "human_profile", None)
    if not c.check("#533 human_profile exists", profile is not None):
        return
    root = tempfile.mkdtemp(prefix="mop-conn-legacy-")

    full = os.path.join(root, "full.test")
    put_legacy(full, ALICE)
    c.expect("#533 a legacy pair still reads as a whole profile",
             profile(full), profile_of(ALICE))
    with patched(creds, ROOT=root), \
            context.use(context.resolve({"server": "full.test"}, {}, {})), \
            config.client_sources():
        c.expect("#533 the bus still runs on a legacy login",
                 bus.server_config("full.test")["url"], "wss://full.test:443/nats")

    # Новый файл старше пары: лежит и парой, и новым -- читается только новый.
    put_connection(full, BOB)
    c.expect("#533 a new file shadows the legacy pair", profile(full), profile_of(BOB))

    op_only = os.path.join(root, "op.test")
    creds.write_operator(op_only, ALICE[0], ALICE[1])
    refute(c, "#533 an operator-only legacy half is not a profile",
           lambda: profile(op_only), "mop login")
    cl_only = os.path.join(root, "cl.test")
    creds.write_client(cl_only, ALICE[2], ALICE[3], ALICE[4])
    refute(c, "#533 a client-only legacy half is not a profile",
           lambda: profile(cl_only), "mop login")

    # Порченая пара -- отказ, как сегодня у client(): не «нет входа».
    broken = os.path.join(root, "broken.test")
    put_legacy(broken, ALICE)
    with open(os.path.join(broken, creds.CLIENT_FILE), "w", encoding="utf-8") as f:
        f.write('{"proxy_key": "x"}')
    refute(c, "#533 a broken legacy pair refuses", lambda: profile(broken), "mop login")


# ── машинный operator.json: конфликт и служебный путь ───────────────────
def check_service_conflict_533(c):
    profile = getattr(creds, "human_profile", None)
    if not c.check("#533 human_profile exists", profile is not None):
        return
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-service-"), "servers")

    # Сосуществование machine operator и human connection -- конфликт для
    # всех читателей: ни service, ни человека молча не выбирают.
    both = os.path.join(root, "both.test")
    put_machine(both)
    put_connection(both, ALICE)
    before = snapshot(both)
    refute(c, "#533 machine + human is a conflict for the profile reader",
           lambda: profile(both), "conflict", "separate")
    with patched(creds, ROOT=root), context.use(context.resolve({"server": "both.test"}, {}, {})), \
            config.client_sources():
        refute(c, "#533 the bus does not pick a side of the conflict",
               lambda: bus.server_config("both.test"), "conflict", "separate")
    c.expect("#533 reading a conflict changes nothing on disk", snapshot(both), before)

    # Service-only каталог обслуживается по прежнему bus-контракту: порт из
    # client.json, если лежит, иначе настройка; proxy не обязателен.
    plain = os.path.join(root, "plain.test")
    put_machine(plain)
    with patched(creds, ROOT=root), patched_env(MOP_HTTPS_PORT="9443"), \
            context.use(context.resolve({"server": "plain.test"}, {}, {})), \
            config.client_sources():
        got = bus.server_config("plain.test")
    c.expect("#533 a service machine connects on the configured port",
             (got["url"], got["user"]), ("wss://plain.test:9443/nats", creds.SERVICE))
    pinned = os.path.join(root, "pinned.test")
    put_machine(pinned)
    creds.write_client(pinned, "8443", "https://pinned.test/llm", "service-key")
    with patched(creds, ROOT=root), \
            context.use(context.resolve({"server": "pinned.test"}, {}, {})), \
            config.client_sources():
        c.expect("#533 a service machine keeps its client port",
                 bus.server_config("pinned.test")["url"], "wss://pinned.test:8443/nats")

    # Человеческая сессия на service-каталоге -- не вход: запускатель
    # отказывает, а не запускает сессию без прокси.
    from mop.cli.core import _common
    with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN="plain.test"), \
            context.use(context.resolve({"server": "plain.test"}, {}, {})), \
            config.client_sources():
        try:
            _common.session_env()
            c.fail("#533 session_env must refuse a machine-only login")
        except RuntimeError as e:
            c.check("#533 the refusal names login, not a silent launch",
                    "mop login" in str(e) or "login" in str(e), str(e))


# ── вход человека в конфликтный каталог отказывает до сети ──────────────
def check_human_login_refuses_machine_533(c):
    root = tempfile.mkdtemp(prefix="mop-conn-refuse-")
    dest = os.path.join(root, "srv.test")
    put_machine(dest)
    touched = []
    record = {"https_port": ALICE[2], "proxy_url": ALICE[3], "proxy_key": ALICE[4]}
    with patched(join, _login=lambda *a: touched.append("_login") or "bus-secret",
                 _fetch=lambda *a: touched.append("_fetch") or record,
                 _probe=lambda *a: touched.append("_probe")), \
            patched(creds, untrusted_cert=lambda *a: touched.append("cert") or None), \
            patched(join.bus, check=lambda *a: None):
        try:
            join.complete("srv.test", "alice", dest, "443")
            c.fail("#533 a human login into a service directory must refuse")
        except (RuntimeError, ValueError) as e:
            c.check("#533 the refusal names a separate account or directory",
                    "separate" in str(e).lower(), str(e))
    c.check("#533 the refusal happens before any network or file attempt",
            not touched, touched)
    c.check("#533 the refusal keeps the machine login untouched",
            creds.operator(dest), {"user": creds.SERVICE, "password": "service-secret"})
    c.check("#533 the refusal writes no human profile",
            not os.path.exists(os.path.join(dest, "connection.json")),
            os.listdir(dest))


# ── частичный legacy восстановим полным входом ──────────────────────────
def check_recover_533(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-recover-"), "servers")
    calls = []

    def finish(host, who, directory, port):
        calls.append((host, who, directory, port))
        put_connection(directory, (who, "fresh-bus", port,
                                   f"https://{host}/llm", "fresh-key"))

    # Операторская половина: пароль мог устареть, поэтому вход полный --
    # с проверкой шины, получением настроек и пробой ключа (вcomplete здесь
    # заглушен, но контракт -- полный вход, не дописывание пары).
    op_only = os.path.join(root, "srv.test")
    creds.write_operator(op_only, "alice", "stale-bus")
    with patched(creds, ROOT=root), patched(join, complete=finish, git=lambda *a: None), \
            patched(context, clone_binding=lambda: {}), \
            context.use(context.resolve({"server": "srv.test", "user": "alice"}, {}, {})), \
            patched_env(MOP_BUS_PASSWORD="fresh-bus"):
        join.ensure()
    c.expect("#533 an operator half is recovered by a full login",
             calls, [("srv.test", "alice", op_only, "443")])
    got = getattr(creds, "human_profile", lambda *a: None)(op_only) or {}
    c.expect("#533 after recovery the profile is whole", got.get("proxy_key"), "fresh-key")
    c.expect("#533 the stale half is not kept as a profile source",
             got.get("password"), "fresh-bus")

    # Прокс-половина: логин приходит контекстом, пароль спрашивается входом.
    calls.clear()
    cl_only = os.path.join(root, "half.test")
    creds.write_client(cl_only, "7443", "https://half.test/llm", "old-key")
    with patched(creds, ROOT=root), patched(join, complete=finish, git=lambda *a: None), \
            patched(context, clone_binding=lambda: {}), \
            context.use(context.resolve({"server": "half.test", "user": "bob"}, {}, {})), \
            patched_env(MOP_BUS_PASSWORD="fresh-bus"):
        join.ensure()
    c.expect("#533 a client half is recovered by a full login",
             calls, [("half.test", "bob", cl_only, "7443")])
    got = getattr(creds, "human_profile", lambda *a: None)(cl_only) or {}
    c.expect("#533 the old proxy key is not trusted", got.get("proxy_key"), "fresh-key")
    c.expect("#533 the recovered profile carries the context login",
             got.get("user"), "bob")

    # Готовый вход не трогается: повторный ensure молчит.
    calls.clear()
    with patched(creds, ROOT=root), patched(join, complete=finish, git=lambda *a: None), \
            patched(context, clone_binding=lambda: {}), \
            context.use(context.resolve({"server": "srv.test", "user": "alice"}, {}, {})):
        join.ensure()
    c.expect("#533 a complete login is left alone", calls, [])


# ── выбор сервера: discovery видит новый формат, неоднозначность -- отказ
def check_two_servers_533(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-two-"), "servers")
    a, b = os.path.join(root, "a.test"), os.path.join(root, "b.test")
    put_connection(a, ALICE)
    put_connection(b, BOB)
    with patched(creds, ROOT=root), config.client_sources(), \
            context.use(context.resolve({}, {}, {})):
        try:
            creds.joined_host()
            c.fail("#533 two joined servers must refuse")
        except RuntimeError as e:
            c.check("#533 ambiguity names both servers",
                    "a.test" in str(e) and "b.test" in str(e), str(e))
        os.remove(os.path.join(b, "connection.json"))
        try:
            c.expect("#533 discovery counts connection.json alone",
                     creds.joined_host(), "a.test")
        except RuntimeError as e:   # до исправления discovery пары не видит
            c.fail("#533 discovery counts connection.json alone", str(e))


def main():
    c = Checks()
    check_mixed_write_533(c)
    check_reader_new_533(c)
    check_reader_legacy_533(c)
    check_service_conflict_533(c)
    check_human_login_refuses_machine_533(c)
    check_recover_533(c)
    check_two_servers_533(c)
    return c.report("connection")


if __name__ == "__main__":
    sys.exit(main())
