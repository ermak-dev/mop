#!/usr/bin/env python3
"""Проверка вердикта папета (mop/common/state.py) без пула: python3 tests/state.py

Пока состояние собиралось поверх alloc exec, проверить его можно было только
на живом папете — и регрессия однажды спряталась именно здесь: пробник сессии
молча падал в откат по ветке, а `mop list` выглядел исправным. С переездом на
шину агент отдаёт факты, а вердикт собирается чистой функцией, поэтому вся
матрица проверяется здесь.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле, и тестов на него нет.
"""
import json
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from _lib import Checks, patched  # noqa: E402
from mop.common import puppets  # noqa: E402
from mop import session  # noqa: E402
from mop.common.state import PuppetRow, State, action_for, failing_row, failure_reason, is_free, silent, task_summary, verdict

CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
# Папет на ветке своего мастера (#256): она и дом клона (#272).
WORK = {"cur": "bug/1063", "def": "master", "home": "bug/1063", "dirty": 0, "ahead": 0}
# Тот же клон у старого агента: ключа home нет, дом -- ветка по умолчанию.
OLD_WORK = {k: v for k, v in WORK.items() if k != "home"}


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


def facts(session, clone=CLEAN, screen=None):
    """Факты с узла. screen — только чтобы показать, что вердикт его не читает
    (#235); агент его больше не шлёт (#236)."""
    got = {"present": True, "session": session, "clone": clone}
    return got if screen is None else {**got, "screen": screen}


CASES = [
    # (что случилось, факты, ожидаемое состояние)
    ("node doesn't recognize the puppet as its own",
     {"present": False}, "HUNG (no tmux session)"),
    ("agent returned an error",
     {"error": "tmux not responding"}, "HUNG (tmux not responding)"),
    ("agent knows nothing about the puppet", None, "HUNG (no answer)"),

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
    # #272: чистый клон не на доме держит тикет (папет ждёт приёма отчёта);
    # дом -- ветка мастера из меты, у старого агента -- ветка по умолчанию.
    ("foreign branch, everything's pushed, but off its home -- holds a ticket",
     facts("idle 1 1", {**WORK, "home": "master"}), "idle: bug/1063 (off home master)"),
    ("the same from an old agent: no home key, home is the default branch",
     facts("idle 1 1", OLD_WORK), "idle: bug/1063 (off home master)"),
    ("the master's branch is its home, everything's pushed",
     facts("idle 1 1", WORK), "free (bug/1063)"),
    ("uncommitted files",
     facts("idle 1 1", {**WORK, "dirty": 3}), "idle: bug/1063 (uncommitted: 3)"),
    ("unpushed commits",
     facts("idle 1 1", {**WORK, "ahead": 2}), "idle: bug/1063 (unpushed: 2)"),
    ("both at once — two numbers, not a sum",
     facts("idle 1 1", {**WORK, "dirty": 3, "ahead": 2}),
     "idle: bug/1063 (uncommitted: 3, unpushed: 2)"),

    # Файла сессии нет (сессия ещё не поднялась) -> откат по клону.
    ("no session file -> branch alone",
     facts("none", WORK), "busy: bug/1063"),
    ("no session file, no clone either",
     facts("none", None), "unknown (no clone data)"),

    # Клон — последнее слово на каждом пути к «free», а не только на пути через
    # файл сессии. Строки ниже сняты с бага 01.09 (pu-rugent-3); дороги через
    # экран ушли вместе с экраном (#235).
    ("no session file, work sits on the default branch — the fallback used to"
     " hide it, since it deliberately mutes the default branch name",
     facts("none", {**CLEAN, "ahead": 2}),
     "idle: master (unpushed: 2)"),
    ("idle session on the default branch over a dirty clone — the window right"
     " after a restart, where unsaved work is exactly what's at stake",
     facts("idle 1 1", {**CLEAN, "dirty": 3}),
     "idle: master (uncommitted: 3)"),
    ("session is idle and the clone probe brought nothing back",
     facts("idle 1 1", None), "unknown (no clone data)"),
    ("half a clone answer is no answer: a missing count is not a zero",
     facts("idle 1 1", {"cur": "master", "def": "master"}),
     "unknown (no clone data)"),
]


