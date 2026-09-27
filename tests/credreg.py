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
from _lib import Checks  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.common import credreg  # noqa: E402
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


def main():
    c = Checks()
    check_login_start_registry_295(c)
    check_foreign_mark_308(c)

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
    c.expect("secrets_line", credreg.secrets_line("Z_AI_KEY", "abc"), "Z_AI_KEY=abc\n")

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
