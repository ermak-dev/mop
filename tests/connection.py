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
    """fn поднимает отказ (ValueError или его наследников, BusError в их
    числе), чей текст содержит needles и не содержит секретов."""
    try:
        fn()
    except (ValueError, RuntimeError) as e:
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
# Инвариант ОДИН на обеих реализациях (базовой и исправленной), ожидание не
# меняется между прогонами: после complete -- успешного или сорвавшегося --
# эффективный вход каталога есть ЦЕЛИКОМ ALICE, ЦЕЛИКОМ BOB или его нет,
# НИКОГДА смесь. Саботаж второго старого writer допустим: в новой реализации
# он просто не вызывается, и вход завершается целиком. Наблюдение
# нормализовано: чтение каталога решателем (до правки -- поштучно из пары,
# после -- готовый профиль), смесь видна как «не ALICE и не BOB».
def effective(dest):
    """Эффективный вход каталога, нормализованный к форме профиля.

    На базовой реализации собирается из пары поштучно (как её читатели), на
    исправленной -- читается готовый профиль; источник не важен, важна
    ЦЕЛОСТНОСТЬ. Отказ решателя -- «нет входа» (None), не смесь."""
    try:
        return creds.human_profile(dest)
    except AttributeError:      # до правки решателя нет: читаем как база
        op, cl = creds.operator(dest), None
        try:
            cl = creds.client(dest)
        except ValueError:
            cl = None
        if op and cl:
            return {"user": op["user"], "password": op["password"],
                    "https_port": cl["https_port"], "proxy_url": cl["proxy_url"],
                    "proxy_key": cl["proxy_key"]}
        return None
    except ValueError:
        return None


def check_mixed_write_533(c):
    root = tempfile.mkdtemp(prefix="mop-conn-533-")
    dest = os.path.join(root, "srv.test")

    # Прерванный повторный вход нового человека: саботаж второго writer
    # (operator.json на базовой реализации, в новой он не вызывается).
    put_legacy(dest, ALICE)
    c.expect("#533 the old login is whole before the attempt",
             effective(dest), profile_of(ALICE))
    outcome = []
    with patched(join, _login=lambda *a: BOB[1], _still_valid=lambda *a: False,
                 _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                    "proxy_key": BOB[4]},
                 _probe=lambda *a: None), \
            patched(creds, write_operator=_sabotage("disk full")):
        try:
            join.complete("srv.test", "bob", dest, "8443")
            outcome.append("completed")
        except (RuntimeError, OSError) as e:
            outcome.append(f"refused: {e}")
            c.check("#533 an interrupted write surfaces its failure",
                    "disk full" in str(e), str(e))
    got = effective(dest)
    c.check("#533 after an interrupted login the directory holds one whole "
            "login, never a mix",
            got in (profile_of(ALICE), profile_of(BOB)),
            f"outcome {outcome}, effective {got}")

    # Успешный повторный вход: эффективный вход -- целиком новый.
    with patched(join, _login=lambda *a: BOB[1], _still_valid=lambda *a: False,
                 _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                    "proxy_key": BOB[4]},
                 _probe=lambda *a: None):
        join.complete("srv.test", "bob", dest, "8443")
    got = effective(dest)
    c.check("#533 a successful re-login switches the whole login",
            got == profile_of(BOB), f"effective {got}")
    c.check("#533 the new proxy travels with its own login",
            (got or {}).get("proxy_key") == BOB[4], f"effective {got}")

    # Приватность -- нормализованно: каталоги 0700, каждый файл входа 0600,
    # временных файлов не остаётся. Одно ожидание на обеих реализациях.
    for d in (root, dest):
        c.expect(f"#533 private directory {os.path.basename(d)}",
                 os.stat(d).st_mode & 0o777, 0o700)
    names = [n for n in os.listdir(dest) if not n.endswith(".tmp")]
    c.check("#533 every profile file is private",
            all(os.stat(os.path.join(dest, n)).st_mode & 0o777 == 0o600 for n in names),
            names)
    c.check("#533 no temporary file remains",
            not any(n.endswith(".tmp") for n in os.listdir(dest)), os.listdir(dest))

    # Неуспешная вторая проверка (проба ключа): ни нового файла, ни полузаписи,
    # прежний вход целиком на месте. Одно ожидание на обеих реализациях.
    with patched(join, _login=lambda *a: BOB[1], _still_valid=lambda *a: False,
                 _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                    "proxy_key": BOB[4]},
                 _probe=lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 401"))):
        try:
            join.complete("srv.test", "bob", dest, "8443")
            c.fail("#533 failed verification must refuse")
        except RuntimeError:
            pass
    c.check("#533 a failed verification keeps the previous login whole",
            effective(dest) in (profile_of(ALICE), profile_of(BOB)),
            f"effective {effective(dest)}")