# ── #235: вердикт без экрана ─────────────────────────────────────────────────
# HYPOTHESIS: разбор пейна слеп. Пикер модели (`/model` без аргумента) стоит
# над футером «Enter to set as default · s to use this session only · Esc to
# cancel», а _screen_complaint ищет «Enter to confirm»; 24.09 он же не увидел
# «Login expired» посреди хода. Живой диалог ростер поймал только по
# waitingFor (#224). Все папета mop несут хуки, так что экран больше не нужен.
# SOLUTION: состояние — из статуса сессии, waitingFor и записи хода; без
# записи хода — из одного статуса сессии. Экран остаётся для глаз (tail,
# slash, attach), в факты он не входит. Папет без хуков — случай
# перерегистрации, его показывает doctor, а не угадывает экран.
# STATUS: FIXED — see #235
SCREENLESS = [
    ("no turn record, idle session: a login complaint on the screen is ignored",
     facts("idle 1 1", CLEAN, "Not logged in · Run /login"), "free (master)"),
    ("no turn record, idle session: a dialog footer on the screen is ignored",
     facts("idle 1 1", CLEAN, "Do you want to proceed?\n"
           "  Enter to confirm · Esc to cancel"), "free (master)"),
    ("no session file: the clone decides, the pane's words do not",
     facts("none", CLEAN, "Working... running tests"), "free"),
    ("facts without a screen key: the session status decides",
     {"present": True, "session": "idle 1 1", "clone": CLEAN}, "free (master)"),
]


# ── #224: исход хода — из хуков, а не с экрана ───────────────────────────────
# HYPOTHESIS: 24.09 pu-mop-2 и pu-mop-3 умерли посреди хода на «Login expired ·
# Please run /login» и полтора часа читались в ростере «idle: <ветка>
# (uncommitted: N)». Жалоба нарисована репликой над рамкой ввода, а
# _screen_complaint ищет логин только в трёх нижних строках (статус-бар) —
# нарочно, чтобы не ловить старую жалобу из истории `--continue`. Экран — не
# тот источник: исход хода claude сообщает хуком StopFailure с кодом ошибки.
# SOLUTION: агент шлёт факт state (ответ `session.py state`, #222) с записью
# хода turn; при ней вердикт решает по turn и статусу сессии, мимо экрана. Без
# записи хода (папет не перерегистрирован с хуками, #223) — прежний путь.
# RESULT: записи хода — не рукописные: настоящие входы хуков из
# tests/session_hooks.json (#222) прогоняются через session.turn_record, тот
# же код, что пишет их на узле. authentication_failed захвачен не был —
# это захваченный StopFailure с подменённым кодом и текстом.
# STATUS: FIXED — see #224

# Экран инцидента: жалоба — реплика над рамкой ввода, статус-бар чистый.
MIDTURN = ("● Reading mop/cli/user.py\n"
           "● Login expired · Please run /login\n"
           "╭───╮\n│ ❯  │\n╰───╯\n"
           "  -- INSERT -- bypass permissions on · 2 agents")


with open(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                       "session_hooks.json")) as _f:
    HOOKS = json.load(_f)


def turn(event, **over):
    """Запись хода из захваченного входа хука; over подменяет поля входа,
    None — убирает поле."""
    payload = dict(HOOKS["fail" if event == "StopFailure" else "ok"][event])
    for k, v in over.items():
        payload.pop(k) if v is None else payload.__setitem__(k, v)
    return session.turn_record(payload, 1790245436)


