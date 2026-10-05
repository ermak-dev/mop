#!/usr/bin/env python3
"""Исполнитель веб-операций без пула: python3 tests/opworker.py

Воркер #499 -- тонкий исполнитель над общим workflow #458: run(verb,
target, params, login) -> вердикт, тем же транспортом, что у CLI. Ни
очереди, ни журнала, ни замка цели (#497: вторая копия «жизни операции»
разошлась с реальностью) -- приём, дедупликация, статусы и SSE-кадры
живут слоем адаптера (tests/adminapi.py, tests/web_intents.py), правда
об эффекте -- аудит пула и /api/journal. Здесь -- контракт исполнения:
safety ДО эффекта, наблюдение рестарта (#464), workspace из ЯВНОГО
удалённого источника (#470), общий жизненный цикл (#471), общая смена
модели (#468) и карта исходов #476.

Это не фреймворк: транспорт подменяется, эффекты считаются вызовами.
"""
import io
import json
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus, ops, puppets  # noqa: E402
from mop.server import adminapi, opworker  # noqa: E402


# ── каркас проверки ──────────────────────────────────────────────────────
class Recorder:
    """Транспорт-подмена: глаголы #458 считаются, любой может упасть.

    alloc -- read-глагол наблюдения (#464): отдаёт неизменную сводку задачи;
    счётчик рестартов стоит -- наблюдение честно кончается unknown, а сам
    эффект считается по рестарт-RPC."""

    ALLOC = {"ID": "a1", "ClientStatus": "running",
             "task": {"state": "running", "restarts": 7, "exit": None,
                      "next_s": None, "failed": False}}

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at or {}
        self.answers = {"add": {"ok": True, "name": "pu-mop-9"},
                        "alloc": {"alloc": dict(self.ALLOC)}}

    def __call__(self, verb, **fields):
        self.calls.append((verb, fields))
        why = self.fail_at.get(verb) or self.fail_at.get("*")
        if why:
            raise ops.bus.Refused(why) if "refuse" in str(why) else RuntimeError(why)
        got = self.answers.get(verb)
        if got is None:
            return {}
        return dict(got) if isinstance(got, dict) else got

    def effect_rpcs(self):
        """Вызовы-эффекты без read-глаголов наблюдения (#464)."""
        return [call for call in self.calls
                if not (isinstance(call, tuple) and len(call) == 2
                        and call[0] in ("alloc",))]


def one(now=(lambda: 1_000.0), registry=None, workspace=None):
    """Воркер без каталога: исполнение #499 ничего не пишет на диск.

    Наблюдение (#464): нулевой срок и переданный сон -- иначе каждая
    матрица опрашивала бы observe_wait по-настоящему."""
    return opworker.Worker(now=now, observe_wait=0, sleep=lambda s: None,
                           registry=registry, workspace=workspace)