def _sabotage(reason):
    """Заглушка-провал для старого writer: саботаж допустим в обеих
    реализациях, в новой он просто не вызывается."""
    def boom(*a, **k):
        raise RuntimeError(reason)
    return boom


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
           lambda: profile(a), "unsupported", "version")
    put_connection(a, ALICE, version="one")
    refute(c, "#533 a nonnumeric version refuses", lambda: profile(a), "version")
    # bool в Python равен единице: `true` в файле не версия 1, а чужой формат.
    put_connection(a, ALICE, version=True)
    refute(c, "#533 a boolean version is not schema version 1",
           lambda: profile(a), "version")
    with open(os.path.join(a, "connection.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    # Порча -- не «нет входа»: отказ называет файл и факт, без совета
    # удалить/править руками (мог бы стереть более новые данные) и без
    # заведомо неработающего «re-run mop login».
    refute(c, "#533 a corrupted file refuses without a legacy fallback",
           lambda: profile(a), "not valid JSON")
    # Нечитаемый файл -- ДРУГОЙ факт: «не JSON» оклеветало бы права как
    # порчу. Класс исключения безопасен и точен, ложный факт -- нет (#533).
    os.chmod(os.path.join(a, "connection.json"), 0)
    try:
        def unreadable():
            try:
                profile(a)
            except ValueError as e:
                if "not valid JSON" in str(e) and "PermissionError" in str(e):
                    raise AssertionError("a PermissionError is not bad JSON") from e
                raise
        refute(c, "#533 an unreadable file names cannot-read, not bad JSON",
               unreadable, "PermissionError", "cannot read")
    finally:
        os.chmod(os.path.join(a, "connection.json"), 0o600)
    # Порча не молчит и на уровне шины: server_config отказывает, а не
    # собирает конфиг из соседних остатков.
    with patched(creds, ROOT=root), context.use(context.resolve({"server": "a.test"}, {}, {})), \
            config.client_sources():
        refute(c, "#533 the bus refuses a corrupted profile",
               lambda: bus.server_config("a.test"), "not valid JSON")
    put_connection(a, ALICE)
    broken = payload_of(ALICE)
    broken["bus"] = "alice"          # не объект: секрета в тексте быть не должно
    creds.make_dir(a)
    with open(os.path.join(a, "connection.json"), "w", encoding="utf-8") as f:
        json.dump(broken, f)
    refute(c, "#533 a wrong-shaped record refuses",
           lambda: profile(a), "does not match version")


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
    # Половина -- не профиль решателя (None), а отказ «неполный вход» --
    # работа consumer'ов (#533): bus проверен ниже, запускатели -- своими
    # текстами в check_service_conflict_533.
    c.expect("#533 an operator-only legacy half is not a profile",
             profile(op_only), None)
    cl_only = os.path.join(root, "cl.test")
    creds.write_client(cl_only, ALICE[2], ALICE[3], ALICE[4])
    c.expect("#533 a client-only legacy half is not a profile",
             profile(cl_only), None)
    # Половина -- не вход и для шины: отказ «неполный вход», не конфиг из
    # того, что лежит (имя без пароля -- вход неизвестно кого).
    with patched(creds, ROOT=root), \
            context.use(context.resolve({"server": "op.test"}, {}, {})), \
            config.client_sources():
        refute(c, "#533 the bus refuses an incomplete login",
               lambda: bus.server_config("op.test"), "incomplete login", "mop login")

    # Порченая пара -- отказ, как сегодня у client(): не «нет входа».
    broken = os.path.join(root, "broken.test")
    put_legacy(broken, ALICE)
    with open(os.path.join(broken, creds.CLIENT_FILE), "w", encoding="utf-8") as f:
        f.write('{"proxy_key": "x"}')
    refute(c, "#533 a broken legacy pair refuses",
           lambda: profile(broken), "legacy pair")


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
    # отказывает, а не запускает сессию без прокси. Пустой каталог -- ДРУГОЙ
    # случай: там «войдите» годится, здесь вход сам запрещён; тексты отказов
    # обязаны их различать (#533).
    from mop.cli.core import _common
    from mop.client import models
    from mop.server.doctor import proxy as doctor
    for label, module, fn, refuse in (
            ("session_env", _common, lambda: _common.session_env(),
             ("separate", "mop login")),
            ("models", models, lambda: models.available(), ("separate",)),
            ("doctor", doctor, lambda: doctor._probe(), ("separate",))):
        for case, name in (("empty", "empty.test"), ("machine", "plain.test")):
            os.makedirs(os.path.join(root, name), exist_ok=True)
            with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN=name), \
                    context.use(context.resolve({"server": name}, {}, {})), \
                    config.client_sources():
                try:
                    fn()
                    c.fail(f"#533 {label} must refuse a {case} directory")
                except RuntimeError as e:
                    text = str(e)
                    if case == "machine":
                        c.check(f"#533 {label}: a machine directory names a "
                                f"separate operator account",
                                all(n in text for n in refuse), text)
                    else:
                        c.check(f"#533 {label}: an empty directory is not "
                                f"called a machine service directory",
                                "machine service" not in text and
                                "run mop login" in text, text)
    for name in ("empty.test",):
        try:
            os.rmdir(os.path.join(root, name))
        except OSError:
            pass


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

    def finish(host, who, directory, port, **kw):
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

    # Операторская половина без терминала: имя известно из остатка, пароль
    # сохранён -- восстановление без единого вопроса (#533: подсказки из
    # ОДНОГО снимка, имя не теряется).
    calls.clear()
    op_quiet = os.path.join(root, "quiet.test")
    creds.write_operator(op_quiet, "alice", "stale-bus")
    with patched(creds, ROOT=root), patched(join, complete=finish, git=lambda *a: None), \
            patched(context, clone_binding=lambda: {}), \
            context.use(context.resolve({"server": "quiet.test"}, {}, {})), \
            patched_env(MOP_BUS_PASSWORD="fresh-bus"), \
            patched(sys, stdin=io.StringIO()):
        join.ensure()
    c.expect("#533 an operator half recovers its own user without asking",
             calls, [("quiet.test", "alice", op_quiet, "443")])

    # Прокс-половина с явным именем и паролем из окружения: полный вход без
    # вопросов и без чтения старого ключа.
    calls.clear()
    root2 = os.path.join(tempfile.mkdtemp(prefix="mop-conn-recover2-"), "servers")
    cl2 = os.path.join(root2, "half2.test")
    creds.write_client(cl2, "9443", "https://half2.test/llm", "old-key")
    def finish2(host, who, directory, port, **kw):
        calls.append((host, who, directory, port))
        put_connection(directory, (who, "fresh-bus", port,
                                   f"https://{host}/llm", "fresh-key"))
    with patched(creds, ROOT=root2), patched(join, complete=finish2,
                                             git=lambda *a: None), \
            patched(context, clone_binding=lambda: {}), \
            context.use(context.resolve({"server": "half2.test", "user": "bob"}, {}, {})), \
            patched_env(MOP_BUS_PASSWORD="fresh-bus"), \
            patched(sys, stdin=io.StringIO()):
        join.ensure()
    c.expect("#533 a client half with an explicit login goes in full",
             calls, [("half2.test", "bob", cl2, "9443")])

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


# ── существующий испорченный/неизвестный профиль не перезаписывается ─────
# Recovery входа существует только для отсутствующего нового файла и
# неполной ВАЛИДНОЙ legacy-пары. Испорченный/неизвестный connection.json --
# отказ ДО сети и записи, во всех путях: complete, ensure, main. На базовой
# реализации каталог читается парой и вход молча лезет в сеть -- ровно это
# и должен поймать красный прогон.
def check_broken_profile_not_overwritten_533(c):
    for label, mutate in (
            ("version 2", lambda d: put_connection(d, ALICE, version=2)),
            ("malformed", lambda d: _corrupt(d))):
        root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-broken-"), "servers")
        host = label.replace(" ", "-") + ".test"
        dest = os.path.join(root, host)
        # Испорченный профиль лежит рядом со старой парой: ни старый пароль,
        # ни порт не читаются из него, файл не перезаписывается, сеть не зовётся.
        put_legacy(dest, ALICE)
        mutate(dest)
        raw = open(os.path.join(dest, "connection.json"), encoding="utf-8").read()
        before = snapshot(dest)
        touched = []
        with patched(creds, ROOT=root), \
                patched(join, _login=lambda *a: touched.append("_login") or "bus-x",
                         _still_valid=lambda *a: touched.append("_still_valid") or False,
                         _fetch=lambda *a: touched.append("_fetch") or
                         {"https_port": "443", "proxy_url": "https://x/llm",
                          "proxy_key": "x"},
                         _probe=lambda *a: touched.append("_probe"),
                         ask=lambda *a: touched.append("ask") or "alice",
                         git=lambda *a: None), \
                patched(creds, untrusted_cert=lambda *a: touched.append("cert") or None), \
                patched(join.bus, check=lambda *a: touched.append("bus.check")), \
                patched(creds, write_connection=lambda *a: touched.append("write")), \
                patched(sys, stderr=io.StringIO()), \
                context.use(context.resolve({"server": host, "user": "alice"}, {}, {})), \
                patched_env(MOP_BUS_PASSWORD="bus-x", MOP_SERVER_LAN=host,
                            MOP_HTTPS_PORT="443"):
            for path, call in (("complete", lambda: join.complete(
                                    host, "alice", dest, "443")),
                                ("ensure", lambda: join.ensure()),
                                ("main", lambda: join.main(["--server", host,
                                                            "--https-port", "443"]))):
                touched.clear()
                refusal, rc = None, None
                try:
                    rc = call()        # main отказывает кодом 1, не исключением
                except (RuntimeError, ValueError) as e:
                    refusal, rc = str(e), 1
                c.check(f"#533 {path} refuses a broken profile ({label}) "
                        f"before network and writing",
                        rc == 1 and not touched,
                        f"rc {rc}, touched {touched}, refusal {refusal}")
        c.expect(f"#533 {label}: the broken file bytes are unchanged",
                 open(os.path.join(dest, "connection.json"), encoding="utf-8").read(), raw)
        c.expect(f"#533 {label}: the directory is untouched", snapshot(dest), before)

        # Тот же испорченный каталог без явного логина и без терминала:
        # отказ обязан нести ИСХОДНУЮ причину (broken), без вопросов, без
        # чтения legacy-половин и без сети (#533: main не глотает класс).
        touched.clear()
        with patched(creds, ROOT=root), \
                patched(join, _login=lambda *a: touched.append("_login") or "bus-x",
                        _still_valid=lambda *a: touched.append("_still_valid") or False,
                        _fetch=lambda *a: touched.append("_fetch") or
                        {"https_port": "443", "proxy_url": "https://x/llm", "proxy_key": "x"},
                        _probe=lambda *a: touched.append("_probe"),
                        ask=lambda *a: touched.append("ask") or "alice",
                        git=lambda *a: None), \
                patched(creds, client=lambda *a: touched.append("client") or None), \
                patched(creds, operator=lambda *a: touched.append("operator") or None), \
                patched(sys, stderr=io.StringIO()), \
                context.use(context.resolve({}, {}, {})), \
                patched_env(MOP_BUS_PASSWORD="bus-x", MOP_SERVER_LAN=host,
                            MOP_HTTPS_PORT="443"):
            try:
                rc = join.main(["--server", host, "--https-port", "443"])
                refusal = None
            except (RuntimeError, ValueError) as e:
                rc, refusal = 1, str(e)
            err = sys.stderr.getvalue()
            c.check(f"#533 {path}: a broken profile with no named login fails "
                    f"with its own reason ({label})",
                    rc == 1 and "connection profile" in err and not touched
                    and "ask" not in touched,
                    f"rc {rc}, touched {touched}, stderr {err!r}")


def _corrupt(directory):
    """Записать в connection.json осмысленно испорченный новый формат."""
    with open(os.path.join(directory, "connection.json"), "w", encoding="utf-8") as f:
        f.write('{"version": 1, "bus": null}')   # не словарь bus -- мусор


# ── провал очистки после успешной записи не отменяет вход ───────────────
# После успешного write_connection провал очистки старых записей -- не отказ
# входа: новый профиль цел, TLS-пин не откатывается, провал назван честно.
def check_cleanup_failure_keeps_profile_533(c):
    if getattr(creds, "write_connection", None) is None:
        return      # до #533 записи две и cleanup внутри того же try: правки нет
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-cleanup-"), "servers")
    dest = os.path.join(root, "srv.test")
    put_legacy(dest, ALICE)
    # Старый ролевой пароль: именно его убирает очистка после записи.
    with open(os.path.join(dest, creds.pass_file(None)), "w", encoding="utf-8") as f:
        f.write("old role password\n")
    pin_path = creds.write_cert(dest, b"old-pin-der")
    pin_before = open(pin_path, encoding="utf-8").read()

    def boom_remove(path):
        raise OSError("cleanup refused")

    with patched(join, _login=lambda *a: BOB[1], _still_valid=lambda *a: False,
                 _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                    "proxy_key": BOB[4]},
                 _probe=lambda *a: None), \
            patched(os, remove=boom_remove):
        err = io.StringIO()
        with patched(sys, stderr=err):
            join.complete("srv.test", "bob", dest, "8443")
    c.check("#533 a failed cleanup is reported, not swallowed",
            "cleanup incomplete" in err.getvalue(), err.getvalue())
    got = creds.human_profile(dest)
    c.expect("#533 the new profile stays after a failed cleanup",
             got, profile_of(BOB))
    c.expect("#533 the TLS pin is not rolled back after the profile is written",
             open(pin_path, encoding="utf-8").read(), pin_before)