def hooked(status, turn_rec, clone=WORK, waiting_for=None, screen=MIDTURN,
           alive=True, listen=True):
    st = {"status": status, "waitingFor": waiting_for, "alive": alive,
          "listen": listen, "turn": turn_rec}
    return {**facts(f"{status} {int(alive)} {int(listen)}", clone, screen), "state": st}


FAILED_AUTH = turn("StopFailure", error="authentication_failed",
                   last_assistant_message="Login expired · Please run /login")
RATE = ("API Error: Request rejected (429) · Usage limit reached for 5 hour. "
        "Your limit will reset at 2026-09-24 15:00:00")

HOOKED = [
    # (что случилось, факты, строка, свободен ли)
    ("the incident: a mid-turn login failure over a dirty clone",
     hooked("idle", FAILED_AUTH, {**WORK, "dirty": 2}),
     "login expired: bug/1063", False),
    ("a login failure on the default branch carries no branch, as today",
     hooked("idle", FAILED_AUTH, CLEAN), "login expired", False),
    ("a new turn is already running: busy beats the failure record",
     hooked("busy", FAILED_AUTH), "busy: bug/1063", False),
    ("a shell command is running: the same",
     hooked("shell", FAILED_AUTH), "busy: bug/1063", False),
    ("Stop after a failure: the turn went through, the clone decides",
     hooked("idle", turn("Stop")), "free (bug/1063)", True),
    ("Stop after a failure over a dirty clone",
     hooked("idle", turn("Stop"), {**WORK, "dirty": 2}),
     "idle: bug/1063 (uncommitted: 2)", False),
    ("the screen still shows the old complaint, but a later turn cleared it",
     hooked("idle", turn("UserPromptSubmit"), CLEAN), "free (master)", True),
    # Решение оператора: до первого хода папет свободен, «Not logged in» в
    # статус-баре поймает первый же ход. Запись хода — значит, экран не читаем.
    ("not logged in before the first turn: free, the first turn catches it",
     hooked("idle", turn("SessionStart"), CLEAN,
            screen="❯ \n  -- INSERT --\n                    Not logged in · Run /login"),
     "free (master)", True),
    ("waiting on an open dialog",
     hooked("waiting", turn("UserPromptSubmit"), waiting_for="dialog open"),
     "needs action: dialog open", False),
    ("waiting with no reason given — as today",
     hooked("waiting", turn("Stop")), "waiting for input", False),
    ("rate limit: the refusal text, not the code",
     hooked("idle", turn("StopFailure", error="rate_limit", last_assistant_message=RATE)),
     f"error: {RATE}", False),
    ("billing error is the quota kind",
     hooked("idle", turn("StopFailure", error="billing_error",
                         last_assistant_message="Credit balance is too low")),
     "no model quota: Credit balance is too low", False),
    ("the captured failure verbatim: model_not_found, the refusal text shown",
     hooked("idle", turn("StopFailure")),
     "error: " + HOOKS["fail"]["StopFailure"]["last_assistant_message"], False),
    ("an unknown code is an error, never free",
     hooked("idle", turn("StopFailure", error="brand_new_code",
                         last_assistant_message=None), CLEAN),
     "error: brand_new_code", False),
    ("a StopFailure without a code is recorded as unknown, still an error",
     hooked("idle", turn("StopFailure", error=None, last_assistant_message=None), CLEAN),
     "error: unknown", False),
    ("a dead session: the failure record does not hide it",
     hooked("idle", FAILED_AUTH, alive=False, listen=False), "HUNG (not responding)", False),
    # Без записи хода — один статус сессии (#235), экран не читается.
    ("no turn record: the session status alone, the complaint on screen ignored",
     hooked("idle", None, CLEAN), "free (master)", True),
    ("a model switch after a failure clears it",
     hooked("idle", turn("Stop", hook_event_name="PostModelSwitch")), "free (bug/1063)", True),
    ("no session at all: {status: null} reads as no session file",
     {**facts("none", {**CLEAN, "ahead": 2}),
      "state": {"status": None, "waitingFor": None, "alive": False,
                "listen": False, "turn": None}},
     "idle: master (unpushed: 2)", False),
]


