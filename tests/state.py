#!/usr/bin/env python3
"""Проверка вердикта папета (mop/state.py) без пула: python3 tests/state.py

Пока состояние собиралось поверх alloc exec, проверить его можно было только
на живом папете — и регрессия однажды спряталась именно здесь: пробник сессии
молча падал в откат по ветке, а `mop list` выглядел исправным. С переездом на
шину агент отдаёт факты, а вердикт собирается чистой функцией, поэтому вся
матрица проверяется здесь.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле, и тестов на него нет.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import puppets  # noqa: E402
from mop.state import (State, action_for, failing_row, failure_reason,  # noqa: E402
                       is_free, silent, task_summary, verdict)

CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
WORK = {"cur": "bug/1063", "def": "master", "dirty": 0, "ahead": 0}


# Свобода стоит под выбором жертв в mop gc: снести чужую работу из-за
# неверного ответа — дорогая опечатка. Пока вердикт был строкой, правило было
# «free по префиксу», потому что свободное место несёт ещё и ветку; теперь это
# вид, и ветка на ответ не влияет вовсе (#145).
FREE_CASES = [
    (State("free"), True),
    (State("free", branch="master"), True),
    (State("free", branch="bug/1063"), True),
    (State("busy"), False),
    (State("idle", "uncommitted: 3", "bug/1063"), False),
    (State("hung", "not responding"), False),
    (silent("node agent node1 silent for 20s"), False),
    (State("dialog"), False),
    (State("unknown", "no clone data"), False),
    (None, False),
]


def facts(session, clone=CLEAN, screen="Herding bytes"):
    return {"present": True, "screen": screen, "session": session, "clone": clone}


CASES = [
    # (что случилось, факты, ожидаемое состояние)
    ("node doesn't recognize the puppet as its own",
     {"present": False}, "HUNG (no tmux session)"),
    ("agent returned an error",
     {"error": "tmux not responding"}, "HUNG (tmux not responding)"),
    ("agent knows nothing about the puppet", None, "HUNG (no answer)"),
    ("empty pane — puppet just came up",
     facts("idle 1 1", screen="   \n\n"), "free"),

    # Сокет = живость, файл = активность. Сочетания разбираются здесь.
    ("session alive and working", facts("busy 1 1", WORK), "busy: bug/1063"),
    # claude завёл статус shell (выполняет команду) — для нас это та же работа.
    # Пока он был незнаком, вердикт уходил в скоринг по буферу: ветка терялась,
    # а занятый папет мог быть объявлен свободным.
    ("session is running a shell command", facts("shell 1 1", WORK), "busy: bug/1063"),
    ("unknown status is shown as is, never as free",
     facts("compacting 1 1", WORK), "compacting: bug/1063"),
    ("unknown status without a branch of its own",
     facts("compacting 1 1", CLEAN), "compacting"),
    ("working on the default branch", facts("busy 1 1", CLEAN), "busy: master"),
    ("process alive, but the socket is silent", facts("hung 1 0"), "HUNG (not responding)"),
    ("process dead, file stale", facts("idle 0 0"), "HUNG (not responding)"),
    ("stuck on an action request",
     facts("requires_action 1 1"), "needs action"),
    ("waiting for input — leave it alone", facts("waiting 1 1"), "waiting for input"),

    # Свободен = в клоне нечего терять. На этом стоит решение о диспатче.
    ("clean and everything's on origin", facts("idle 1 1", CLEAN), "free (master)"),
    ("foreign branch, but everything's pushed",
     facts("idle 1 1", WORK), "free (bug/1063)"),
    ("uncommitted files",
     facts("idle 1 1", {**WORK, "dirty": 3}), "idle: bug/1063 (uncommitted: 3)"),
    ("unpushed commits",
     facts("idle 1 1", {**WORK, "ahead": 2}), "idle: bug/1063 (unpushed: 2)"),
    ("both at once — two numbers, not a sum",
     facts("idle 1 1", {**WORK, "dirty": 3, "ahead": 2}),
     "idle: bug/1063 (uncommitted: 3, unpushed: 2)"),

    # Жалобы видны только на экране: сессия жива и отвечает, а ход выдать не может.
    ("login expired",
     facts("idle 1 1", WORK, "Login expired · Please run /login"),
     "login expired: bug/1063"),
    # Живая жалоба про логин стоит в статус-баре под рамкой ввода. Снято с
    # живого папета 2026-08-31.
    ("not logged in — жалоба в статус-баре",
     facts("idle 1 1", CLEAN,
           "\u203b recap: ticket landed, waiting for the master\n"
           "\u276f \n  -- INSERT -- bypass permissions on \u00b7 2 agents\n"
           "                    Not logged in \u00b7 Run /login"),
     "not logged in"),
    # А жалоба из прошлого приезжает вместе с историей обычной репликой посреди
    # буфера — папет при этом залогинен и здоров.
    ("восстановленная сессия принесла старую жалобу про логин",
     facts("idle 1 1", CLEAN,
           "\u25cf 502 on 127.0.0.1:2001 — backend did not come up\n"
           "\u25cf Login expired \u00b7 Please run /login\n"
           "\u273b Churned for 43m 17s \u00b7 done 4:42 PM\n"
           "\u276f \n  -- INSERT -- bypass permissions on \u00b7 2 agents"),
     "free (master)"),
    ("not logged in at all",
     facts("idle 1 1", CLEAN, "Not logged in · Run /login"), "not logged in"),
    # Подъём истории спрашивает, чем поднимать длинную сессию. Файл сессии при
    # этом здоров, и без экрана папет читался бы свободным.
    ("stuck on the choice of how to resume history",
     facts("idle 1 1", WORK,
           "This session is 1h 30m old and 251.5k tokens.\n"
           "  1. Resume from summary (recommended)\n"
           "  2. Resume full session as-is\n  3. Don't ask me again\n"
           "  Enter to confirm \u00b7 Esc to cancel"),
     "needs action: resume prompt"),
    ("any other dialog — by the footer, no need to know the question",
     facts("idle 1 1", CLEAN,
           "Do you want to proceed?\n  1. Yes\n  2. No\n"
           "  Enter to confirm \u00b7 Esc to cancel"),
     "needs action: dialog"),
    ("footer scrolled up — the question's already answered",
     facts("busy 1 1", CLEAN,
           "  Enter to confirm \u00b7 Esc to cancel\n"
           "Herding bytes\n  6 tasks (3 done)"), "busy: master"),
    ("model quota ran out",
     facts("idle 1 1", CLEAN, "You're out of usage credits. keep using Opus 4.5"),
     "no model quota: Opus 4.5"),
    ("quota was hit, but the model's already switched",
     facts("busy 1 1", CLEAN,
           "out of usage credits\nSet model to sonnet\nHerding bytes"), "busy: master"),
    # Отказ провайдера: в таблицу едет средний блок скобок. Код (1308) и
    # request id мастеру не говорят ничего, «Request rejected (429)» умалчивает
    # время возврата квоты — а именно оно решает, ждать папета или переводить.
    ("provider refused for quota, message wrapped by the renderer",
     facts("idle 1 1", WORK,
           "● API Error: Request rejected (429) · [1308][Usage limit reached for"
           " 5 hour. Your limit will reset at 2026-08-31\n"
           "  18:19:41][20260831150427d7cd9f9634d84ecc]\n"
           "✻ Brewed for 57m 29s · done 2:04 PM\n"
           "  6 tasks (3 done, 1 in progress, 2 open)"),
     "error: Usage limit reached for 5 hour. Your limit will reset at"
     " 2026-08-31 18:19:41"),
    ("provider refusal without brackets — show what we have",
     facts("idle 1 1", CLEAN, "● API Error: Connection error"),
     "error: Connection error"),
    # Пара снята с живого пула 2026-08-31: обе сессии несут в буфере один и тот
    # же отказ, но первую восстановили вместе со скроллбэком, и она работает.
    # Отличает их не жалоба, а то, что под ней.
    ("restored session brought a refusal from history and is working",
     facts("idle 1 1", WORK,
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "✻ Brewed for 57m 29s · done 2:04 PM\n"
           "● Session model glm-5.3 could not be restored — using opus instead\n"
           "❯ продолжай\n  \u23bf  4 skills available\n  Ran 3 shell commands"),
     "free (bug/1063)"),
    ("same complaint, but underneath is only a cut-off turn — the puppet really is stuck",
     facts("idle 1 1", WORK,
           "  Ran 2 shell commands\n"
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "✻ Baked for 49m 57s · done 2:04 PM\n"
           "  2 tasks (0 done, 1 in progress, 1 open)\n"
           "  \u25fc Add clo gitlab fetch-redemption"),
     "error: Usage limit reached"),
    ("there was a refusal, but the model's already switched",
     facts("busy 1 1", CLEAN,
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "Set model to sonnet\nHerding bytes"), "busy: master"),

    # Файла сессии нет (старый claude / нет python3) -> откаты.
    ("no session file, but the pane says working",
     facts("none", CLEAN, "Working... running tests"), "busy"),
    ("no session file, pane is silent -> branch alone",
     facts("none", WORK, "какой-то текст"), "busy: bug/1063"),
    ("no session file, no clone either",
     facts("none", None, "какой-то текст"), "unknown (no clone data)"),

    # Клон — последнее слово на каждом пути к «free», а не только на пути через
    # файл сессии. Четвёрка ниже снята с бага 01.09 (pu-rugent-3): три дороги
    # в обход клона и одна в обход самих данных.
    ("no session file, work sits on the default branch — the fallback used to"
     " hide it, since it deliberately mutes the default branch name",
     facts("none", {**CLEAN, "ahead": 2}, "какой-то текст"),
     "idle: master (unpushed: 2)"),
    ("empty pane over a dirty clone — the window right after a restart, where"
     " unsaved work is exactly what's at stake",
     facts("idle 1 1", {**CLEAN, "dirty": 3}, "   \n\n"),
     "idle: master (uncommitted: 3)"),
    ("buffer scoring says idle, but the clone holds unpushed work",
     facts("none", {**WORK, "ahead": 2}, "waiting in reserve"),
     "idle: bug/1063 (unpushed: 2)"),
    ("session is idle and the clone probe brought nothing back",
     facts("idle 1 1", None), "unknown (no clone data)"),
    ("half a clone answer is no answer: a missing count is not a zero",
     facts("idle 1 1", {"cur": "master", "def": "master"}),
     "unknown (no clone data)"),
]



# ── #145: лечение и свобода по каждому состоянию ─────────────────────────────
# HYPOTHESIS: вердикт папета — английская фраза, и её потребители (doctor,
# is_free, корзина дашборда, отчёт deploy) решают по префиксу. Переформулировка
# сообщения молча меняет лечение и решение «свободен», на котором стоит диспатч.
# SOLUTION: тип State(kind, detail, branch) в mop/state.py; потребители смотрят
# в kind, а строка — только для показа.
#
# Характеризация: таблица ниже снята с ТЕКУЩИХ функций до переезда (строка ->
# лечение, свобода, корзина дашборда) и прогнана зелёной на них. Переезд не
# трогает таблицу; меняется только judge() — единственное место, которое
# знает, как спросить код. Каждое состояние, которое пул умеет произвести:
# вердикты по фактам (CASES), молчащий агент (SILENT) и падающий на старте.
#
# (строка состояния, лечение doctor, свободен, корзина дашборда)
TREATMENT = [
    ("HUNG (no tmux session)", "restart", False, "sick"),
    ("HUNG (tmux not responding)", "restart", False, "sick"),
    ("HUNG (no answer)", "restart", False, "sick"),
    ("HUNG (not responding)", "restart", False, "sick"),
    ("free", False, True, "free"),
    ("free (master)", False, True, "free"),
    ("free (bug/1063)", False, True, "free"),
    ("busy", False, False, "busy"),
    ("busy: bug/1063", False, False, "busy"),
    ("busy: master", False, False, "busy"),
    ("compacting: bug/1063", False, False, "busy"),
    ("compacting", False, False, "busy"),
    ("needs action", "restart", False, "sick"),
    ("needs action: resume prompt", "restart", False, "sick"),
    ("needs action: dialog", "restart", False, "sick"),
    ("waiting for input", False, False, "busy"),
    ("idle: bug/1063 (uncommitted: 3)", False, False, "busy"),
    ("idle: bug/1063 (unpushed: 2)", False, False, "busy"),
    ("idle: bug/1063 (uncommitted: 3, unpushed: 2)", False, False, "busy"),
    ("idle: master (unpushed: 2)", False, False, "busy"),
    ("idle: master (uncommitted: 3)", False, False, "busy"),
    ("login expired: bug/1063", "login+restart", False, "sick"),
    ("not logged in", "login+restart", False, "sick"),
    ("no model quota: Opus 4.5", "model", False, "sick"),
    ("error: Usage limit reached for 5 hour. Your limit will reset at"
     " 2026-08-31 18:19:41", "model", False, "sick"),
    ("error: Connection error", "model", False, "sick"),
    ("error: Usage limit reached", "model", False, "sick"),
    ("unknown (no clone data)", False, False, "busy"),
    ("AGENT SILENT (no responders)", None, False, "silent"),
    ("AGENT SILENT (nats: timeout)", None, False, "silent"),
]

# Молчащий агент рождается не из фактов, а из ответа шины: (ответ, строка).
SILENT = [("no responders", "AGENT SILENT (no responders)"),
          ("nats: timeout", "AGENT SILENT (nats: timeout)")]


def judge(case):
    """Как код отвечает про одно состояние: (строка, лечение, свободен, корзина).
    case — факты из CASES либо ("silent", ответ шины).

    До переезда здесь стояли строковые функции puppets.puppet_state,
    puppets._action_for, is_free(строка) и строка вместо kind в row — таблица
    TREATMENT была зелёной на них. После переезда решения берутся из вида, а
    строка — из str(State): так таблица проверяет заодно, что str() воспроизводит
    каждую строку дословно."""
    from mop import web
    v = silent(case[1]) if isinstance(case, tuple) else verdict(case)
    row = {"alloc_status": "running", "state": str(v), "kind": v.kind}
    return str(v), action_for(v.kind), is_free(v.kind), web.classify(row)


def check_treatment():
    bad, cases = 0, 0
    table = {s: rest for s, *rest in TREATMENT}
    seen = set()
    inputs = [given for _, given, _ in CASES] + [("silent", a) for a, _ in SILENT]
    for given in inputs:
        cases += 1
        state, *got = judge(given)
        seen.add(state)
        if state not in table:
            bad += 1
            print(f"FAILED  treatment: {state!r} is not in the table")
        elif list(table[state]) != got:
            bad += 1
            print(f"FAILED  treatment of {state!r}: wanted {table[state]}, got {got}")
    # Таблица без лишних строк: каждая её строка кем-то произведена.
    for state in set(table) - seen:
        bad += 1
        print(f"FAILED  treatment: nobody produces {state!r}")
    return bad, cases + 1

# ── ростер глазами одного проекта (#29) ──────────────────────────────────────
# HYPOTHESIS: джоба-папет без origin в Meta (старая регистрация) невидима
# любому списку, включая admin, — что и выглядело как «папеты исчезают»:
# pu-rugent-1..10 жили на hyper running, невидимые всему.
# SOLUTION: определение папета — service-джоб с префиксом pu- (pu-cleanup и
# его периодические дети — sysbatch); origin — свойство современной спеки, а
# не пропуск в ростер. Admin видит и непомеченных, проект — только своих.
def _job(name, jtype="service", origin=None):
    return {"ID": name, "Type": jtype,
            "Meta": {"origin": origin} if origin else None}


# ── память проектов: origin'ы и легаси-имена (#33) ─────────────────────────────
# HYPOTHESIS: память обязана хранить origin'ы (имя выводится basename'ом, а
# вот имя в origin не разворачивается), но строки-имена от легаси-времён
# терять нельзя: их origin уже не узнать, а потеря имени молча выписывает
# проекта из конфига NATS при следующем deploy.
def check_project_ids(cases):
    bad = 0
    for what, lines, want_o, want_n in cases:
        got_o, got_n = puppets.project_ids(lines)
        if got_o != want_o or got_n != want_n:
            bad += 1
            print(f"FAILED  project_ids, {what}: origins {got_o!r} names {got_n!r}")
    return bad, len(cases)


PROJECT_IDS = [
    ("origin'ы узнаются, имена остаются",
     ["git@h:ermak/mop.git", "https://h/rugent/rugent.git", "backup"],
     {"git@h:ermak/mop.git", "https://h/rugent/rugent.git"}, {"backup"}),
    ("пусто", [], set(), set()),
    ("пустые строки не считаются", ["", "  ", "mop"], set(), {"mop"}),
]


def check_visible(cases):
    bad = 0
    for what, listing, project, want in cases:
        got = puppets.visible(listing, project)
        got_ids = [j["ID"] for j in got]
        if sorted(got_ids) != sorted(want):
            bad += 1
            print(f"FAILED  visible, {what}: wanted {want}, got {got_ids}")
    return bad, len(cases)


VISIBLE = [
    # Главное: непомеченный работающий папет виден admin — «исчезнувший» пул
    # обязан быть виден хоть оператору.
    ("непомеченный виден admin", [_job("pu-rugent-8")], "admin", ["pu-rugent-8"]),
    ("непомеченного не видит проект", [_job("pu-rugent-8")], "mop", []),
    ("свой по origin виден проект", [_job("pu-mop-1", origin="…/mop.git")], "mop",
     ["pu-mop-1"]),
    ("чужой по origin не виден проект", [_job("pu-mop-1", origin="…/mop.git")], "rugent", []),
    ("admin видит всех", [_job("pu-mop-1", origin="…/mop.git"), _job("pu-rugent-8")],
     "admin", ["pu-mop-1", "pu-rugent-8"]),
    # pu-cleanup и его периодические дети — sysbatch, не папеты: их префикс
    # pu- обманчив, и в ростере им места нет ни для кого.
    ("watchdog не виден admin", [_job("pu-cleanup", "sysbatch")], "admin", []),
    ("периодический ребёнок не виден admin",
     [_job("pu-cleanup/periodic-1", "sysbatch")], "admin", []),
]


# ── #126: падающий на старте папет. Nomad держит аллокацию в restart-backoff
# в `pending`, и ростер показывал его ищущим место. Образец -- настоящий
# stderr pu-rugent-1 (две попытки подряд, bootstrap без файлов на сервере).
STDERR = """pu-rugent-1: body 9828 at 10.77.38.100
bootstrap of pu-rugent-1 failed: bootstrap failed (ansible exit 2):
TASK [Project env files of rugent] ***
[ERROR]: Task failed: Unexpected AnsibleActionFail error: Could not find or access '~/rugent/.env' on the Ansible Controller.
If you are using a module and expect the file to exist on the remote, see the remote_src option
PLAY RECAP *****
bootstrap of pu-rugent-1 failed: bootstrap failed (ansible exit 2):
[ERROR]: Task failed: Unexpected AnsibleActionFail error: Could not find or access '~/rugent/.env-prod' on the Ansible Controller.
If you are using a module and expect the file to exist on the remote, see the remote_src option
Origin: /home/ermak/.config/mop/bootstrap/rugent-tasks.yml:8:3
[ERROR]: Task failed: Unexpected AnsibleActionFail error: Could not find or access '~/rugent/.providers' on the Ansible Controller.
PLAY RECAP *****
10.77.38.100               : ok=3    changed=0    unreachable=0    failed=1
"""
ALLOC = {"ClientStatus": "pending", "TaskStates": {"claude": {
    "State": "pending", "Failed": False, "Restarts": 4,
    "Events": [{"Type": "Started"},
               {"Type": "Terminated", "ExitCode": 1},
               {"Type": "Restarting", "StartDelay": 1576228170008,
                "Time": 1_000_000_000_000}]}}}


def check_failing():
    """HYPOTHESIS (#126): ростер читал только ClientStatus. SOLUTION: сводка
    задачи и причина из stderr. STATUS: FIXED — see #126"""
    bad, cases = 0, 0
    cases += 1
    t = task_summary(ALLOC, now=1000)
    want = {"state": "pending", "restarts": 4, "exit": 1, "next_s": 1576, "failed": False}
    if t != want:
        bad += 1
        print(f"FAILED  task_summary -> {t}, wanted {want}")
    # Срок -- оставшийся, а не задержка на момент события: через десять минут
    # «next in 26m» было бы неправдой.
    cases += 1
    later = task_summary(ALLOC, now=1000 + 600)
    if later["next_s"] != 976:
        bad += 1
        print(f"FAILED  next_s must count down from the event: {later['next_s']}")
    cases += 1
    if task_summary(ALLOC, now=1000 + 99999)["next_s"] != 0:
        bad += 1
        print("FAILED  a passed deadline is 0, not negative")
    # Причина -- из последней попытки, первая ошибка после строки врапера.
    cases += 1
    got = failure_reason(STDERR)
    want = "bootstrap: Could not find or access '~/rugent/.env-prod' on the Ansible Controller."
    if got != want:
        bad += 1
        print(f"FAILED  failure_reason -> {got!r}, wanted {want!r}")
    # Без знакомых строк -- последняя непустая, а пусто -- None.
    cases += 1
    if failure_reason("x\nsomething broke\n\n") != "something broke" \
            or failure_reason("") is not None:
        bad += 1
        print("FAILED  failure_reason must fall back to the last line, and None on empty")
    # Строка ростера: падающий -- failing с причиной и сроком, а не pending.
    cases += 1
    fn = failing_row
    got = fn("pending", t, "bootstrap: no file") if fn else None
    want = ("failing", "FAILED: bootstrap: no file (4 restarts, next in 26m)")
    if got != want:
        bad += 1
        print(f"FAILED  failing_row -> {got}, wanted {want}")
    # Работающая задача и задача без падений -- не failing.
    for task in ({"state": "running", "restarts": 4, "exit": 1, "next_s": None, "failed": False},
                 {"state": "pending", "restarts": 0, "exit": None, "next_s": None, "failed": False},
                 None):
        cases += 1
        if fn and fn("pending", task, None) is not None:
            bad += 1
            print(f"FAILED  failing_row must be None for {task}")
    # Задача исчерпала попытки -- тоже failing, без срока.
    cases += 1
    dead = {"state": "dead", "restarts": 9, "exit": 1, "next_s": None, "failed": True}
    got = fn("failed", dead, "boom") if fn else None
    if got != ("failing", "FAILED: boom (9 restarts, gave up)"):
        bad += 1
        print(f"FAILED  failing_row for a task that gave up -> {got}")
    return bad, cases


def main():
    bad = 0
    for what, given, want in CASES:
        got = str(verdict(given))
        if got != want:
            bad += 1
            print(f"FAILED  {what}\n  wanted:  {want!r}\n  got: {got!r}")
    cases = len(CASES)
    vbad, vcases = check_visible(VISIBLE)
    sbad, scases = check_project_ids(PROJECT_IDS)
    bad += sbad
    cases += scases
    bad += vbad
    cases += vcases
    fbad, fcases = check_failing()
    bad += fbad
    cases += fcases
    tbad, tcases = check_treatment()
    bad += tbad
    cases += tcases
    for state, want in FREE_CASES:
        cases += 1
        if is_free(state and state.kind) != want:
            bad += 1
            print(f"FAILED  is_free({state!r})")
    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