# ── битый operator.json -- не «половина пары», а отказ ──────────────────
# Публичный operator() молча даёт None для мусора; решатель обязан
# различать «файла нет» (recovery разрешён) и «файл не читается/не той
# формы» (recovery перезаписал бы неизвестно чью запись). Служебный
# operator.json, нечитаемый рядом с человеческим connection.json, тоже не
# имеет права превратиться в «оператора нет».
def check_broken_operator_533(c):
    for label, mutate in (
            ("malformed json", lambda d: _write_raw(d, creds.OPERATOR_FILE, "{oops")),
            ("wrong shape", lambda d: _write_raw(d, creds.OPERATOR_FILE,
                                                 '{"user": 1, "password": "x"}')),
            ("unreadable", lambda d: os.chmod(os.path.join(d, creds.OPERATOR_FILE), 0))):
        root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-op-"), "servers")
        host = label.replace(" ", "-") + ".test"
        dest = os.path.join(root, host)
        put_legacy(dest, ALICE)     # валидная пара, ломаем операторскую половину
        mutate(dest)
        touched = []
        with patched(creds, ROOT=root), \
                patched(join, _login=lambda *a: touched.append("_login") or "bus-x",
                         _still_valid=lambda *a: touched.append("_still_valid") or False,
                         _fetch=lambda *a: touched.append("_fetch") or
                         {"https_port": "443", "proxy_url": "https://x/llm", "proxy_key": "x"},
                         _probe=lambda *a: touched.append("_probe"),
                         ask=lambda *a: touched.append("ask") or "alice",
                         git=lambda *a: None), \
                patched(creds, write_connection=lambda *a: touched.append("write")), \
                context.use(context.resolve({"server": host, "user": "alice"}, {}, {})), \
                patched_env(MOP_BUS_PASSWORD="bus-x", MOP_SERVER_LAN=host):
            for path, call in (("reader", lambda: creds.human_profile(dest)),
                                ("ensure", lambda: join.ensure()),
                                ("main", lambda: join.main(["--server", host]))):
                touched.clear()
                refusal, rc = None, None
                try:
                    rc = call()
                except (RuntimeError, ValueError) as e:
                    refusal, rc = str(e), 1
                except AttributeError:
                    refusal = "no resolver on this implementation"
                c.check(f"#533 {path} refuses a broken operator record ({label}) "
                        f"before network, questions and writing",
                        rc == 1 and not touched,
                        f"rc {rc}, touched {touched}, refusal {refusal}")
        # Без явного логина и без терминала тот же битый operator.json --
        # исходная причина, ноль вопросов (#533: main не глотает класс).
        touched.clear()
        with patched(creds, ROOT=root), \
                patched(join, _login=lambda *a: touched.append("_login") or "bus-x",
                        _still_valid=lambda *a: touched.append("_still_valid") or False,
                        _fetch=lambda *a: touched.append("_fetch") or
                        {"https_port": "443", "proxy_url": "https://x/llm", "proxy_key": "x"},
                        _probe=lambda *a: touched.append("_probe"),
                        ask=lambda *a: touched.append("ask") or "alice",
                        git=lambda *a: None), \
                patched(creds, client=lambda *a: touched.append("client") or None), \
                patched(creds, operator=lambda *a: touched.append("operator") or None), \
                patched(sys, stderr=io.StringIO()), \
                context.use(context.resolve({}, {}, {})), \
                patched_env(MOP_BUS_PASSWORD="bus-x", MOP_SERVER_LAN=host):
            try:
                rc = join.main(["--server", host])
                refusal = None
            except (RuntimeError, ValueError) as e:
                rc, refusal = 1, str(e)
            err = sys.stderr.getvalue()
            c.check(f"#533 a broken operator record with no named login fails "
                    f"with its own reason ({label})",
                    rc == 1 and "operator record" in err and "ask" not in touched
                    and not touched,
                    f"rc {rc}, touched {touched}, stderr {err!r}")
        # Ничего не записано, файл оператора не тронут (права вернули для чтения).
        os.chmod(os.path.join(dest, creds.OPERATOR_FILE), 0o600)
        c.check(f"#533 {label}: no new profile is written",
                not os.path.exists(os.path.join(dest, getattr(
                    creds, "CONNECTION_FILE", "connection.json"))),
                os.listdir(dest))

    # Нечитаемый operator.json рядом с ВАЛИДНЫМ connection.json -- не «нет
    # оператора»: профиль не читается молча поверх неизвестной записи.
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-op2-"), "servers")
    dest = os.path.join(root, "srv.test")
    put_connection(dest, ALICE)
    _write_raw(dest, creds.OPERATOR_FILE, '{"user": "service", "password": "service-secret"}')
    os.chmod(os.path.join(dest, creds.OPERATOR_FILE), 0)
    try:
        refute(c, "#533 an unreadable operator next to a human profile refuses",
               lambda: creds.human_profile(dest), "operator")
    finally:
        os.chmod(os.path.join(dest, creds.OPERATOR_FILE), 0o600)