def check_hooked(c, cases):
    for what, given, want, free in cases:
        got = verdict(given)
        c.check(f"#224 {what}", not (str(got) != want or is_free(got.kind) != free),
                f"wanted {want!r} free={free}, got {str(got)!r} free={is_free(got.kind)}")



# ── #145: лечение и свобода по каждому состоянию ─────────────────────────────
# HYPOTHESIS: вердикт папета — английская фраза, и её потребители (doctor,
# is_free, корзина дашборда, отчёт deploy) решают по префиксу. Переформулировка
# сообщения молча меняет лечение и решение «свободен», на котором стоит диспатч.
# SOLUTION: тип State(kind, detail, branch) в mop/common/state.py; потребители смотрят
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
    ("busy: bug/1063", False, False, "busy"),
    ("busy: master", False, False, "busy"),
    ("compacting: bug/1063", False, False, "busy"),
    ("compacting", False, False, "busy"),
    ("needs action", "restart", False, "sick"),
    ("needs action: dialog open", "restart", False, "sick"),
    ("waiting for input", False, False, "busy"),
    ("idle: bug/1063 (uncommitted: 3)", False, False, "busy"),
    ("idle: bug/1063 (unpushed: 2)", False, False, "busy"),
    ("idle: bug/1063 (uncommitted: 3, unpushed: 2)", False, False, "busy"),
    ("idle: bug/1063 (off home master)", False, False, "busy"),
    ("idle: bug/1063 (uncommitted: 2)", False, False, "busy"),
    ("idle: master (unpushed: 2)", False, False, "busy"),
    ("idle: master (uncommitted: 3)", False, False, "busy"),
    ("login expired: bug/1063", "login+restart", False, "sick"),
    ("login expired", "login+restart", False, "sick"),
    ("no model quota: Credit balance is too low", "model", False, "sick"),
    (f"error: {RATE}", "model", False, "sick"),
    ("error: " + HOOKS["fail"]["StopFailure"]["last_assistant_message"], "model", False, "sick"),
    ("error: brand_new_code", "model", False, "sick"),
    ("error: unknown", "model", False, "sick"),
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
    from mop.server import web
    v = silent(case[1]) if isinstance(case, tuple) else verdict(case)
    row = PuppetRow("pu-x-1", "n", "running", str(v), v.kind, "-", "claude", "?")
    return str(v), action_for(v.kind), is_free(v.kind), web.classify(row)


def check_treatment(c):
    table = {s: rest for s, *rest in TREATMENT}
    seen = set()
    inputs = ([given for _, given, _ in CASES + SCREENLESS]
              + [given for _, given, _, _ in HOOKED] + [("silent", a) for a, _ in SILENT])
    for given in inputs:
        state, *got = judge(given)
        seen.add(state)
        if c.check(f"treatment: {state!r} is in the table", state in table):
            c.expect(f"treatment of {state!r}", got, list(table[state]))
    # Таблица без лишних строк: каждая её строка кем-то произведена.
    unproduced = set(table) - seen
    c.check("treatment: every table row is produced", not unproduced,
            f"nobody produces {sorted(unproduced)!r}")


# ── #174: устаревшая спека и лечение ─────────────────────────────────────────
# Перерегистрация перезапускает сессию: свободному папету это ничего не стоит,
# занятому — убивает ход. Поэтому устаревшая спека лечится `update` только у
# свободного папета и у того, где сессии нет вовсе (аллокация не бежит); у
# остальных она показана и ждёт. После раскатки #174 устаревшими читаются
# ВСЕ папеты разом — ключа версии нет ни у кого, — и doctor --fix обязан
# тронуть только свободных.
SPEC_ACTION = [
    ("free", "update"), (None, "update"),
    ("busy", None), ("idle", None), ("waiting", None), ("dialog", None),
    ("hung", None), ("silent", None), ("login", None), ("quota", None),
    ("error", None), ("unknown", None), ("other", None),
]


