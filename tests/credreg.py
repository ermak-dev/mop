#!/usr/bin/env python3
"""Реестр кредитов без сервера: python3 tests/credreg.py

Кредит -- именованная авторизация у провайдера LLM (эпик #281). Здесь
проверяется чистая часть реестра (#283): запись кредита, строки для
`mop cred list`, слияние пробы со статусом, решение о продлении токена.
Сеть, pty и шина остаются живому серверу.

HYPOTHESIS: кредита как сущности нет -- логин claude.ai живёт копией файла
оператора по телам, ключ GLM строкой в .env; кому принадлежит, сколько
осталось и когда сброс, не знает никто, и `mop list` узнаёт о беде по
провалу хода.
SOLUTION: mop/common/credreg.py -- запись `cred.json` и её чтение в строки;
mop/server/credreg.py -- дома кредитов на сервере, проба через хук профиля,
продление токена самим клиентом; глаголы cred_* сервиса кластера.
STATUS: FIXED — see #283
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, Msg, patched  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.common import credreg, fsutil  # noqa: E402
from mop.common.domain import CredStatus  # noqa: E402

NOW = 1_790_500_000


# ── #295: вход со страницы -- только для кредита из реестра ───────────
# HYPOTHESIS: credreg.login_start проверяет лишь синтаксис имени: для имени
# вне реестра заводит дом и клиента, а удачный код через register_login
# заводит новый кредит -- добавление мимо `mop cred` (решение 27.09, #294);
# брошенный вход оставляет каталог, который реестр показывает строкой.
# SOLUTION: login_start отказывает, если записи нет, до дома и клиента;
# новый кредит claude заводит только `mop cred login` на сервере, который
# ведёт драйвер сам и сюда не ходит. STATUS: FIXED — see #295
def check_login_start_registry_295(c):
    import tempfile
    from mop.server import credreg as srv
    started = []

    class FakeLogin:
        url = "https://claude.com/x"

        @classmethod
        def start(cls, home, mode):
            started.append((os.path.basename(home), mode))
            return cls()

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        old_root, old_start = srv.ROOT, srv.credlogin.Login.start
        srv.ROOT = tmp
        srv.credlogin.Login.start = FakeLogin.start
        try:
            try:
                srv.login_start("nobody")
                c.fail("#295 login_start must refuse a name outside the registry")
            except RuntimeError as e:
                c.check("#295 the refusal names the command that adds one",
                        "mop cred login" in str(e), str(e))
            c.expect("#295 no home is created for an unknown name", sorted(os.listdir(tmp)), [])
            c.expect("#295 no client is started for an unknown name", started, [])
            srv.save(credreg.record("anton", "claude", "login", now=NOW))
            c.expect("#295 a registered credential still logs in",
                     srv.login_start("anton"), "https://claude.com/x")
            c.expect("#295 the client runs in that credential's home", started, [("anton", "login")])
        finally:
            srv.ROOT, srv.credlogin.Login.start = old_root, old_start
            srv._logins.clear()


# ── #330: вход claude -- только для кредита профиля claude ────────────
# HYPOTHESIS: login_start сверяет имя и наличие записи (#295), но не её
# профиль: страница прячет кнопку у GLM сама (canAuthorize), а сервер по
# /api/creds/login/start и глаголу cred_login_start запускает `claude auth
# login` в доме кредита glm, и удачный код кладёт туда .credentials.json.
# SOLUTION: login_start отказывает записи не профиля claude -- до клиента и
# до снятия прежнего незавершённого логина того же имени. STATUS: FIXED — see #330
def check_login_start_profile_330(c):
    import tempfile
    from mop.server import credreg as srv
    started, closed = [], []

    class FakeLogin:
        url = "https://claude.com/x"

        @classmethod
        def start(cls, home, mode):
            started.append((os.path.basename(home), mode))
            return cls()

        def close(self):
            closed.append(self)

    with tempfile.TemporaryDirectory() as tmp:
        old_root, old_start = srv.ROOT, srv.credlogin.Login.start
        srv.ROOT = tmp
        srv.credlogin.Login.start = FakeLogin.start
        try:
            srv.save(credreg.record("zai", "glm", "key", now=NOW))
            pending = FakeLogin()
            srv._logins["zai"] = pending
            try:
                srv.login_start("zai")
                c.fail("#330 login_start must refuse a glm credential")
            except RuntimeError as e:
                c.check("#330 the refusal names the profile",
                        "is a glm credential" in str(e) and "only claude" in str(e), str(e))
            c.expect("#330 no client is started for a glm credential", started, [])
            c.check("#330 a pending login of that name is left alone",
                    closed == [] and srv._logins.get("zai") is pending, (closed, srv._logins))
            srv.save(credreg.record("anton", "claude", "login", now=NOW))
            c.expect("#330 a claude credential still logs in",
                     srv.login_start("anton"), "https://claude.com/x")
            c.expect("#330 the client runs in its home", started, [("anton", "login")])
        finally:
            srv.ROOT, srv.credlogin.Login.start = old_root, old_start
            srv._logins.clear()


# ── #339: режим входа решает вид кредита, а не вызывающий ─────────────
# HYPOTHESIS: страница шлёт только имя, parse_login_start и глагол
# cred_login_start подставляют режим login до того, как login_start видит
# запись; у кредита вида token вход `claude auth login` сообщает успех, а
# запись остаётся token, secret() читает прежний файл, новый
# .credentials.json не продлевается и не раздаётся. Зеркально -- setup-token
# в доме login.
# SOLUTION: чистое правило login_mode(запись, режим): есть запись -- режим
# по виду (login -> login, token -> setup-token), явный противоречащий --
# отказ; записи нет -- спрошенный, по умолчанию login. login_start берёт
# режим из него, вызывающие больше не подставляют умолчание. STATUS: FIXED — see #339
def check_login_mode_339(c):
    import tempfile
    from mop.server import credreg as srv
    rule = getattr(srv, "login_mode", None)
    if c.check("#339 credreg.login_mode exists", rule is not None):
        tok = credreg.record("old", "claude", "token", now=NOW)
        log = credreg.record("anton", "claude", "login", now=NOW)
        c.expect("#339 a token record logs in with setup-token", rule(tok, None), ("setup-token", None))
        c.expect("#339 a login record logs in with login", rule(log, None), ("login", None))
        c.expect("#339 the matching mode is accepted", rule(tok, "setup-token"), ("setup-token", None))
        mode, refusal = rule(tok, "login")
        c.check("#339 a contradicting mode is refused, naming the right one",
                mode is None and "old is a token" in (refusal or "") and "setup-token" in refusal,
                (mode, refusal))
        mode, refusal = rule(log, "setup-token")
        c.check("#339 the mirror is refused too",
                mode is None and "anton is a login" in (refusal or ""), (mode, refusal))
        c.expect("#339 no record: the asked mode", rule(None, "setup-token"), ("setup-token", None))
        c.expect("#339 no record, nothing asked: login", rule(None, None), ("login", None))

    started, closed = [], []

    class FakeLogin:
        url = "https://claude.com/x"

        @classmethod
        def start(cls, home, mode):
            started.append((os.path.basename(home), mode))
            return cls()

        def close(self):
            closed.append(self)

    with tempfile.TemporaryDirectory() as tmp:
        old_root, old_start = srv.ROOT, srv.credlogin.Login.start
        srv.ROOT = tmp
        srv.credlogin.Login.start = FakeLogin.start
        try:
            srv.save(credreg.record("old", "claude", "token", now=NOW))
            srv.save(credreg.record("anton", "claude", "login", now=NOW))
            srv.login_start("old")
            srv.login_start("anton")
            c.expect("#339 login_start without a mode follows the kind", started,
                     [("old", "setup-token"), ("anton", "login")])
            started.clear()
            closed.clear()
            pending = srv._logins["old"]
            try:
                srv.login_start("old", "login")
                c.fail("#339 login_start must refuse login for a token credential")
            except RuntimeError as e:
                c.check("#339 the refusal names the kind", "is a token" in str(e), str(e))
            c.expect("#339 no client is started on a refused mode", started, [])
            c.check("#339 a pending login of that name is left alone",
                    closed == [] and srv._logins.get("old") is pending, (closed, srv._logins))
        finally:
            srv.ROOT, srv.credlogin.Login.start = old_root, old_start
            srv._logins.clear()

# ── #308: метка кредита -- подсказка, правда -- аренда ─────────────────
# HYPOTHESIS: метку `.local/state/mop/cred` пишет глагол `write`, который
# доступен мастеру любого проекта; note_turn берёт имя кредита из записи
# хода (turn["cred"]), и чужое имя в метке пометило бы чужой кредит
# needs_login или quota_wait.
# RESULT: через tick так не выходит и до правки -- tick с #284 зовёт
# note_turn только для записи, чья метка совпала с кредитом, держателей
# которого он перебирает. Дыра -- в самой note_turn (любой другой вызов
# верил бы записи) и в молчании: несовпавшая запись пропадала без следа, а
# `write` кладёт метку во ВСЕ тела узла, так что метку соседа папет получает
# и законной раздачей.
# SOLUTION: note_turn(name, запись) -- имя кредита даёт аренда, запись с
# другой меткой игнорируется и называется строкой журнала.
# STATUS: FIXED — see #308
def check_foreign_mark_308(c):
    import tempfile
    from mop.server import credreg as srv

    def turn(cred):
        return {"event": "StopFailure", "at": NOW, "error": "billing_error",
                "detail": "no credits", "cred": cred}

    with tempfile.TemporaryDirectory() as tmp:
        saved = (srv.ROOT, srv.holders, srv.turns_of, srv.probe, srv.materialize,
                 srv.distribute)
        srv.ROOT = tmp
        try:
            for name in ("anton", "other"):
                srv.save(credreg.record(name, "glm", "key", now=NOW - 100))
            srv.holders = lambda api=None: {"anton": {"pu-a-1": "n1"}}
            srv.probe = lambda name, now=None: None
            srv.materialize = lambda name, rec=None: []
            srv.distribute = lambda name, api=None, now=None: {}

            def status(name):
                return (srv.load(name).get("status") or {}).get("kind")

            # Через tick: держатель "anton", метка хода -- "other".
            srv.turns_of = lambda holding, api=None: {"pu-a-1": turn("other")}
            lines = srv.tick(now=NOW + 10)
            c.expect("#308 tick: a foreign mark leaves the holder's credential alone",
                     status("anton"), None)
            c.expect("#308 tick: a foreign mark does not reach the named credential",
                     status("other"), None)
            c.check("#308 tick: the ignored turn is named in the journal",
                    any("pu-a-1" in l and "other" in l and "ignored" in l for l in lines),
                    lines)
            # Запись хода живёт до следующего хода: называть её каждый круг --
            # шум в журнале, одного раза хватит.
            again = srv.tick(now=NOW + 70)
            c.check("#308 tick: the same ignored turn is named once, not every round",
                    not any("ignored" in l for l in again), again)
            # Прямо note_turn: имя кредита -- аргумент, не поле записи.
            try:
                got = srv.note_turn("anton", turn("other"), NOW + 10)
                c.check("#308 note_turn: a foreign mark is ignored, not attributed",
                        status("anton") is None and status("other") is None
                        and "ignored" in (got or ""), (got, status("anton"), status("other")))
                got = srv.note_turn("anton", turn("anton"), NOW + 10)
                c.expect("#308 note_turn: the holder's own mark lands as before",
                         status("anton"), "quota_wait")
            except (TypeError, AttributeError) as e:
                c.fail(f"#308 note_turn must take the credential's name from the lease: {e}")
            # Через tick, своя метка -- как раньше.
            srv.save(credreg.record("anton", "glm", "key", now=NOW - 100))
            srv.turns_of = lambda holding, api=None: {"pu-a-1": turn("anton")}
            srv.tick(now=NOW + 20)
            c.expect("#308 tick: the holder's own mark lands as before",
                     status("anton"), "quota_wait")
        finally:
            (srv.ROOT, srv.holders, srv.turns_of, srv.probe, srv.materialize,
             srv.distribute) = saved


# ── #315: глагол write -- одно определение в bus ──────────────────────
# HYPOTHESIS: разбор ответов write (OK / NOT REACHED / FAILED), кодировка
# [путь, b64] и WRITE_TIMEOUT живут копиями в server/credreg и client/keys,
# а distribute и turns_of обходят узлы руками вместо bus.request_many.
# Характеристика до переезда: что уходит агенту и какие строки выходят,
# снятая на уровне arequest -- его зовут оба пути, request и request_many.
# SOLUTION: bus.results_from, bus.as_file/from_file, bus.WRITE_TIMEOUT;
# distribute и turns_of -- через request_many. STATUS: FIXED — see #315
def check_write_fanout_315(c):
    import asyncio
    import base64
    import json
    import tempfile
    from nats.errors import NoRespondersError
    from mop.common import bus
    from mop.server import credreg as srv

    long_error = "agent is not allowed to write to " + "x" * 200
    sent = []

    async def arequest(_nc, subj, data, timeout):
        body = json.loads(data)
        sent.append((subj, body, timeout))
        node = next(n for n in ("n1", "n2", "n3", "n4") if subj.split(".").count(n))
        if body["verb"] == "write":
            if node == "n1":
                return Msg(body={"written": ["x"]})
            if node == "n2":
                return Msg(body={"error": long_error})
            if node == "n3":
                raise asyncio.TimeoutError()
            raise NoRespondersError()
        if node == "n1":
            return Msg(body={"puppets": {
                "pu-a-1": {"state": {"turn": {"cred": "anton", "at": NOW}}},
                "pu-a-2": {"state": {"turn": "not a dict"}},
                "pu-x-9": {"state": {}}}})
        if node == "n2":
            return Msg(body={"error": "no such verb"})
        raise asyncio.TimeoutError()

    files = [("a/b.json", b"{}"), ("c/d", b"key\n")]
    with tempfile.TemporaryDirectory() as tmp, \
            patched(bus, connect=lambda *a, **k: object(), arequest=arequest), \
            patched(srv, ROOT=tmp, materialize=lambda name, rec=None: files,
                    holders=lambda api=None: {"anton": {"pu-a-1": "n1", "pu-a-2": "n2",
                                                        "pu-a-3": "n3", "pu-a-4": "n4",
                                                        "pu-a-5": None, "pu-a-6": "n1"}}):
        srv.save(credreg.record("anton", "glm", "key", now=NOW - 100))
        got = srv.distribute("anton", now=NOW)
        c.expect("#315 distribute: OK, FAILED (cut at 120), NOT REACHED on silence",
                 {n: got.get(n) for n in ("n1", "n2", "n3")},
                 {"n1": "OK", "n2": "FAILED: " + long_error[:120],
                  "n3": "NOT REACHED: node agent n3 did not answer in 60s"})
        c.check("#315 distribute: no responders -> NOT REACHED naming the node",
                str(got.get("n4")).startswith("NOT REACHED: node agent n4 is not subscribed"),
                got.get("n4"))
        c.expect("#315 distribute: one answer per holding node", sorted(got), ["n1", "n2", "n3", "n4"])
        writes = [(s, b, t) for s, b, t in sent if b["verb"] == "write"]
        c.expect("#315 distribute: every node asked once, admin project, WRITE_TIMEOUT",
                 sorted((s, t) for s, _, t in writes),
                 sorted((bus.subject(n, "rpc", bus.ADMIN), 60) for n in ("n1", "n2", "n3", "n4")))
        c.expect("#315 distribute: files go as [path, b64]",
                 [b["files"] for _, b, _ in writes][0],
                 [[p, base64.b64encode(d).decode()] for p, d in files])
        c.expect("#315 distribute: the push is stamped in the record",
                 (srv.load("anton").get("pushed") or {}).get("nodes"), got)

        sent.clear()
        got = srv.turns_of({"pu-a-1": "n1", "pu-a-6": "n1", "pu-a-2": "n2",
                            "pu-a-3": "n3", "pu-a-5": None})
        c.expect("#315 turns_of: only dict turns, silent and failing nodes skipped",
                 got, {"pu-a-1": {"cred": "anton", "at": NOW}})
        c.expect("#315 turns_of: each node asked once for its own puppets",
                 sorted((s, b["verb"], b.get("names"), t) for s, b, t in sent),
                 sorted([(bus.subject("n1", "rpc", bus.ADMIN), "states", ["pu-a-1", "pu-a-6"], 20),
                         (bus.subject("n2", "rpc", bus.ADMIN), "states", ["pu-a-2"], 20),
                         (bus.subject("n3", "rpc", bus.ADMIN), "states", ["pu-a-3"], 20)]))

    with patched(srv, holders=lambda api=None: {}, materialize=lambda name, rec=None: files), \
            tempfile.TemporaryDirectory() as tmp, patched(srv, ROOT=tmp):
        srv.save(credreg.record("anton", "glm", "key", now=NOW - 100))
        c.expect("#315 distribute: no holders -> {} and nothing stamped",
                 (srv.distribute("anton", now=NOW), srv.load("anton").get("pushed")), ({}, None))


# ── #317: знание о кредитах -- в common, одним местом ─────────────────
# HYPOTHESIS (DRY после #262): путь файла кредов claude записан тремя
# написаниями (paths.CREDENTIALS, keys.CREDENTIALS, credlogin.credentials_path),
# его схема (claudeAiOauth/accessToken/expiresAt в мс) -- в common/credreg,
# server/credreg и keys; каталог реестра "creds" -- строкой в двух местах;
# правило слова статуса (не проверялся, needs_login с подробностью) -- в
# credreg.row и web.cred_status_word; возраст -- дважды; «годен = активен,
# равные по имени» -- в credreg.pick и tiers._alive; «логинится только
# claude» -- в трёх; «прочесть JSON или умолчание» -- в семи.
# SOLUTION: common держит одно место на каждое: paths.CREDS, помощники
# файла кредов в common/credreg, status_word с таблицей слов параметром,
# age, usable, LOGIN_PROFILE, fsutil.read_json; вызывающие только
# переключаются. Первая половина проверки -- характеристика: выходы те же
# до и после. STATUS: FIXED — see #317
def check_creds_knowledge_317(c):
    import json
    import tempfile
    from mop.common import creds, fsutil, landing, paths, projects, tiers
    from mop.common.domain import CredStatus
    from mop.client import keys
    from mop.server import credlogin, credreg as srv, web
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    # ── характеристика: выходы до и после одни ──
    sts = {"never": None, "active": {"kind": "active"},
           "quota": {"kind": "quota_wait", "resets_at": NOW + 7200},
           "login": {"kind": "needs_login"},
           "login+": {"kind": "needs_login", "detail": "token expired"},
           "odd": {"kind": "weird"}}
    c.expect("#317 list: status words as before",
             {k: credreg.row({"name": "x", "status": v, "added_at": NOW - 90000}, NOW)[4]
              for k, v in sts.items()},
             {"never": "unknown", "active": "active", "quota": "quota wait",
              "login": "needs login", "login+": "needs login: token expired", "odd": "weird"})
    c.expect("#317 page: status words as before",
             {k: web.cred_status_word(v) for k, v in sts.items()},
             {"never": "не проверялся", "active": "активен",
              # Время сброса -- не в слове (#331): его пишет страница.
              "quota": "ждёт квоты",
              "login": "ждёт ручной авторизации",
              "login+": "ждёт ручной авторизации: token expired", "odd": "weird"})
    for added, want in ((NOW - 90000, "1d"), (NOW - 120, "2m"), (0, "0m"), (None, "0m")):
        rec = {"name": "x", "status": None, "added_at": added}
        c.expect(f"#317 age of added_at={added}: list and page agree",
                 (credreg.row(rec, NOW)[7], web.cred_rows([rec], NOW)[0]["age"]), (want, want))
    recs = [credreg.merge_status(credreg.record(n, p, "key", now=NOW), CredStatus(k), NOW)
            for n, p, k in (("b", "glm", "active"), ("a", "glm", "active"),
                            ("c", "glm", "needs_login"), ("d", "claude", "active"))]
    recs.append(credreg.record("e", "glm", "key", now=NOW))
    pairs = {r["name"]: (r["profile"], credreg.status_of(r) or CredStatus("needs_login"))
             for r in recs}
    c.expect("#317 usable: pick and tiers agree -- active, by name",
             (credreg.pick("glm", recs), tiers._alive(pairs, "glm"),
              credreg.pick("zai", recs)), ("a", ["a", "b"], None))
    c.expect("#317 the secrets line (fsutil.write_kv) as before",
             fsutil.write_kv({"Z_AI_KEY": "k1"}), "Z_AI_KEY=k1\n")
    home = os.path.expanduser("~")
    c.expect("#317 the credentials file: one path in every spelling",
             {keys.CREDENTIALS, os.path.join(home, paths.CREDENTIALS),
              credreg.credentials_file(home)},
             {os.path.join(home, ".claude", ".credentials.json")})
    with tempfile.TemporaryDirectory() as d:
        missing, bad, listed = (os.path.join(d, n) for n in ("missing", "bad", "list"))
        open(bad, "w").write("{not json")
        open(listed, "w").write("[1, 2]")
        c.expect("#317 landing.read: missing, broken, not a dict -> no tokens",
                 [landing.read(p) for p in (missing, bad, listed)], [{}, {}, {}])
        lim = os.path.join(d, "limits")
        open(lim, "w").write('{"a": "x"}')
        c.expect("#317 read_limits: missing, broken, bad value -> no limits",
                 [projects.read_limits(p) for p in (missing, bad, lim)], [{}, {}, {}])
        c.expect("#317 operator creds: none in an empty directory",
                 creds.operator(d), None)
        keep_root, keep_cred = srv.ROOT, keys.CREDENTIALS
        try:
            srv.ROOT = d
            os.makedirs(os.path.join(d, "x"))
            open(os.path.join(d, "x", srv.RECORD), "w").write('{"name": "y"}')
            c.expect("#317 server load: missing, foreign name -> None",
                     (srv.load("nope"), srv.load("x")), (None, None))
            c.expect("#317 server credentials: no file -> {}", srv.credentials("x"), {})
            keys.CREDENTIALS = os.path.join(d, "cred.json")
            got = [keys.credentials_fresh()]
            for body in ("{bad", json.dumps({"claudeAiOauth": {"expiresAt": 1000}}),
                         json.dumps({"claudeAiOauth": {"expiresAt": (NOW + 10**6) * 1000}})):
                open(keys.CREDENTIALS, "w").write(body)
                got.append(keys.credentials_fresh())
            c.expect("#317 credentials_fresh: missing, broken, expired, fresh",
                     got, [False, False, False, True])
        finally:
            srv.ROOT, keys.CREDENTIALS = keep_root, keep_cred

    # ── одно место на каждое знание ──
    for mod, name in ((paths, "CREDS"), (credreg, "credentials_file"),
                      (credreg, "access_token"), (credreg, "is_login_file"),
                      (credreg, "status_word"), (credreg, "age"), (credreg, "usable"),
                      (credreg, "LOGIN_PROFILE"), (fsutil, "read_json")):
        c.check(f"#317 one place: {mod.__name__}.{name}", hasattr(mod, name))

    import inspect
    import re as re_

    def text(rel):
        return open(os.path.join(root, rel)).read()
    # Путь -- строковый литерал, который сам путь к файлу кредов (проза в
    # докстрингах и тексте отказа -- не путь).
    path_literal = re_.compile(r"""["'][^"'\s]*(\.credentials\.json|\.claude)["']""")
    for rel in ("mop/client/keys.py", "mop/server/credlogin.py", "mop/server/credreg.py"):
        c.check(f"#317 {rel} spells no credentials path", not path_literal.search(text(rel)),
                path_literal.findall(text(rel)))
    for rel in ("mop/client/keys.py", "mop/server/credreg.py"):
        c.check(f"#317 {rel} knows no credentials schema",
                '"claudeAiOauth"' not in text(rel) and '"expiresAt"' not in text(rel)
                and '"accessToken"' not in text(rel))
    for rel in ("mop/server/credreg.py", "mop/cli/cred/login.py"):
        c.check(f'#317 {rel} spells no "creds" directory', 'local("creds"' not in text(rel))
    for rel in ("mop/server/credreg.py", "mop/client/keys.py"):
        c.check(f'#317 {rel} spells no login profile', 'profile="claude"' not in text(rel)
                and 'credreg.record(name, "claude"' not in text(rel))
    for fn in (srv.load, srv.credentials, credlogin.prepare_home, landing.read, creds.operator,
               keys.credentials_fresh, projects.read_limits):
        src = inspect.getsource(fn)
        c.check(f"#317 {fn.__module__}.{fn.__name__} reads JSON through fsutil.read_json",
                "read_json(" in src and "json.load(" not in src)
    c.check("#317 web and tiers use the shared rules",
            "credrows.age(" in text("mop/server/web.py")
            and "status_word(" in text("mop/server/web.py")
            and "usable(" in text("mop/common/tiers.py")
            and "secrets_line" not in text("mop/server/credreg.py")
            and not hasattr(credreg, "secrets_line"))


# ── #312: адресная раздача одному телу ─────────────────────────────────
# HYPOTHESIS: у раздачи нет адреса: `write` уходил узлу без тел, и агент
# писал во все тела узла. Ответ агента на отказ тела -- строка в written, а
# не error: чтение по одному verdict приняло бы непришедшую аренду за OK.
# SOLUTION: credreg.push(кредит, узел, тела) -- materialize и адресная
# запись (`bodies`); итог -- OK, FAILED с причиной (в том числе тело FAILED
# или NOT LIVE в written) либо NOT REACHED. STATUS: FIXED — see #312
def check_push_312(c):
    import tempfile
    from mop.common import bus
    from mop.server import credreg as srv
    push = getattr(srv, "push", None)
    if push is None:
        c.fail("#312 no credreg.push: no addressed delivery")
        return
    sent = []

    # Запись -- через bus.request_many (#315): ответ -- {узел: ответ | ошибка},
    # ошибка шины на узле возвращается, а не бросается.
    def answer(reply):
        def request_many(verb, nodes, timeout=None, project=None, **fields):
            for node in nodes:
                sent.append((node, verb, project, fields.get("bodies"),
                             [f[0] for f in fields.get("files") or []]))
            return {node: reply for node in nodes}
        return request_many
    with tempfile.TemporaryDirectory() as tmp, patched(srv, ROOT=tmp):
        srv.save(credreg.record("anton", "glm", "key", now=NOW))
        with patched(srv, materialize=lambda name, rec=None: [("a/b", b"x"), ("m", b"anton\n")]):
            for reply, want in (
                    ({"written": ["pu-mop-1:a/b", "pu-mop-1:m"]}, "OK"),
                    ({"written": ["pu-mop-1 FAILED — body 9001 is stopped"]},
                     "FAILED: pu-mop-1 FAILED — body 9001 is stopped"),
                    ({"written": ["pu-mop-1 NOT LIVE"]}, "FAILED: pu-mop-1 NOT LIVE"),
                    ({"error": "puppet pu-mop-1 is not in project x"},
                     "FAILED: puppet pu-mop-1 is not in project x"),
                    (bus.BusError("node agent hyper did not answer in 60s"),
                     "NOT REACHED: node agent hyper did not answer in 60s")):
                sent.clear()
                with patched(bus, request_many=answer(reply)):
                    got = push("anton", "hyper", ["pu-mop-1"])
                c.expect(f"#312 push result for {reply!r}", got, want)
            c.expect("#312 push: one addressed write, admin, to that node and body",
                     sent, [("hyper", "write", bus.ADMIN, ["pu-mop-1"], ["a/b", "m"])])

        def nothing(name, rec=None):
            raise ValueError("credential anton: no key to distribute")
        with patched(srv, materialize=nothing), patched(bus, request_many=answer({"written": []})):
            sent.clear()
            c.expect("#312 push: nothing to carry -> FAILED, no write",
                     (push("anton", "hyper", ["pu-mop-1"]), sent),
                     ("FAILED: credential anton: no key to distribute", []))


def main():
    c = Checks()
    check_write_fanout_315(c)
    check_login_start_registry_295(c)
    check_login_start_profile_330(c)
    check_login_mode_339(c)
    check_foreign_mark_308(c)
    check_creds_knowledge_317(c)
    check_push_312(c)

    # Запись: форма закреплена -- её читают list, дашборд и политика.
    rec = credreg.record("anton", "claude", "login", owner="anton@example.dev", now=NOW)
    c.expect("record: the fixed shape", rec,
             {"name": "anton", "profile": "claude", "kind": "login",
              "owner": "anton@example.dev", "added_at": NOW, "status": None})
    for bad in ("", "a/b", "../x", "with space", ".hidden"):
        try:
            credreg.record(bad, "claude", "login")
            c.fail(f"record: name {bad!r} must refuse -- it becomes a directory")
        except ValueError:
            pass
    try:
        credreg.record("ok", "claude", "session")
        c.fail("record: an unknown kind must refuse")
    except ValueError:
        pass

    # Проба вливается в запись вместе с временем пробы; kind/resets/percent/
    # detail -- ровно поля CredStatus, чтобы страница и политика читали одно.
    probed = credreg.merge_status(rec, CredStatus("quota_wait", resets_at=NOW + 3600,
                                                  percent=100, detail="5h window exhausted"),
                                  now=NOW + 10)
    c.expect("merge_status: the probe lands in the record", probed["status"],
             {"kind": "quota_wait", "resets_at": NOW + 3600, "percent": 100,
              "detail": "5h window exhausted", "probed_at": NOW + 10})
    c.check("merge_status: the input record is untouched", rec["status"] is None)

    # Строки списка: слово статуса, сброс по-человечески, процент, возраст.
    key = credreg.merge_status(credreg.record("z1", "glm", "key", now=NOW - 3 * 86400),
                               CredStatus("active", percent=53, detail="5h 1%, weekly 53%"),
                               now=NOW - 60)
    dead = credreg.merge_status(credreg.record("old", "glm", "key", now=NOW - 40 * 86400),
                                CredStatus("needs_login", detail="HTTP 401"), now=NOW)
    rows = credreg.rows([rec, probed, key, dead], now=NOW + 10)
    # Колонка holders (#284): кто из папетов держит аренду кредита.
    c.expect("rows: header first", rows[0],
             ("name", "profile", "kind", "owner", "status", "resets", "used", "age", "holders"))
    c.expect("rows: unprobed -> unknown", rows[1],
             ("anton", "claude", "login", "anton@example.dev", "unknown", "-", "-", "0m", "-"))
    c.expect("rows: quota wait with a human reset", rows[2],
             ("anton", "claude", "login", "anton@example.dev", "quota wait", "in 59m", "100%", "0m", "-"))
    c.expect("rows: active key", rows[3],
             ("z1", "glm", "key", "-", "active", "-", "53%", "3d", "-"))
    c.expect("rows: dead key names the reason", rows[4],
             ("old", "glm", "key", "-", "needs login: HTTP 401", "-", "-", "40d", "-"))
    c.expect("rows: holders named, sorted", credreg.rows(
        [key], now=NOW + 10, holders={"z1": ["pu-b-2", "pu-a-1"]})[1][8], "pu-a-1,pu-b-2")
    c.expect("rows: nothing -> header only", credreg.rows([], now=NOW), [rows[0]])

    # Сброс в прошлом -- окно уже открылось: показываем «now», а не минус.
    stale = credreg.merge_status(rec, CredStatus("quota_wait", resets_at=NOW - 5), now=NOW)
    c.expect("rows: a reset in the past reads now", credreg.rows([stale], now=NOW)[1][5], "now")

    # Продление токена (#283): claude обновляет токен сам при запросе, и
    # сервер зовёт `claude -p` в доме кредита, пока срок не вышел. Порог --
    # два часа; без срока (ключ, setup-token) продлевать нечего.
    for what, exp, want in [
        ("far away -> no", NOW + 8 * 3600, False),
        ("within two hours -> yes", NOW + 3600, True),
        ("already expired -> yes (refresh token may still work)", NOW - 10, True),
        ("no expiry -> no", None, False),
    ]:
        c.expect(f"needs_keepalive: {what}", credreg.needs_keepalive(exp, NOW), want)

    # Срок из файла кредов claude: expiresAt в миллисекундах.
    c.expect("expires_at: ms -> s", credreg.expires_at(
        {"claudeAiOauth": {"expiresAt": 1790425418306}}), 1790425418)
    c.expect("expires_at: no oauth -> None", credreg.expires_at({}), None)
    c.expect("expires_at: garbage -> None", credreg.expires_at("nope"), None)

    # ── #284: аренда кредита папетом ──────────────────────────────────────
    # HYPOTHESIS: кредит папету не назначается: логин -- копия файла
    # оператора по всем телам с refresh-токеном, который ротирует
    # провайдер; провал хода и расход приписать некому.
    # SOLUTION: JobMeta.cred (аренда), pick -- первый активный кредит
    # профиля по имени; в тело уезжает файл без refreshToken и метка
    # `.local/state/mop/cred`; хук кладёт метку в запись хода, turn_status
    # приписывает провал кредиту. STATUS: FIXED — see #284
    a1 = credreg.merge_status(credreg.record("b-key", "glm", "key", now=NOW),
                              CredStatus("active"), now=NOW)
    a2 = credreg.merge_status(credreg.record("a-key", "glm", "key", now=NOW),
                              CredStatus("active"), now=NOW)
    waiting = credreg.merge_status(credreg.record("0-key", "glm", "key", now=NOW),
                                   CredStatus("quota_wait", resets_at=NOW + 60), now=NOW)
    unprobed = credreg.record("login1", "claude", "login", now=NOW)
    c.expect("pick: first active of the profile by name", credreg.pick("glm", [a1, waiting, a2]), "a-key")
    c.expect("pick: another profile -> None", credreg.pick("claude", [a1, a2]), None)
    c.expect("pick: unprobed does not count", credreg.pick("claude", [unprobed]), None)
    c.expect("pick: nothing active -> None", credreg.pick("glm", [waiting]), None)

    full = {"claudeAiOauth": {"accessToken": "sk-ant-oat-x", "refreshToken": "sk-ant-ort-y",
                              "expiresAt": 1790425418306, "scopes": ["user:inference"],
                              "subscriptionType": "max"}}
    lean = credreg.without_refresh(full)
    c.expect("without_refresh: no refreshToken, the rest intact", lean,
             {"claudeAiOauth": {"accessToken": "sk-ant-oat-x", "expiresAt": 1790425418306,
                                "scopes": ["user:inference"], "subscriptionType": "max"}})
    c.check("without_refresh: the input is untouched", "refreshToken" in full["claudeAiOauth"])
    c.expect("secrets line (fsutil.write_kv, #317)", fsutil.write_kv({"Z_AI_KEY": "abc"}),
             "Z_AI_KEY=abc\n")

    c.expect("reset_time_of: the rate_limit text (UTC assumed)", credreg.reset_time_of(
        "API Error: Request rejected (429) · Usage limit reached for 5 hour. "
        "Your limit will reset at 2026-09-24 15:00:00"), 1790262000)
    c.expect("reset_time_of: no time -> None", credreg.reset_time_of("Usage limit reached"), None)
    c.expect("reset_time_of: None -> None", credreg.reset_time_of(None), None)

    def turn(error, detail=None, at=NOW):
        return {"event": "StopFailure", "at": at, "error": error, "detail": detail, "cred": "anton"}
    got = credreg.turn_status(turn("authentication_failed"), changed_at=NOW - 100)
    c.expect("turn_status: login expired, home unchanged -> needs_login",
             (got.kind, got.detail), ("needs_login", "login expired"))
    c.expect("turn_status: login expired but the home changed after the turn -> None (re-distribute)",
             credreg.turn_status(turn("authentication_failed"), changed_at=NOW + 5), None)
    got = credreg.turn_status(turn("rate_limit", "Usage limit reached for 5 hour. "
                                   "Your limit will reset at 2026-09-24 15:00:00"), changed_at=None)
    c.expect("turn_status: rate_limit -> quota_wait until the reset",
             (got.kind, got.resets_at), ("quota_wait", 1790262000))
    got = credreg.turn_status(turn("billing_error", "no credits"), changed_at=None)
    c.expect("turn_status: billing_error -> quota_wait without a time",
             (got.kind, got.resets_at), ("quota_wait", None))
    c.expect("turn_status: a good turn -> None",
             credreg.turn_status({"event": "Stop", "at": NOW, "error": None, "detail": None,
                                  "cred": "anton"}, changed_at=None), None)
    c.expect("turn_status: another error -> None",
             credreg.turn_status(turn("server_error", "boom"), changed_at=None), None)

    return c.report("credreg")


if __name__ == "__main__":
    sys.exit(main())
