#!/usr/bin/env python3
"""Сток хуков claude и запись исхода хода без пула: python3 tests/session.py

HYPOTHESIS (#222): состояние папета наполовину угадывается по экрану tmux
(state._screen_complaint ищет «login expired», «API Error» в 20 строках
пейна). 24.09 в 10:23:56Z pu-mop-2 и pu-mop-3 умерли посреди хода на «Login
expired · Please run /login», а ростер полтора часа читал их как `idle
(uncommitted)`. Claude Code сообщает исход хода структурно -- хуком: вместо
Stop приходит StopFailure с полем error.
SOLUTION: `session.py hook` пишет запись исхода хода по session_id
(~/.local/state/mop/turns/<session_id>.json), `session.py state <cwd>` отдаёт
её вместе с состоянием свежейшей сессии каталога. Вход хуков в проверках --
настоящий: tests/session_hooks.json снят живым `claude -p` (2.1.281).
STATUS: FIXED — see #222
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from mop import session  # noqa: E402

with open(os.path.join(HERE, "session_hooks.json")) as f:
    HOOKS = json.load(f)
OK, FAIL = HOOKS["ok"], HOOKS["fail"]
NOW = 1_790_000_000
SESSION_PY = os.path.join(os.path.dirname(HERE), "mop", "session.py")


def check_turn_record(c):
    """Чистая функция: события хода -> запись ровно этой формы."""
    fn = getattr(session, "turn_record", None)
    if not c.check("session.turn_record(payload, now) is missing", not (fn is None)):
        return
    # Ход начался или кончился как надо -- ошибки нет (снимает прежнюю).
    for name, payload in (("SessionStart", OK["SessionStart"]),
                          ("UserPromptSubmit", OK["UserPromptSubmit"]),
                          ("Stop", OK["Stop"]),
                          ("PostModelSwitch", dict(OK["Stop"], hook_event_name="PostModelSwitch"))):
        got = fn(payload, NOW)
        # Метка кредита (#284): всегда в записи, None без аренды.
        want = {"event": name, "at": NOW, "error": None, "detail": None, "cred": None}
        c.check(f"{name} -> {got!r}, wanted {want!r}", not (got != want))
    # Ход кончился ошибкой API: код и текст, который видел бы человек.
    got = fn(FAIL["StopFailure"], NOW)
    want = {"event": "StopFailure", "at": NOW, "error": "model_not_found",
            "detail": FAIL["StopFailure"]["last_assistant_message"], "cred": None}
    c.check(f"StopFailure -> {got!r}, wanted {want!r}", not (got != want))
    c.expect("the cred marker lands in the record (#284)",
             (fn(FAIL["StopFailure"], NOW, cred="anton") or {}).get("cred"), "anton")
    long = dict(FAIL["StopFailure"], last_assistant_message="x" * 1000)
    c.check("the detail must be cut to 300 characters",
            not (len((fn(long, NOW) or {}).get("detail") or "") != 300))
    # Не наше событие, нет session_id, не объект -- записи нет.
    for what, payload in (("PreToolUse", dict(OK["Stop"], hook_event_name="PreToolUse")),
                          ("Notification", dict(OK["Stop"], hook_event_name="Notification")),
                          ("no session_id", {k: v for k, v in OK["Stop"].items()
                                             if k != "session_id"}),
                          ("a list", [1]), ("None", None),
                          # session_id -- имя файла: путь в нём -- не наш вход.
                          ("a path as session_id", dict(OK["Stop"], session_id="../../x"))):
        c.check(f"{what} must give no record: {fn(payload, NOW)!r}",
                not (fn(payload, NOW) is not None))


def run_hook(home, stdin):
    env = dict(os.environ, HOME=home)
    return subprocess.run([sys.executable, SESSION_PY, "hook"], input=stdin, env=env,
                          capture_output=True, text=True, timeout=30)


def check_hook_cli(c):
    """Сток: молча, всегда 0, запись атомарно; Stop снимает StopFailure."""
    home = tempfile.mkdtemp(prefix="mop-test-222-")
    turns = os.path.join(home, ".local", "state", "mop", "turns")
    sid = FAIL["StopFailure"]["session_id"]
    path = os.path.join(turns, f"{sid}.json")
    # Мусор на входе: ни строчки (stdout UserPromptSubmit уходит в контекст
    # модели) и код 0 (2 на Stop заставил бы claude продолжить ход).
    for garbage in ("", "not json", "[1]", '{"hook_event_name": "Stop"}', "\x00\xff"):
        r = run_hook(home, garbage)
        c.check(f"hook on {garbage!r}: exit {r.returncode}, out {r.stdout!r}, "
                f"err {r.stderr!r}",
                not (r.returncode != 0 or r.stdout or r.stderr))
    c.check("garbage must write nothing", not (os.path.exists(turns) and os.listdir(turns)),
            os.listdir(turns) if os.path.exists(turns) else [])
    r = run_hook(home, json.dumps(FAIL["StopFailure"]))
    try:
        rec = json.load(open(path))
    except (OSError, ValueError) as e:
        c.fail(f"StopFailure must write {path}: {e}; exit {r.returncode}")
        return
    c.check(f"StopFailure through the sink: exit {r.returncode}, {r.stdout!r}, {rec!r}",
            not (r.returncode != 0 or r.stdout or rec.get("error") != "model_not_found"))
    c.check(f"at must be now in epoch seconds: {rec!r}",
            not (abs(rec.get("at", 0) - time.time()) > 60))
    # Следом нормальный ход той же сессии -- ошибка снята.
    stop = dict(OK["Stop"], session_id=sid)
    run_hook(home, json.dumps(stop))
    rec = json.load(open(path))
    c.check(f"Stop after StopFailure must clear the error: {rec!r}",
            not (rec.get("event") != "Stop" or rec.get("error") is not None))
    # Никаких временных файлов рядом: запись атомарна.
    left = [f for f in os.listdir(turns) if not f.endswith(".json")]
    c.check(f"temporary files left behind: {left}", not (left))
    # Записи старше 7 дней снимаются при записи; свежие остаются.
    old, fresh = os.path.join(turns, "old-session.json"), os.path.join(turns, "fresh-session.json")
    for p, age in ((old, 8 * 86400), (fresh, 6 * 86400)):
        with open(p, "w") as f:
            f.write("{}")
        os.utime(p, (time.time() - age, time.time() - age))
    run_hook(home, json.dumps(OK["SessionStart"]))
    c.check(f"records older than 7 days must go, younger stay: {sorted(os.listdir(turns))}",
            not (os.path.exists(old) or not os.path.exists(fresh)))


def check_state(c):
    """state <cwd>: свежайшая сессия каталога и её запись хода, чужие -- мимо."""
    fn = getattr(session, "state", None)
    if not c.check("session.state(cwd) is missing", not (fn is None)):
        return
    root = tempfile.mkdtemp(prefix="mop-test-222-state-")
    sessions, turns, cwd = (os.path.join(root, d) for d in ("sessions", "turns", "work"))
    for d in (sessions, turns, cwd):
        os.makedirs(d)
    with patched(session, SESSIONS=sessions, TURNS=turns):

        def put_session(pid, sid, at, **more):
            with open(os.path.join(sessions, f"{pid}.json"), "w") as f:
                json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "statusUpdatedAt": at,
                           "messagingSocketPath": os.path.join(root, f"{pid}.sock"),
                           **more}, f)

        def put_turn(sid, error):
            with open(os.path.join(turns, f"{sid}.json"), "w") as f:
                json.dump({"event": "StopFailure" if error else "Stop", "at": NOW,
                           "error": error, "detail": None}, f)
        # Пустой каталог: явный пустой ответ, а не исключение.
        got = fn(cwd)
        want = {"status": None, "waitingFor": None, "alive": False, "listen": False, "turn": None}
        c.check(f"no session -> {got!r}, wanted {want!r}", not (got != want))
        # Старая сессия упала на логине, свежая ждёт в диалоге: берётся свежая.
        put_session(999999, "old-sid", 1000, status="idle")
        put_turn("old-sid", "authentication_failed")
        put_session(999998, "new-sid", 2000, status="waiting", waitingFor="dialog open")
        put_turn("new-sid", None)
        # Сессия другого каталога не в счёт, даже самая свежая.
        with open(os.path.join(sessions, "999997.json"), "w") as f:
            json.dump({"pid": 999997, "sessionId": "other", "cwd": root,
                       "statusUpdatedAt": 3000, "status": "busy",
                       "messagingSocketPath": os.path.join(root, "x.sock")}, f)
        got = fn(cwd)
        c.check(f"state must read the freshest session of cwd and its turn: {got!r}",
                not (got.get("status") != "waiting" or got.get("waitingFor") != "dialog open"
                     or (got.get("turn") or {}).get("event") != "Stop"
                     or got.get("alive") is not False or got.get("listen") is not False))
        # Нет записи хода у свежейшей -- turn null, а не запись старой.
        os.remove(os.path.join(turns, "new-sid.json"))
        got = fn(cwd)
        c.check(f"no record for the freshest session must be null, not the old one's: {got!r}",
                not (got.get("turn") is not None))
        # Живость -- как у probe: живой pid, слушающий сокет.
        put_session(os.getpid(), "me", 4000, status="busy")
        got = fn(cwd)
        c.check(f"alive must be probe's pid check: {got!r}",
                not (got.get("alive") is not True or got.get("status") != "busy"))
        # CLI: одна строка JSON.
        env = dict(os.environ, HOME=root)
        r = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]); "
                            "from mop import session as s; "
                            "s.SESSIONS, s.TURNS = sys.argv[2], sys.argv[3]; "
                            "sys.exit(s.main(['state', sys.argv[4]]))",
                            os.path.dirname(HERE), sessions, turns, cwd],
                           env=env, capture_output=True, text=True, timeout=30)
        lines = r.stdout.splitlines()
        c.check(f"state CLI must print one JSON line: exit {r.returncode}, {r.stdout!r}, "
                f"{r.stderr!r}",
                not (r.returncode != 0 or len(lines) != 1
                     or json.loads(lines[0]).get("status") != "busy"))


def main():
    c = Checks()
    for fn in (check_turn_record, check_hook_cli, check_state):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("session")


if __name__ == "__main__":
    sys.exit(main())