def check_stale_spec(c):
    """STATUS: FIXED — see #174"""
    from mop.common import state
    fn = getattr(state, "spec_action", None)
    for kind, want in SPEC_ACTION:
        got = fn(kind) if fn else "missing"
        c.expect(f"spec_action({kind!r})", got, want)
    # doctor целиком: roster подменён, лечение — из diagnose.
    run = {"ClientStatus": "running", "NodeName": "n1"}

    def item(name, kind, st, stale=True, alloc=run):
        return {"job": {"ID": name}, "alloc": alloc, "stale": stale,
                "state": st, "kind": kind, "task": None, "reason": None}
    with patched(puppets, roster=lambda stale=False: [
            item("pu-a-1", "free", "free (master)"),
            item("pu-a-2", "busy", "busy: feat/7"),
            item("pu-a-3", "hung", "HUNG (not responding)"),
            item("pu-a-4", "free", "free (master)", stale=False),
    ]):
        got = [(i["name"], i["action"]) for i in puppets.diagnose()]
    want = [("pu-a-1", "update"), ("pu-a-2", None), ("pu-a-3", None),
            ("pu-a-3", "restart")]
    c.expect("diagnose over stale specs", got, want)

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
def check_project_ids(c, cases):
    for what, lines, want_o, want_n in cases:
        got_o, got_n = puppets.project_ids(lines)
        c.check(f"project_ids, {what}", not (got_o != want_o or got_n != want_n),
                f"origins {got_o!r} names {got_n!r}")


PROJECT_IDS = [
    ("origin'ы узнаются, имена остаются",
     ["git@h:ermak/mop.git", "https://h/rugent/rugent.git", "backup"],
     {"git@h:ermak/mop.git", "https://h/rugent/rugent.git"}, {"backup"}),
    ("пусто", [], set(), set()),
    ("пустые строки не считаются", ["", "  ", "mop"], set(), {"mop"}),
]


def check_visible(c, cases):
    for what, listing, project, want in cases:
        got = puppets.visible(listing, project)
        got_ids = [j["ID"] for j in got]
        c.check(f"visible, {what}", not (sorted(got_ids) != sorted(want)),
                f"wanted {want}, got {got_ids}")


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


def check_failing(c):
    """HYPOTHESIS (#126): ростер читал только ClientStatus. SOLUTION: сводка
    задачи и причина из stderr. STATUS: FIXED — see #126"""
    t = task_summary(ALLOC, now=1000)
    want = {"state": "pending", "restarts": 4, "exit": 1, "next_s": 1576, "failed": False}
    c.expect("task_summary", t, want)
    # Срок -- оставшийся, а не задержка на момент события: через десять минут
    # «next in 26m» было бы неправдой.
    later = task_summary(ALLOC, now=1000 + 600)
    c.expect("next_s must count down from the event", later["next_s"], 976)
    c.expect("a passed deadline is 0, not negative",
             task_summary(ALLOC, now=1000 + 99999)["next_s"], 0)
    # Причина -- из последней попытки, первая ошибка после строки врапера.
    got = failure_reason(STDERR)
    want = "bootstrap: Could not find or access '~/rugent/.env-prod' on the Ansible Controller."
    c.expect("failure_reason", got, want)
    # Без знакомых строк -- последняя непустая, а пусто -- None.
    c.check("failure_reason must fall back to the last line, and None on empty",
            not (failure_reason("x\nsomething broke\n\n") != "something broke"
                 or failure_reason("") is not None))
    # Строка ростера: падающий -- failing с причиной и сроком, а не pending.
    fn = failing_row
    got = fn("pending", t, "bootstrap: no file") if fn else None
    want = ("failing", "FAILED: bootstrap: no file (4 restarts, next in 26m)")
    c.expect("failing_row", got, want)
    # Работающая задача и задача без падений -- не failing.
    for task in ({"state": "running", "restarts": 4, "exit": 1, "next_s": None, "failed": False},
                 {"state": "pending", "restarts": 0, "exit": None, "next_s": None, "failed": False},
                 None):
        c.check(f"failing_row must be None for {task}",
                not (fn and fn("pending", task, None) is not None))
    # Задача исчерпала попытки -- тоже failing, без срока.
    dead = {"state": "dead", "restarts": 9, "exit": 1, "next_s": None, "failed": True}
    got = fn("failed", dead, "boom") if fn else None
    c.expect("failing_row for a task that gave up", got,
             ("failing", "FAILED: boom (9 restarts, gave up)"))