def main():
    c = Checks()
    allow = lambda name, project, **kw: (True, None)

    # ── каркас #499: run -> вердикт библиотечного эффекта ───────────────
    # HYPOTHESIS: воркер #476 держал вторую копию «жизни операции» (журнал
    # ops.jsonl, payload-файлы, _recover, пер-целевая очередь), адаптер --
    # третью (очередь задач, _active-замок с 409, _results с TTL). Копии
    # разошлись (#497: вечный accepted, 409 часами), а общий workflow #458
    # уже один. SOLUTION: воркер -- синхронный run(verb, target, params,
    # login) -> вердикт ops.*, тем же транспортом, что CLI; приём и
    # статусы -- слой адаптера (проверяется tests/adminapi.py и
    # tests/web_intents.py). Гарантии удалённых механизмов живут новыми
    # проверками: дедуп -- в памяти адаптера, сериализация по цели --
    # семафором адаптера (waiting), рестарт службы -- honest unknown со
    # ссылкой на журнал, крах эффекта -- failed-вердикт потока адаптера.
    transport = Recorder()
    out1 = one().run("restart", "pu-mop-1", {}, login="anton",
                     transport=transport, safety=allow)
    c.check("#476 a run reaches a terminal status through the library effect",
            out1.get("status") in opworker.TERMINAL, out1)

    # ── наблюдаемый рестарт (#464): сигнал, тишина, падение ─────────────
    # Счётчик сдвинулся и задача вернулась running -- succeeded с фактом.
    class Ticking(Recorder):
        """alloc растит счётчик рестартов после первого эффекта."""

        def __init__(self):
            super().__init__()
            self.fired = False

        def __call__(self, verb, **fields):
            if verb == "restart":
                self.fired = True
            got = super().__call__(verb, **fields)
            if verb == "alloc" and self.fired:
                got = {"alloc": dict(self.ALLOC, task=dict(self.ALLOC["task"],
                                                           restarts=8))}
            return got

    tg = Ticking()
    got = one().run("restart", "pu-mop-1", {}, login="anton",
                    transport=tg, safety=allow)
    c.check("#464 an observed restart whose signal arrives succeeds",
            got.get("status") == "succeeded" and got.get("restarts") == 8, got)
    # Сигнала нет (счётчик стоит) -- unknown, не succeeded: прежний running
    # не успех (#464).
    th = Recorder()
    got = one().run("restart", "pu-mop-1", {}, login="anton",
                    transport=th, safety=allow)
    c.check("#464 a standing old running is unknown, not succeeded",
            got.get("status") == "unknown"
            and "no restart seen" in (got.get("error") or ""), got)
    # Задача вернулась падшей -- partial с причиной.

    class Falling(Recorder):
        def __call__(self, verb, **fields):
            got = super().__call__(verb, **fields)
            if verb == "alloc" and self.fired:
                return {"alloc": dict(self.ALLOC,
                                      task=dict(self.ALLOC["task"], restarts=8,
                                                failed=True, state="dead"))}
            if verb == "restart":
                self.fired = True
            return got

    ti = Falling()
    got = one().run("restart", "pu-mop-1", {}, login="anton",
                    transport=ti, safety=allow)
    c.check("#464 a task dead after the restart is partial",
            got.get("status") == "partial", got)

    # ── safety ДО эффекта ───────────────────────────────────────────────
    t2 = Recorder()
    out = one().run("restart", "pu-mop-1", {}, login="olga",
                    transport=t2, safety=lambda name, project, **kw: (False, "led by anton"))
    c.check("#476 a refused safety never fires the effect",
            not t2.calls, t2.calls)
    c.check("#476 the refusal is the outcome",
            (out.get("status"), out.get("error")), ("refused", "led by anton"))

    # ── крах до RPC: исключение летит в поток адаптера ──────────────────
    # Воркер #499 не строит свою копию «failed»: эффект, упавший не
    # ValueError, поднимается из run, и поток адаптера (#499 adminapi)
    # пишет честный failed с причиной -- эта связка проверена в
    # tests/adminapi.py («an effect that crashes reads failed»).
    t3 = Recorder(fail_at={"*": "bus down"})
    try:
        one().run("restart", "pu-mop-1", {}, login="anton",
                  transport=t3, safety=allow)
        c.check("#476 a crash before the RPC propagates to the adapter thread",
                False, "run returned")
    except RuntimeError as e:
        c.check("#476 a crash before the RPC propagates to the adapter thread",
                str(e) == "bus down", e)

    # ── роль: не admin -- отказ ДО эффекта ──────────────────────────────
    t4 = Recorder()
    out = one().run("restart", "pu-mop-1", {}, login="olga", role="user",
                    transport=t4)
    c.check("#476 a demoted actor is refused before any effect",
            (out.get("status"), t4.calls) == ("refused", []), out)

    # ── аудит: ops.record на каждую операцию, без message/workspace ─────
    # HYPOTHESIS: payload операции (workspace, message) в вердикте и аудите
    # не живёт: это чужие секреты. SOLUTION: run возвращает вердикт с
    # фактами и без params; аудит-запись -- ops.record {event, verb, name,
    # project, principal, sender} без payload.
    t9 = Recorder()
    out = one().run("restart", "pu-mop-1", {"note": "do it"}, login="anton",
                    transport=t9, safety=allow)
    c.check("#476 the verdict carries no executor payload",
            not any(k in json.dumps(out) for k in ("note", "workspace",
                                                   "message", "params")), out)
    rec = ops.record("restart", "pu-mop-1", None, "anton")
    c.check("#476 the audit record shape stays ops.record",
            all(k in rec for k in ("event", "verb", "name", "project",
                                   "principal", "sender"))
            and not any(k in rec for k in ("note", "message", "params")), rec)

    # ── неизвестный глагол -- громкий отказ, не тишина ──────────────────
    # Регистрация и исполнение -- одна точка (register), но чужой глагол
    # обязан падать громко: Runtime поднимается из run, поток адаптера
    # (#499 adminapi) пишет честный failed. Молчаливый unknown тут -- ложь.
    try:
        one().run("reboot", "pu-mop-1", {}, login="anton",
                  transport=Recorder(), safety=allow)
        c.check("#471 a verb with no library operation is loud, not silent",
                False, "run returned")
    except RuntimeError as e:
        c.check("#471 a verb with no library operation is loud, not silent",
                str(e) == "no library operation for reboot", str(e))

    # ── add/update поверх общих операций (#470) ─────────────────────────
    # HYPOTHESIS: у веба нет рабочей копии: workspace обязан приезжать из
    # ЯВНОГО удалённого источника (fetch origin -- как его читает
    # manifest), никаких cwd-чтений и локальных файлов сервера; origin add
    # обязан быть в реестре -- иначе отказ до эффекта. fresh у update --
    # явный bool, молчаливого режима нет.
    # SOLUTION: run зовёт ops.add/ops.update (#458); источник workspace --
    # remote_workspace(origin) или подмена воркера; реестр -- callable;
    # цель add -- сам origin, присвоенное имя -- факт вердикта.
    PROV = {"source": "origin", "commit": "c1", "dirty": False,
            "tasks": 1, "present": True}

    class AddT(Recorder):
        """Ответы сервиса для add: имя, метка, подъём, свой итог прогона."""

        def __init__(self):
            super().__init__()
            self.answers = {"add": {"ok": True, "name": "pu-mop-9",
                                    "bootstrap_marker": "m1"},
                            "alloc": {"alloc": dict(self.ALLOC)},
                            "bootstrap_result": {"result": {"sent": "m1",
                                                            "played": True,
                                                            "ok": True}}}

    def web_worker(registry=("git@h:g/x.git",), workspace=None):
        # Выкачки источника считаются: w._fetched -- список origin.
        seen = []
        core = workspace or (lambda o: (f"boot for {o}", dict(PROV)))
        w = one(registry=(lambda: set(registry)) if registry is not None else None,
                workspace=lambda o: (seen.append(o), core(o))[1])
        w._fetched = seen
        return w

    # Успешный add: workspace уходит из явного источника, одна выкачка.
    w = web_worker(workspace=lambda o: (f"boot for {o}", dict(PROV)))
    t = AddT()
    got = w.run("add", None, {"origin": "git@h:g/x.git", "branch": "dev"},
                login="anton", transport=t, safety=allow)
    c.check("#470 add through the worker succeeds with #476 verdicts",
            got.get("status") == "succeeded", got)
    c.check("#470 add rides the explicit remote workspace",
            t.calls and t.calls[0] == ("add", {"origin": "git@h:g/x.git",
                                               "timeout": 30,
                                               "workspace": "boot for git@h:g/x.git",
                                               "bootstrap_sent": PROV,
                                               "branch": "dev"}), t.calls[:1])
    c.check("#470 the workspace is fetched once, from the given origin",
            w._fetched == ["git@h:g/x.git"], w._fetched)
    c.check("#470 the assigned puppet name lands in the verdict",
            got.get("name") == "pu-mop-9", got)

    # Незарегистрированный origin -- отказ ДО эффекта (кнопки нет -- и
    # обходной дороги нет: воркер сам gates).
    w = web_worker(registry=("git@h:g/other.git",))
    t = AddT()
    got = w.run("add", None, {"origin": "git@h:g/x.git"}, login="anton",
                transport=t, safety=allow)
    c.check("#470 an unregistered origin is refused before any effect",
            got.get("status") == "refused" and "registry" in (got.get("error") or "")
            and not t.calls, (got, t.calls))
    # Реестр не подключён -- отказ (fail closed), не тихий пропуск.
    w = web_worker(registry=None)
    got = w.run("add", None, {"origin": "git@h:g/x.git"}, login="anton",
                transport=AddT(), safety=allow)
    c.check("#470 add without a wired registry is refused, not waved through",
            got.get("status") == "refused", got)

    # Кривой add -- валидационный отказ приёма, не failed и не эффект.
    t = AddT()
    got = web_worker().run("add", None, {"origin": ""}, login="anton",
                           transport=t, safety=allow)
    c.check("#470 a malformed add is refused before the effect",
            got.get("status") == "refused" and not t.calls, (got, t.calls))

    # Payload не входит в вердикт: текст bootstrap'а живёт только в вызове
    # операции, в ответе run и аудит-записи его нет.
    w = web_worker(workspace=lambda o: ("BOOT-SECRET-470", dict(PROV)))
    got = w.run("add", None, {"origin": "git@h:g/x.git"}, login="anton",
                transport=AddT(), safety=allow)
    c.check("#470 the bootstrap text never enters the verdict",
            "BOOT-SECRET-470" not in json.dumps(got), got)
    rec = ops.record("add", None, None, "anton")
    c.check("#470 the audit record carries no workspace",
            "BOOT-SECRET-470" not in json.dumps(rec), rec)

    # ── update через воркер: fresh явный, источник -- origin папета ─────
    # keep (fresh false): origin прежний (из спеки), workspace -- из него
    # же; спека читается ОДИН раз и едет в операцию (не спрашивать вторично).
    w = web_worker()
    t = Recorder()
    t.answers = {"spec": {"meta": {"origin": "git@h:g/x.git"}},
                 "update": {"replaced": None}}
    got = w.run("update", "pu-mop-1", {"fresh": False}, login="anton",
                transport=t, safety=allow)
    c.check("#470 a keep update reads as partial (accepted, no run)",
            got.get("status") == "partial", got)
    c.check("#470 a keep update keeps the origin and resumes",
            len(t.calls) > 1 and t.calls[1] == ("update",
                {"name": "pu-mop-1", "origin": "git@h:g/x.git", "cont": True,
                 "new_origin": None, "workspace": "boot for git@h:g/x.git",
                 "bootstrap_sent": PROV, "owner": "anton", "force": False,
                 "branch": None}), t.calls[1:])
    c.check("#470 a keep update fetches the puppet's own origin",
            w._fetched == ["git@h:g/x.git"], w._fetched)
    c.check("#470 the spec is read once and rides into the operation",
            [verb for verb, _ in t.calls].count("spec") == 1, t.calls)
    # remove (fresh true + origin): workspace -- из НОВОГО origin.
    w = web_worker()
    t = Recorder()
    t.answers = {"spec": {"meta": {"origin": "git@h:g/x.git"}},
                 "update": {"bootstrap_marker": "m3"},
                 "bootstrap_result": {"result": {"sent": "m3", "played": True,
                                                 "ok": True}}}
    got = w.run("update", "pu-mop-1", {"fresh": True, "origin": "git@h:g/new.git"},
                login="anton", transport=t, safety=allow)
    c.check("#470 a remove update succeeds through the worker",
            got.get("status") == "succeeded", got)
    fields = t.calls[1][1]
    c.check("#470 a remove update rides the NEW origin's workspace",
            fields.get("cont") is False
            and fields.get("new_origin") == "git@h:g/new.git"
            and fields.get("workspace") == "boot for git@h:g/new.git", fields)
    c.check("#470 a remove update fetches only the new origin",
            w._fetched == ["git@h:g/new.git"], w._fetched)
    # fresh отсутствует -- валидационный отказ, молчаливого режима нет.
    t = Recorder()
    got = web_worker().run("update", "pu-mop-1", {}, login="anton",
                           transport=t, safety=allow)
    c.check("#470 update without an explicit fresh is refused",
            got.get("status") == "refused" and not t.calls, (got, t.calls))

    # ── safety-матрица на update (#460/#470): отказ ДО эффекта ──────────
    NOW = 1_000_000.0
    CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}

    def bound_safety(facts):
        # disruptive (#471) воркер ставит сам; заглушке он не нужен --
        # общий ops.safety его принимает, факт disruptive у фактов свой.
        return lambda name, project, owner, disruptive=False: ops.safety(
            name, project, owner=owner, facts=facts, now=NOW,
            disruptive=disruptive)

    def update_with(facts):
        w = web_worker()
        t = Recorder()
        t.answers = {"spec": {"meta": {"origin": "git@h:g/x.git"}},
                     "update": {"replaced": None}}
        return w, t, bound_safety(facts)

    for what, facts in (
            ("a dirty clone", {"clone": dict(CLEAN, dirty=2, owner=None)}),
            ("a foreign live lease",
             {"clone": dict(CLEAN, owner={"user": "olga", "at": NOW - 5})}),
            ("unknown facts", None),
            ("an agent error", {"error": "silent"})):
        w, t, safety = update_with(facts)
        got = w.run("update", "pu-mop-1", {"fresh": False}, login="anton",
                    transport=t, safety=safety)
        c.check(f"#470 update refuses {what} before the effect",
                got.get("status") == "refused" and not t.calls, (got, t.calls))
    ok, why = bound_safety({"clone": dict(CLEAN, owner={"user": "olga",
                                                        "at": NOW - 5})})(
        "pu-mop-1", None, "anton")
    c.check("#470 the foreign-lease refusal names the holder",
            not ok and "olga" in (why or ""), why)
    # Своя живая аренда на чистом клоне -- проход, эффект исполняется.
    w, t, safety = update_with({"clone": dict(CLEAN, owner={"user": "anton",
                                                            "at": NOW - 5})})
    got = w.run("update", "pu-mop-1", {"fresh": False}, login="anton",
                transport=t, safety=safety)
    c.check("#470 update with the owner's own clean lease runs",
            got.get("status") == "partial" and t.calls, (got, t.calls))

    # Выкачка не удалась -- исключение летит в поток адаптера (там станет
    # честным failed с причиной; связка -- tests/adminapi.py).
    def broken_source(origin):
        raise RuntimeError("cannot read x: access denied")

    w = web_worker(workspace=broken_source)
    t = AddT()
    try:
        w.run("add", None, {"origin": "git@h:g/x.git"}, login="anton",
              transport=t, safety=allow)
        c.check("#470 a failed workspace fetch propagates to the adapter thread",
                False, "run returned")
    except RuntimeError as e:
        c.check("#470 a failed workspace fetch propagates to the adapter thread",
                "access denied" in str(e) and not t.calls, (str(e), t.calls))

    # ── remote_workspace: происхождение тем же видом, что у CLI (#334) ──
    # Только origin-источник: fetch_tree и cwd дороги не касаются вовсе.
    rs = getattr(opworker, "remote_workspace", None)
    if c.check("#470 remote_workspace exists", rs is not None):
        from mop.common import manifest
        WS_TEXT = ("- name: ws\n  vars:\n    A: 1\n  tasks:\n"
                   "    - name: t1\n      ansible.builtin.debug: {msg: hi}\n"
                   "    - name: t2\n      ansible.builtin.debug: {msg: ho}\n")

        def boom(*a, **k):
            raise AssertionError("fetch_tree must not be touched by the web")

        with patched(manifest, fetch_tree=boom):
            text, prov = rs("git@h:g/x.git",
                            fetch=lambda o: {"bootstrap_text": WS_TEXT,
                                             "commit": "abc123"})
        c.expect("#470 the remote provenance carries the CLI shape",
                 prov, {"source": "origin", "commit": "abc123", "dirty": False,
                        "tasks": 2, "present": True})
        c.check("#470 the remote text is the file's text", text == WS_TEXT, text)
        text, prov = rs("o", fetch=lambda o: {"bootstrap_text": "",
                                              "commit": "abc"})
        c.expect("#470 no file means present false and zero tasks",
                 prov, {"source": "origin", "commit": "abc", "dirty": False,
                        "tasks": 0, "present": False})
        text, prov = rs("o", fetch=lambda o: {"bootstrap_text": "vars: {}\n",
                                              "commit": None})
        c.check("#470 a malformed file reads as tasks unknown",
                prov.get("tasks") is None and prov.get("present") is True, prov)

    # ── жизненный цикл через воркер (#471): recycle/delete/wipe ─────────
    # HYPOTHESIS: у веб-клика те же общие реализации puppets.recycle/delete/
    # wipe (#458), что и у CLI; вторая реализация разошлась бы с первой в
    # тот же день. SOLUTION: обёртки зовут готовые функции и кладут ответ
    # в структурный вердикт outcome() #458; safety -- disruptive перед
    # эффектом, run -- карта исходов в статусы #476.
    # RESULT: 12 RED-checks (no library operation) -> wrappers in
    # mop/server/opworker.py; матрица -- тем же ходом.
    # STATUS: FIXED — see #471; с #499 вход в обёртки -- run воркера.

    class Lifecycle:
        """Подмена puppets.recycle/delete/wipe: вызовы и их ответы."""

        def __init__(self, result=None, error=None):
            self.calls = []
            self.result = result or {}
            self.error = error

        def recycle(self, name, **kw):
            self.calls.append(("recycle", name, kw))
            if self.error:
                raise RuntimeError(self.error)
            return dict(self.result, node="n1")

        def delete(self, name, **kw):
            self.calls.append(("delete", name, kw))
            if self.error:
                raise RuntimeError(self.error)
            return dict(self.result, node="n1", body="destroyed")

        def wipe(self, node, name, **kw):
            self.calls.append(("wipe", node, name, kw))
            if self.error:
                raise RuntimeError(self.error)
            return {"reset": True, "target": "target"}

        def node_of(self, name, running=True):
            self.calls.append(("node_of", name, {"running": running}))
            return "n9"

    def run_verb(verb, lc, safety=None):
        """Воркер, чей эффект зовёт общий слой подменой; вход -- run."""
        with patched(puppets, recycle=lc.recycle, delete=lc.delete,
                     wipe=lc.wipe, node_of=lc.node_of):
            return one().run(verb, "pu-mop-1", {}, login="anton",
                             transport=Recorder(),
                             safety=safety or allow)

    # Успешный путь каждого глагола через воркер: эффект -- общий, вердикт --
    # карта исходов #476. registered -> succeeded.
    lc = Lifecycle()
    got = run_verb("recycle", lc)
    c.check("#471 recycle through the worker succeeds",
            got.get("status") == "succeeded", got)
    c.check("#471 recycle calls the common implementation once",
            [(x[0], x[1]) for x in lc.calls] == [("recycle", "pu-mop-1")], lc.calls)
    c.check("#471 recycle carries no force from the web",
            lc.calls and lc.calls[0][2].get("force") is False, lc.calls)

    lc = Lifecycle()
    got = run_verb("delete", lc)
    c.check("#471 delete through the worker succeeds",
            got.get("status") == "succeeded", got)
    c.check("#471 delete calls the common implementation",
            [(x[0], x[1]) for x in lc.calls] == [("delete", "pu-mop-1")], lc.calls)

    lc = Lifecycle()
    got = run_verb("wipe", lc)
    c.check("#471 wipe through the worker succeeds",
            got.get("status") == "succeeded", got)
    c.check("#471 wipe resolves the node stopped and wipes there",
            [(x[0], x[1]) for x in lc.calls] == [("node_of", "pu-mop-1"),
                                                 ("wipe", "n9")]
            and lc.calls[1][2] == "pu-mop-1"
            and lc.calls[1][3].get("force") is False,
            lc.calls)

    # Partial (#476): recycle поднял папета, наблюдение не сошлось --
    # replaced=False общей операции (спека не изменилась, клон не поднимется)
    # читается как partial, а не тишина.

    class AcceptedLifecycle(Lifecycle):
        def recycle(self, name, **kw):
            super().recycle(name, **kw)
            return {"replaced": False, "node": "n1"}

    lc = AcceptedLifecycle()
    got = run_verb("recycle", lc)
    c.check("#471 recycle without its confirmation reads partial",
            got.get("status") == "partial", got)

    # Отказ эффекта без внятного «нет» -- исключение в поток адаптера
    # (там станет failed с причиной; связка -- tests/adminapi.py).
    lc = Lifecycle(error="pu-mop-1 has no allocation — nothing to recycle")
    try:
        run_verb("recycle", lc)
        c.check("#471 a failing effect propagates to the adapter thread",
                False, "run returned")
    except RuntimeError as e:
        c.check("#471 a failing effect propagates to the adapter thread",
                "no allocation" in str(e), str(e))

    # Живое отказ агента у wipe (#40): tmux-сессия жива -- refused, как
    # в CLI (puppets.wipe отвечает RuntimeError'ом с полем error агента).
    lc = Lifecycle(error="pu-mop-1: tmux session is alive — stop the job first")
    got = run_verb("wipe", lc)
    c.check("#471 wipe on a live session is refused, as in the CLI",
            (got.get("status"), "alive" in (got.get("error") or ""))
            == ("refused", True), got)

    # ── safety-матрица (#460) на каждом из трёх, disruptive ─────────────
    # Dirty-клон, чужая живая аренда, busy-сессия и неизвестные факты --
    # отказ ДО эффекта на каждом глаголе; эффект не зовётся вовсе. Предикат
    # здесь -- сам ops.safety, но disruptive воркеру задаёт СЛОВАРЬ
    # РЕГИСТРАЦИИ (DISRUPTIVE): какой kw воркер передал, проверяет
    # ProbingSafety ниже.

    class ProbingSafety:
        """ops.safety как её зовёт воркер: kw пишутся, disruptive читается."""

        def __init__(self, facts, busy):
            self.facts, self.busy = facts, busy
            self.seen = {}

        def __call__(self, name, project, **kw):
            self.seen = kw
            return ops.safety(name, project, owner=kw.get("owner"),
                              facts=self.facts, busy=self.busy,
                              disruptive=self.disruptive(kw), now=NOW)

        @staticmethod
        def disruptive(kw):
            return bool(kw.get("disruptive"))

    CL = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
    DIRTY_CL = {"cur": "epic/x", "def": "master", "dirty": 3, "ahead": 1}
    MATRIX = (
        ("a dirty clone", {"clone": dict(DIRTY_CL, owner=None)}),
        ("a foreign live lease",
         {"clone": dict(CL, owner={"user": "olga", "at": NOW - 5})}),
        ("a busy session", {"clone": dict(CL, owner={"user": "anton", "at": NOW - 5})}),
        ("unknown facts", None),
        ("an agent error", {"error": "agent silent"}),
    )
    for verb in ("recycle", "delete", "wipe"):
        for what, facts in MATRIX:
            lc = Lifecycle()
            busy = what == "a busy session"
            probe = ProbingSafety(facts, busy)
            got = run_verb(verb, lc, safety=probe)
            refused = got.get("status") == "refused"
            c.check(f"#471 {verb} refuses on {what}",
                    refused and bool(got.get("error")), got)
            c.check(f"#471 {verb} on {what} never fires the effect",
                    not lc.calls, lc.calls)

        # Регистрация обязана пометить глагол разрушительным: busy-сессия
        # (своя аренда, чистый клон) доходит до safety и отпирается им.
        lc = Lifecycle()
        probe = ProbingSafety({"clone": dict(CL, owner={"user": "anton",
                                                       "at": NOW - 5})}, True)
        run_verb(verb, lc, safety=probe)
        c.check(f"#471 {verb} is registered disruptive",
                probe.seen.get("disruptive") is True, probe.seen)

    # Своя аренда на чистом клоне, сессия не занята -- проход (не матрица
    # отказа): воркер зовёт safety disruptive=True сам.
    lc = Lifecycle()

    def ok_safety(name, project, **kw):
        return ops.safety(name, project, owner=kw.get("owner"),
                          facts={"clone": dict(CL, owner={"user": "anton",
                                                          "at": NOW - 5})},
                          busy=False, disruptive=True, now=NOW)

    got = run_verb("recycle", lc, safety=ok_safety)
    c.check("#471 own lease on a clean idle clone passes to the effect",
            got.get("status") == "succeeded" and len(lc.calls) == 1, got)

    # ── вердикт без payload (#471/#476: без message/workspace) ──────────
    lc = Lifecycle()
    got = run_verb("recycle", lc)
    c.check("#471 the operation verdict carries no executor payload",
            not any(k in json.dumps(got) for k in ("workspace", "message",
                                                   "request", "params")), got)
    rec = ops.record("recycle", "pu-mop-1", None, "anton")
    c.check("#471 every lifecycle operation has an audit record shape",
            "principal" in rec and "sender" in rec
            and "workspace" not in json.dumps(rec), rec)

    # ── смена модели через воркер (#468) ─────────────────────────────────
    # HYPOTHESIS: веб-клик и MCP напечатали бы каждый свою смену: белый
    # список ID, подтверждение телеметрией и no-op разошлись бы так же,
    # как add/workspace. До регистрации адаптер честно отвечает 404.
    # SOLUTION: глагол model регистрируется в opworker.register
    # (MODEL_PARAMS), валидатор model_request отбрасывает кривой id ДО
    # эффекта; эффект -- общий ops.switch_model (#468): воркер резолвит
    # узел цели и кормит ему node-образный транспорт; safety -- не
    # disruptive (смена не убивает сессию, TUI-ход), отказы агента --
    # refused. Вход -- run воркера (#499).

    class ModelT:
        """Node-транспорт: read-глагол states -- тот же конверт, что у
        ростера. Вызовы считаются (node, verb, fields)."""

        def __init__(self, states):
            self.states = states
            self.calls = []

        def __call__(self, node, verb, **fields):
            self.calls.append((node, verb, fields))
            if verb == "states":
                return {"puppets": {n: self.states[n] for n in
                                    fields.get("names", [])
                                    if n in self.states}}
            return {}

    STATE468 = {"status": "idle", "alive": True, "listen": True,
                "sessionId": "s1", "model": "claude-opus-5-5",
                "model_at": 999_995}
    t468 = ModelT({"pu-mop-1": {"present": True, "session": "idle 1 1",
                                "clone": None, "state": dict(STATE468)}})
    # Base-запись свежая относительно такта воркера (now=1e6): модель
    # старая -- эффект пойдёт; смена -- подменённый puppets.switch_model.

    def model_worker():
        return one(now=lambda: 1_000_000.0)

    # Успешная смена: эффект -- общий слой, verdict registered -> succeeded.
    # Обёртка ходит bus.request-транспортом (read-глагол узла) и
    # puppets.node_of; шину подменяют на ответы t468: эффект --
    # puppets.switch_model, его тоже считает seen_calls.
    seen_calls = []

    def flip(node, name, model, force=False, **kw):
        seen_calls.append((node, name, model, force))
        t468.states["pu-mop-1"]["state"] = {
            **STATE468, "model": "glm-5.3[1m]", "model_at": 1_000_000}

    with patched(bus, request=lambda node, verb, **f: t468(node, verb, **f)), \
            patched(puppets, switch_model=flip,
                    node_of=lambda name, running=True: "n8"):
        got = model_worker().run("model", "pu-mop-1", {"model": "glm-5.3"},
                                 login="anton", transport=t468, safety=allow)
    c.check("#468 model through the worker succeeds",
            got.get("status") == "succeeded", got)
    c.check("#468 the switcher got the puppet node and the ordered model",
            seen_calls == [("n8", "pu-mop-1", "glm-5.3", False)], seen_calls)

    # Кривой id -- отказ ДО эффекта: ни вызова switcher'а, ни транспорта.
    with patched(puppets, switch_model=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not switch")),
            node_of=lambda *a, **k: "n8"):
        got = model_worker().run("model", "pu-mop-1", {"model": "glm/../etc"},
                                 login="anton", transport=ModelT({}),
                                 safety=allow)
    c.check("#468 an invalid model id is refused before any effect",
            got.get("status") == "refused" and "model id" in (got.get("error") or ""),
            got)

    # Отказ эффекта (диалог не закрылся) -- refused с причиной, не failed:
    # общий ops.switch_model сам переводит отказ эффекта в refused-вердикт.
    t468.states["pu-mop-1"]["state"] = dict(STATE468)
    with patched(bus, request=lambda node, verb, **f: t468(node, verb, **f)), \
            patched(puppets, switch_model=lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("model switch dialog did not close")),
                node_of=lambda *a, **k: "n8"):
        got = model_worker().run("model", "pu-mop-1", {"model": "glm-5.3"},
                                 login="anton", transport=t468, safety=allow)
    c.check("#468 a dialog that did not close reads refused",
            got.get("status") == "refused"
            and "dialog" in (got.get("error") or ""), got)

    # Смена -- не disruptive: сессия живёт, safety не должен получать
    # destructive-флаг. Проверка -- ProbingSafety видит disruptive=False.

    class ModelSafety:
        def __init__(self):
            self.seen = None

        def __call__(self, name, project, **kw):
            self.seen = kw
            return True, None

    probe = ModelSafety()
    t468.states["pu-mop-1"]["state"] = dict(STATE468)
    with patched(bus, request=lambda node, verb, **f: t468(node, verb, **f)), \
            patched(puppets, switch_model=flip,
                    node_of=lambda *a, **k: "n8"):
        model_worker().run("model", "pu-mop-1", {"model": "glm-5.3"},
                           login="anton", transport=t468, safety=probe)
    c.check("#468 model is registered non-disruptive",
            probe.seen is not None and probe.seen.get("disruptive") is False,
            probe.seen)

    # ── карта исходов (#476/#499): вердикт workflow -> статус ───────────
    # Одна точка карты для CLI-вердиктов (outcome) и воркерных (status):
    # адаптер #499 получает эффектом любой из них.
    for kind, status in (("registered", "succeeded"), ("accepted", "partial"),
                         ("refused", "refused"), ("failed", "failed"),
                         ("partial", "partial"), ("unknown", "unknown")):
        c.expect(f"#476 outcome {kind} maps to {status}",
                 opworker.status_of({"outcome": kind}), status)
    c.expect("#476 a worker verdict maps by its own status",
             opworker.status_of({"status": "waiting"}), "failed")
    c.expect("#476 garbage maps to failed, never silence",
             opworker.status_of("junk"), "failed")

    # ── адаптер: verb model в реестре -- 202 приёма, лишний param -- 400 ─
    # Валидатор id живёт в run воркера (проверен выше); здесь -- реестр и
    # схема приёма в одной точке с регистрацией.
    ADMIN = ("anton", "admin")
    wired = adminapi.Operations()
    opworker.register(wired)

    def req(action, target="pu-mop-1", params=None):
        return json.dumps({"action": action, "target": target,
                           "params": params or {}}).encode()

    class ModelHandler:
        """Подставной handler: как Handler в tests/adminapi.py."""

        def __init__(self, body):
            self.who = ADMIN
            self.origin = "https://pool.example"
            self.headers = {"Origin": self.origin,
                            "Content-Type": "application/json",
                            "Content-Length": str(len(body))}
            self.command = "POST"
            self.path = "/api/admin/ops"
            self.rfile = io.BytesIO(body)
            self.sent = []

        def _json(self, code, payload):
            self.sent.append((code, payload))

    def post_model(params):
        # Свежая Api на пост: живая операция прежнего поста не держит
        # ключ -- здесь проверяется реестр и валидатор приёма.
        api = adminapi.Api(wired, origin="https://pool.example", who=ADMIN,
                           executor=lambda *a: {"outcome": "registered"})
        h = ModelHandler(req("model", target="pu-mop-1", params=params))
        api.post(h)
        return h

    h = post_model({"model": "glm-5.3"})
    c.check("#468 model posts one parameter and is accepted (202)",
            h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
    h = post_model({})
    c.check("#468 model without the model parameter is accepted, the run layer refuses",
            h.sent and h.sent[0][0] == 202, h.sent)
    h = post_model({"model": "glm/../etc"})
    c.check("#468 model with an invalid id is accepted, the run layer refuses",
            h.sent and h.sent[0][0] == 202, h.sent)
    h = post_model({"model": "glm-5.3", "force": True})
    c.check("#468 model refuses any other parameter",
            h.sent and h.sent[0][0] == 400, h.sent)

    # ── #499: тонкий исполнитель -- без своей очереди, замка и журнала ──
    # HYPOTHESIS: веб держал две копии «жизни операции» -- очередь задач,
    # _active-замок с 409 и _results с TTL в адаптере, журнал ops.jsonl,
    # payload-файлы, _recover и пер-целевую очередь в воркере. Копии
    # разошлись (#497: вечный accepted, 409 часами), а общий workflow #458
    # уже один. SOLUTION: воркер -- только исполнение: run(verb, target,
    # params, login) -> вердикт ops.* тем же транспортом, что CLI; приём,
    # дедупликация, статусы и публикация -- слой адаптера (adminapi),
    # дюрабельности нет -- рестарт службы честно говорит unknown со
    # ссылкой на журнал. Механизмы очереди/журнала удаляются вместе со
    # своими characterization-проверками -- их гарантии покрывают новые
    # контракты выше и в tests/adminapi.py.
    c.check("#499 the worker exposes run, not submission machinery",
            callable(getattr(opworker.Worker, "run", None))
            and not hasattr(opworker.Worker, "submit")
            and not hasattr(opworker.Worker, "read"), None)
    w499 = opworker.Worker(now=lambda: 1_000.0, observe_wait=0,
                           sleep=lambda s: None)
    t499 = Recorder()
    got499 = w499.run("restart", "pu-mop-1", {}, login="anton",
                      transport=t499,
                      safety=lambda name, project, **kw: (True, None))
    c.check("#499 run drives the library effect to a terminal verdict",
            got499.get("status") in opworker.TERMINAL, got499)
    c.check("#499 run leaves no journal and no payload directory",
            not os.path.exists(os.path.join(w499._root, "ops.jsonl"))
            if hasattr(w499, "_root") else True, None)
    c.check("#499 the executor keeps no durable storage at all",
            not any(hasattr(opworker.Worker, k)
                    for k in ("_recover", "_load", "_append", "_payload",
                              "_tombstones", "_purge")), None)
    # Отказ safety -- отказ операции, эффект не зовётся.
    w499b = opworker.Worker(now=lambda: 1_000.0, observe_wait=0,
                            sleep=lambda s: None)
    t499b = Recorder()
    got499b = w499b.run("restart", "pu-mop-1", {}, login="olga",
                        transport=t499b,
                        safety=lambda name, project, **kw: (False, "led by anton"))
    c.check("#499 a refused safety still refuses before the effect",
            (got499b.get("status"), t499b.calls) == ("refused", []),
            (got499b, t499b.calls))
    # Один вызов -- один прогон: повторного запуска в run нет.
    c.check("#499 run fires the effect once",
            len(t499.effect_rpcs()) == 1, t499.calls)

    return c.report("opworker")


if __name__ == "__main__":
    sys.exit(main())