def _write_raw(directory, name, text):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        f.write(text)


# ── строгая схема version 1: неизвестные поля -- отказ, не потеря ────────
def check_schema_strict_533(c):
    profile = getattr(creds, "human_profile", None)
    if not c.check("#533 human_profile exists", profile is not None):
        return
    root = tempfile.mkdtemp(prefix="mop-conn-schema-")
    dest = os.path.join(root, "srv.test")
    cases = {
        "extra top field": {"extra": 1},
        "extra bus field": {"bus": {"user": "alice", "password": "p", "role": "x"}},
        "extra proxy field": {"proxy": {"url": "https://x/llm", "key": "k", "tag": 1}},
    }
    for label, patch_ in cases.items():
        payload = payload_of(ALICE)
        payload.update(patch_)
        creds.make_dir(dest)
        with open(os.path.join(dest, "connection.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(os.path.join(dest, "connection.json"), 0o600)
        try:
            profile(dest)
            c.fail(f"#533 unknown fields must refuse ({label})")
        except ValueError as e:
            c.check(f"#533 unknown fields refuse, not vanish ({label})",
                    "connection profile" in str(e), str(e))
            c.check(f"#533 the refusal carries no unworkable advice ({label})",
                    "re-run mop login" not in str(e), str(e))
        os.remove(os.path.join(dest, "connection.json"))
    # И версии/форма: совет «re-run mop login» заведомо неработоспособен.
    put_connection(dest, ALICE, version=2)
    try:
        profile(dest)
        c.fail("#533 an unknown version must refuse")
    except ValueError as e:
        c.check("#533 unknown version names the file, not a relogin",
                "srv.test" in str(e) and "re-run mop login" not in str(e), str(e))
        # Числовая версия -- названа: после строгой проверки типа это факт
        # формата, не содержимое файла.
        c.check("#533 a numeric version is named after the type check",
                "version 2" in str(e), str(e))
    # Значение версии может быть чем угодно -- от токена до чужих данных:
    # отказ называет тип/факт, но НЕ цитирует произвольное значение.
    put_connection(dest, ALICE, version="secret-token")
    try:
        profile(dest)
        c.fail("#533 a bogus version must refuse")
    except ValueError as e:
        c.check("#533 the refusal does not quote the bogus version value",
                "secret-token" not in str(e), str(e))
        c.check("#533 the refusal does not advise hand repair or removal",
                not any(w in str(e).lower() for w in ("repair", "remove", "edit")),
                str(e))
    _corrupt(dest)
    try:
        profile(dest)
        c.fail("#533 a corrupted file must refuse")
    except ValueError as e:
        c.check("#533 a corrupted file names the fact, without removal advice",
                "re-run mop login" not in str(e) and
                not any(w in str(e).lower() for w in ("repair", "remove")),
                str(e))


# ── подсказки login/порта берутся из нового профиля, не из старой пары ───
def check_hints_from_snapshot_533(c):
    calls, prompts = [], []

    def record_complete(host, login, dest, https, **kw):
        calls.append((login, https))

    def no_ask(*a, **k):
        prompts.append(a)
        return "should-not-be-asked"

    # Новый профиль bob:8443 + оставленная старая пара alice:443: без явных
    # user/port вход идёт НОВЫМ логином и портом.
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-hints-"), "servers")
    dest = os.path.join(root, "srv.test")
    put_legacy(dest, ALICE)
    put_connection(dest, BOB)
    with patched(creds, ROOT=root), patched(join, complete=record_complete,
                                            ask=no_ask, git=lambda *a: None, _bind=lambda *a: None), \
            context.use(context.resolve({"server": "srv.test"}, {}, {})), \
            patched_env(MOP_BUS_PASSWORD="bus-x", MOP_SERVER_LAN="srv.test"):
        join.main(["--server", "srv.test"])
    c.expect("#533 relogin hints come from the new profile: login", [x[0] for x in calls], [BOB[0]])
    c.expect("#533 relogin hints come from the new profile: port", [x[1] for x in calls], [BOB[2]])
    c.check("#533 a stored profile is not asked for again", not prompts, prompts)

    # Свежий каталог только с connection.json (пары нет): логин и порт --
    # из профиля, без вопросов.
    calls.clear()
    fresh_root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-hints2-"), "servers")
    fresh = os.path.join(fresh_root, "srv2.test")
    put_connection(fresh, BOB)
    with patched(creds, ROOT=fresh_root), patched(join, complete=record_complete,
                                                  ask=no_ask, git=lambda *a: None,
                                                  _bind=lambda *a: None), \
            context.use(context.resolve({}, {}, {})), \
            patched_env(MOP_BUS_PASSWORD="bus-x"):
        join.main(["--server", "srv2.test"])
    c.expect("#533 a connection-only directory hints its own login",
             [x[0] for x in calls], [BOB[0]])
    c.expect("#533 a connection-only directory hints its own port",
             [x[1] for x in calls], [BOB[2]])
    c.check("#533 no questions when the profile knows the login", not prompts, prompts)


# ── один снимок в complete: служебный каталог не перезаписывается ───────
# ── прямой complete на остатке: подсказка из ЕГО снимка, не параметр ────
def check_complete_partial_hint_533(c):
    # complete -- существующая точка входа (тесты и будущие командлеты зовут
    # его напрямую). Подсказка прежнего пароля приходит из ЕГО собственной
    # классификации на входе фазы, а не необязательным параметром: параметр
    # со значением по умолчанию None -- обход проверки, если звавший забыл
    # его передать (#533).
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-cph-"), "servers")
    dest = os.path.join(root, "srv.test")
    creds.write_operator(dest, "alice", "stale-bus")
    calls = []
    with patched(join, _still_valid=lambda *a: calls.append(("_still_valid", a[3])) or True,
                 _login=lambda *a: calls.append(("_login", a[1])) or "fresh-bus",
                 _fetch=lambda *a: {"https_port": "443", "proxy_url": "https://x/llm",
                                    "proxy_key": "k"},
                 _probe=lambda *a: None), \
            patched_env(MOP_BUS_PASSWORD="fresh-bus"):
        join.complete("srv.test", "alice", dest, "443")
    c.check("#533 a direct complete re-checks the stored half password",
            calls == [("_still_valid", {"user": "alice", "password": "stale-bus",
                                        "https_port": None})],
            calls)


# ── writer меняет ТОЛЬКО connection.json; соседи -- нетронуты ──────────
def check_writer_neighbors_533(c):
    if getattr(creds, "write_connection", None) is None:
        return      # до #533 writer -- пара записей; контракт нового writer'а
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-neigh-"), "servers")
    dest = os.path.join(root, "srv.test")
    put_legacy(dest, ALICE)
    with open(os.path.join(dest, creds.pass_file(None)), "w", encoding="utf-8") as f:
        f.write("old role password\n")
    pin_path = creds.write_cert(dest, b"old-pin-der")
    neighbors = {n: open(os.path.join(dest, n), encoding="utf-8").read()
                 for n in (creds.OPERATOR_FILE, creds.CLIENT_FILE,
                           os.path.basename(pin_path))}
    with patched(join, _login=lambda *a: BOB[1], _still_valid=lambda *a: False,
                 _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                    "proxy_key": BOB[4]},
                 _probe=lambda *a: None):
        join.complete("srv.test", "bob", dest, "8443")
    for name, was in neighbors.items():
        c.expect(f"#533 the writer does not touch {name}",
                 open(os.path.join(dest, name), encoding="utf-8").read(), was)
    c.check("#533 the cleanup removes only the role password",
            creds.pass_file(None) not in os.listdir(dest), os.listdir(dest))
    c.expect("#533 the new login is the connection profile alone",
             creds.human_profile(dest), profile_of(BOB))


# ── один снимок в complete: служебный каталог не перезаписывается ───────
def check_machine_race_533(c):
    # Два чтения -- два поколения: пре-чек operator() увидел пустоту, снимок
    # уже видит service. Решение одно: классификация каталога одним
    # snapshot, machine -- отказ до сети и записи.
    root = os.path.join(tempfile.mkdtemp(prefix="mop-conn-race-"), "servers")
    dest = os.path.join(root, "srv.test")
    put_machine(dest)
    answers = iter([None,                    # пре-чек operator() в complete
                    {"user": creds.SERVICE, "password": "service-secret"}])  # снимок

    def flaky_operator(directory):
        return next(answers, {"user": creds.SERVICE, "password": "service-secret"})

    touched = []
    with patched(creds, ROOT=root), patched(creds, operator=flaky_operator), \
            patched(join, _login=lambda *a: touched.append("_login") or "bus-x",
                    _fetch=lambda *a: touched.append("_fetch") or
                    {"https_port": "443", "proxy_url": "https://x/llm", "proxy_key": "x"},
                    _probe=lambda *a: touched.append("_probe")), \
            patched(creds, write_connection=lambda *a: touched.append("write")), \
            context.use(context.resolve({"server": "srv.test", "user": "alice"}, {}, {})):
        try:
            join.complete("srv.test", "alice", dest, "443")
            c.fail("#533 a machine-classified directory must refuse a human login")
        except (RuntimeError, ValueError) as e:
            c.check("#533 machine classification refuses before network and writing",
                    not touched, f"touched {touched}: {e}")
    c.check("#533 no human profile lands in the service directory",
            not os.path.exists(os.path.join(dest, "connection.json")), os.listdir(dest))


# ── провал ЛЮБОГО шага очистки после записи не отменяет вход ────────────
def check_cleanup_phase_covered_533(c):
    profile = getattr(creds, "human_profile", None)
    if profile is None:
        return      # до #533 решателя нет; поведение очистки не его тема
    for label, sabotage in (("listdir", "listdir"), ("remove", "remove")):
        root = os.path.join(tempfile.mkdtemp(prefix=f"mop-conn-clean-{label}-"), "servers")
        dest = os.path.join(root, "srv.test")
        put_legacy(dest, ALICE)
        with open(os.path.join(dest, creds.pass_file(None)), "w", encoding="utf-8") as f:
            f.write("old role password\n")
        pin_path = creds.write_cert(dest, b"old-pin-der")
        pin_before = open(pin_path, encoding="utf-8").read()
        real_listdir, real_remove = os.listdir, os.remove

        def boom_listdir(p):
            if p == dest:
                raise OSError("listdir refused")
            return real_listdir(p)

        def boom_remove(p):
            raise OSError("cleanup refused")

        sabotage_patch = patched(os, listdir=boom_listdir) if label == "listdir" \
            else patched(os, remove=boom_remove)
        crashed = None
        with patched(join, _login=lambda *a: BOB[1], _still_valid=lambda *a: False,
                     _fetch=lambda *a: {"https_port": BOB[2], "proxy_url": BOB[3],
                                        "proxy_key": BOB[4]},
                     _probe=lambda *a: None), sabotage_patch:
            err = io.StringIO()
            with patched(sys, stderr=err):
                try:
                    join.complete("srv.test", "bob", dest, "8443")
                except OSError as e:
                    # До правки listdir сидел в общем try: отказ очистки --
                    # «проваленный вход» исключением, без сообщения. Это и
                    # есть дефект; красный прогон фиксирует его вот так.
                    crashed = str(e)
        c.check(f"#533 a failed {label} cleanup is reported, not a failed login "
                f"(base crash: {crashed})",
                "cleanup incomplete" in err.getvalue(), err.getvalue())
        c.expect(f"#533 the new profile stays after a {label} cleanup failure",
                 creds.human_profile(dest), profile_of(BOB))
        c.expect(f"#533 the TLS pin survives a {label} cleanup failure",
                 open(pin_path, encoding="utf-8").read(), pin_before)


def main():
    c = Checks()
    check_mixed_write_533(c)
    check_reader_new_533(c)
    check_reader_legacy_533(c)
    check_service_conflict_533(c)
    check_human_login_refuses_machine_533(c)
    check_recover_533(c)
    check_two_servers_533(c)
    check_broken_profile_not_overwritten_533(c)
    check_cleanup_failure_keeps_profile_533(c)
    check_broken_operator_533(c)
    check_schema_strict_533(c)
    check_hints_from_snapshot_533(c)
    check_machine_race_533(c)
    check_complete_partial_hint_533(c)
    check_writer_neighbors_533(c)
    check_cleanup_phase_covered_533(c)
    return c.report("connection")


if __name__ == "__main__":
    sys.exit(main())