def check_row_none_274(c):
    """HYPOTHESIS (#274): «здесь ничего» в строке ростера зашито строками при
    сборке -- puppets._row пишет node "-" без аллокации, owner "-", origin
    "?", state "-", -- и решения сравнивают строки (puppet_sizes и gc:
    `r.node != "-"`). Прочерк для человека стал данными для решения.
    SOLUTION: PuppetRow держит None; прочерк и вопрос появляются только при
    показе -- render(), он же to_dict (провод дашборда, страница читает по
    имени). Решения сравнивают с None. STATUS: FIXED — see #274"""
    import ast
    import inspect
    from mop.common import bus, puppets
    from mop.cli.core import list as cli_list
    empty = PuppetRow("pu-x-1", None, "pending", None, None, None, "claude", None)
    old = {"name": "pu-x-1", "node": "-", "alloc_status": "pending", "state": "-",
           "kind": None, "owner": "-", "llm": "claude", "origin": "?", "disk_kb": None}
    render = getattr(empty, "render", None)
    c.expect("#274 None renders as the old strings", render and render(), old)
    c.expect("#274 to_dict is the rendered wire form", empty.to_dict(), old)
    c.expect("#274 mop list line: None as before",
             cli_list.line(empty), cli_list.line(PuppetRow(**{**old})))
    # Сборка: без аллокации, владельца и origin -- None, не строки.
    item = {"job": {"ID": "pu-x-1", "Status": "pending", "Meta": {}}, "alloc": None,
            "error": None, "state": None, "kind": None, "owner": None}
    row = puppets._row(item)
    c.expect("#274 _row holds None for nothing",
             (row.node, row.state, row.owner, row.origin), (None, None, None, None))
    c.expect("#274 _row renders as before", row.to_dict()["node"] + row.to_dict()["state"]
             + row.to_dict()["owner"] + row.to_dict()["origin"], "---?")
    # Решение по None -- то же, что по "-": обмер не спрашивает узел, которого нет.
    asked = []

    def stream(verb, asks, **kw):
        asked.append(dict(asks))
        return iter(())
    keep = bus.request_stream
    try:
        bus.request_stream = stream
        running = PuppetRow("pu-x-2", None, "running", "free", "free", None, "claude", None)
        placed = PuppetRow("pu-x-3", "n1", "running", "free", "free", None, "claude", None)
        puppets.puppet_sizes([running, placed])
    finally:
        bus.request_stream = keep
    c.expect("#274 sizes skip a row without a node", asked,
             [{"pu-x-3": ("n1", {"names": ["pu-x-3"]})}])
    # _row больше не пишет прочерки в строку, решения их не сравнивают.
    tree = ast.parse(inspect.getsource(puppets._row))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "PuppetRow")
    written = sorted(k.arg for k in call.keywords for x in ast.walk(k.value)
                     if isinstance(x, ast.Constant) and x.value in ("-", "?")
                     and k.arg != "alloc_status")
    c.expect("#274 _row writes no sentinel into node/state/owner/origin", written, [])
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    compared = []
    for rel in ("mop/common/puppets.py", "mop/cli/pool/gc.py", "mop/server/web.py"):
        for n, line in enumerate(open(os.path.join(root, rel)), 1):
            if any(f'.{f} {op} "{s}"' in line for f in ("node", "owner", "origin", "state")
                   for op in ("==", "!=") for s in ("-", "?")):
                compared.append(f"{rel}:{n}")
    c.expect("#274 decisions compare with None, not the dash", compared, [])


def main():
    c = Checks()
    for what, given, want in CASES:
        c.expect(what, str(verdict(given)), want)
    for what, given, want in SCREENLESS:
        c.expect(f"#235 {what}", str(verdict(given)), want)
    check_hooked(c, HOOKED)
    check_visible(c, VISIBLE)
    check_project_ids(c, PROJECT_IDS)
    check_failing(c)
    check_treatment(c)
    check_stale_spec(c)
    check_clone_agreement(c)
    check_row_none_274(c)
    check_invariants_273(c)
    for state, want in FREE_CASES:
        c.expect(f"is_free({state!r})", is_free(state and state.kind), want)
    return c.report("state")


# Таблица клонов: (что, факты клона агента, держит ли работу).
CLONE_TABLE = [
    ("clean on the default branch", CLEAN, False),
    ("clean on the master's branch (its home)", WORK, False),
    # #272: дом клона -- ветка мастера или ветка по умолчанию.
    ("clean off its home: waiting for accept", {**WORK, "home": "master"}, True),
    ("clean off its home, old agent (no home key)", OLD_WORK, True),
    ("clean on the default branch, home elsewhere", {**CLEAN, "home": "swarm"}, True),
    ("uncommitted on the default branch", {**CLEAN, "dirty": 2}, True),
    ("unpushed on a branch", {**WORK, "ahead": 1}, True),
    ("both on a branch", {**WORK, "dirty": 1, "ahead": 3}, True),
]


def check_clone_agreement(c):
    """«В клоне работа» -- одно правило на всех потребителей (#266).

    HYPOTHESIS: правило записано трижды: lease.holds_work считал работой и
    ветку не по умолчанию, state._clone_veto и classify_junk -- только
    dirty/ahead. Папет на ветке мастера (#256) ростер звал free, а аренда
    держала его вечно.
    SOLUTION: domain.CloneFacts и domain.holds_work; state, lease,
    classify_junk и cluster.gate решают им.
    STATUS: FIXED — see #266"""
    from mop.common import lease
    from mop.common.domain import CloneFacts, Owner
    from mop.server import cluster
    now = 1_000_000
    stale = Owner("olga", now - lease.WINDOW - 1).to_dict()
    for what, clone, holds in CLONE_TABLE:
        wire = {**clone, "owner": stale}
        got = {
            "state": not is_free(verdict(facts("idle 1 1", wire)).kind),
            "lease": not lease.may_touch(Owner.from_dict(stale), "anton",
                                         CloneFacts.from_dict(wire), now)[0],
            "sweep": not puppets.classify_junk(
                {"n": {"bodies": ["pu-x-1"], "work": {"pu-x-1": wire}}}, set())[0]["sweepable"],
            "gate": cluster.gate("pu-x-1", {"_caller": "anton"}, {"clone": wire}, now)[0] is not None,
        }
        for who, says in got.items():
            c.expect(f"#266 {who} on {what}: holds work", says, holds)



def check_invariants_273(c):
    """HYPOTHESIS (#273): доменные значения заморожены, но без инвариантов:
    CloneFacts(dirty=-1), Body(vmid="abc"), JobMeta(origin="") строятся
    молча, и CloneFacts.known пропускает полусобранный объект -- каждый
    читатель проверяет сам.
    SOLUTION: __post_init__ отказывает ValueError с именем поля, одним
    местом; from_dict терпит провод старого агента (нет ключа -- None), но
    не мусор (не тот тип -- ValueError).
    STATUS: FIXED — see #273"""
    from mop.common.domain import Body, CloneFacts, Gone, JobMeta

    def refused(what, build, field):
        try:
            got = build()
        except ValueError as e:
            c.check(f"#273 {what}: the refusal names {field}", field in str(e), str(e))
            return
        c.fail(f"#273 {what} must be refused", repr(got))

    def fine(what, build):
        try:
            build()
            c.check(f"#273 {what} builds", True)
        except ValueError as e:
            c.fail(f"#273 {what} must build", str(e))

    for what, build, field in [
            ("CloneFacts(dirty=-1)", lambda: CloneFacts(dirty=-1, ahead=0), "dirty"),
            ("CloneFacts(ahead=-2)", lambda: CloneFacts(dirty=0, ahead=-2), "ahead"),
            ("CloneFacts(dirty='3')", lambda: CloneFacts(dirty="3", ahead=0), "dirty"),
            ("CloneFacts(dirty=True)", lambda: CloneFacts(dirty=True, ahead=0), "dirty"),
            ("CloneFacts half-built (dirty only)", lambda: CloneFacts(dirty=1), "ahead"),
            ("CloneFacts half-built (ahead only)", lambda: CloneFacts(ahead=1), "dirty"),
            ("Body(vmid='abc')", lambda: Body("pu-mop-1", vmid="abc"), "vmid"),
            ("Body(vmid=True)", lambda: Body("pu-mop-1", vmid=True), "vmid"),
            ("Body(created='yes')", lambda: Body("pu-mop-1", created="yes"), "created"),
            ("Body(name='nope')", lambda: Body("nope"), "name"),
            ("Body(name='pu-mop-1; id')", lambda: Body("pu-mop-1; id"), "name"),
            ("Gone(target='')", lambda: Gone(""), "target"),
            ("Gone(target=None)", lambda: Gone(None), "target"),
            ("JobMeta(origin='')", lambda: JobMeta("", "claude"), "origin"),
            ("JobMeta(origin=5)", lambda: JobMeta(5, "claude"), "origin"),
            ("JobMeta(llm=7)", lambda: JobMeta("git@h:g/mop.git", 7), "llm"),
            ("CloneFacts.from_dict garbage", lambda: CloneFacts.from_dict({"dirty": "x", "ahead": 0}), "dirty"),
            ("Body.from_dict garbage created", lambda: Body.from_dict({"name": "pu-mop-1", "created": "yes"}), "created"),
            ("Gone.from_dict without target", lambda: Gone.from_dict({"reset": True}), "target")]:
        refused(what, build, field)
    for what, build in [
            ("CloneFacts() (nothing known)", lambda: CloneFacts()),
            ("CloneFacts(dirty=0, ahead=0)", lambda: CloneFacts(dirty=0, ahead=0)),
            ("CloneFacts(dirty=3, ahead=1)", lambda: CloneFacts(dirty=3, ahead=1)),
            ("Body host", lambda: Body("pu-mop-1")),
            ("Body pve", lambda: Body("pu-mop-1", 9003, "10.77.35.59", True)),
            ("Gone host", lambda: Gone("/home/u/puppets/pu-mop-1/target")),
            ("Gone pve", lambda: Gone("body 9003", 9003)),
            ("JobMeta full", lambda: JobMeta("git@h:g/mop.git", "claude", "feat/1", "3")),
            ("JobMeta without origin (a job without meta)", lambda: JobMeta(None, None)),
            ("JobMeta.from_meta({})", lambda: JobMeta.from_meta({})),
            ("Body.from_dict of an old agent (no created)", lambda: Body.from_dict({"name": "pu-mop-1", "body": None}))]:
        fine(what, build)
    old = CloneFacts.from_dict({"cur": "master", "def": "master", "origin": "git@h:g/mop.git",
                                "owner": None})
    c.check("#273 an old agent's clone without dirty/ahead reads, and is not known",
            old is not None and old.known is False, repr(old))
    c.expect("#273 Body.from_dict of an old agent: created is False",
             Body.from_dict({"name": "pu-mop-1", "body": None}).created, False)


if __name__ == "__main__":
    sys.exit(main())
