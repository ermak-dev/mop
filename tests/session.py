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

HYPOTHESIS (#453): дашборд показывает модель папета пусто: сессии claude не
несут модель нигде, что читает ростер (в файле сессии поля нет, транс крипт
молчит), а после #449 выбор модели у папета -- состояние времени выполнения.
Claude Code сам выдаёт текущую модель в JSON на stdin statusline-команды при
старте и примерно раз в 10 с, а успешный переход -- событием PostModelSwitch
(from_model/to_model); отмена и провал события не дают (проверено вживую,
2.1.288: cancel/Esc, validation error, network error).
SOLUTION: `session.py statusline` и PostModelSwitch в `hook` пишут запись
модели по session_id (~/.local/state/mop/models/<session_id>.json -- только
проверенные поля, ничего из сырого JSON), `state <cwd>` отдаёт model.id
только живой сессии каталога. Запаздывающий statusLine не откатывает
переход: у statusLine нет времени наблюдения, «сейчас» стока -- момент
прихода, поэтому событие перехода ставит модель и открывает окно стагнации
(STALE_WINDOW): опрос с прежней моделью внутри окна отбрасывается, с новой
-- подтверждает; вне окна опрос -- правда (модель могла смениться без
события). Вход в проверках настоящий: tests/statusline_hooks.json снят тем
же способом.
STATUS: FIXED — see #453
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
with open(os.path.join(HERE, "statusline_hooks.json")) as f:
    STATUS = json.load(f)
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
        want = {"event": name, "at": NOW, "error": None, "detail": None}
        c.check(f"{name} -> {got!r}, wanted {want!r}", not (got != want))
    # Ход кончился ошибкой API: код и текст, который видел бы человек.
    got = fn(FAIL["StopFailure"], NOW)
    want = {"event": "StopFailure", "at": NOW, "error": "model_not_found",
            "detail": FAIL["StopFailure"]["last_assistant_message"]}
    c.check(f"StopFailure -> {got!r}, wanted {want!r}", not (got != want))

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
        want = {"status": None, "waitingFor": None, "alive": False, "listen": False,
                "turn": None, "model": None}
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




def run_statusline(home, stdin):
    env = dict(os.environ, HOME=home)
    return subprocess.run([sys.executable, SESSION_PY, "statusline"], input=stdin, env=env,
                          capture_output=True, text=True, timeout=30)


def check_model_record(c):
    """Чистая функция: статус-строка и PostModelSwitch -> {model, at} | None.

    Запись ровно этой формы и ничего из сырого JSON: cost/контекст/пути --
    расход и внутренности сессии, ростеру нужное один id."""
    fn = getattr(session, "model_record", None)
    if not c.check("session.model_record(payload, now) is missing", not (fn is None)):
        return
    sl, sw = STATUS["statusline"], STATUS["switch"]
    # Настоящий вход statusLine при старте: id модели.
    got = fn(sl, NOW)
    want = {"model": "claude-opus-5-5", "at": NOW}
    c.check(f"statusline -> {got!r}, wanted {want!r}", not (got != want))
    # Успешный /model: PostModelSwitch отдаёт to_model той же формы.
    got = fn(sw, NOW + 5)
    want = {"model": "glm-5.3[1m]", "at": NOW + 5}
    c.check(f"PostModelSwitch -> {got!r}, wanted {want!r}", not (got != want))
    # Чужие события hook-стока моделью не считаются: PostModelSwitch
    # обрабатывают оба стока, но turn-вид даёт None.
    got = fn(FAIL["StopFailure"], NOW)
    c.check(f"StopFailure is not a model event: {got!r}", not (got is not None))
    # Битый ввод: не наш session_id, нет модели, нет словаря.
    for name, payload in (("no session_id", {"model": {"id": "x"}}),
                          ("bad session_id", dict(sl, session_id="../../etc")),
                          ("no model", {k: v for k, v in sl.items() if k != "model"}),
                          ("model no id", dict(sl, model={"display_name": "X"})),
                          ("empty id", dict(sl, model={"id": ""})),
                          ("id not str", dict(sl, model={"id": 42})),
                          ("huge id", dict(sl, model={"id": "x" * 129})),
                          ("switch no to_model", {k: v for k, v in sw.items()
                                                  if k != "to_model"}),
                          ("not a dict", [1])):
        got = fn(payload, NOW)
        c.check(f"{name} -> None, got {got!r}", not (got is not None))


def check_statusline_cli(c):
    """Сток statusline: молча, всегда 0, запись только валидная и атомарно."""
    home = tempfile.mkdtemp(prefix="mop-test-453-sl-")
    models = os.path.join(home, ".local", "state", "mop", "models")
    sid = STATUS["statusline"]["session_id"]
    path = os.path.join(models, f"{sid}.json")
    # Мусор на входе -- как у hook: ни строчки, код 0 (stdout statusline
    # рендерится в терминал сессии).
    for garbage in ("", "not json", "[1]", '{"session_id": "x"}', "\x00\xff"):
        r = run_statusline(home, garbage)
        c.check(f"statusline on {garbage!r}: exit {r.returncode}, out {r.stdout!r}, "
                f"err {r.stderr!r}",
                not (r.returncode != 0 or r.stdout or r.stderr))
    c.check("garbage must write nothing", not (os.path.exists(models) and os.listdir(models)),
            os.listdir(models) if os.path.exists(models) else [])
    r = run_statusline(home, json.dumps(STATUS["statusline"]))
    try:
        rec = json.load(open(path))
    except (OSError, ValueError) as e:
        c.fail(f"statusline must write {path}: {e}; exit {r.returncode}")
        return
    c.check(f"statusline through the sink: exit {r.returncode}, {r.stdout!r}, {rec!r}",
            not (r.returncode != 0 or r.stdout or rec.get("model") != "claude-opus-5-5"))
    c.check(f"record must carry only model and at: {rec!r}",
            not (set(rec) != {"model", "at"}))
    c.check(f"at must be now in epoch seconds: {rec!r}",
            not (abs(rec.get("at", 0) - time.time()) > 60))
    # Никаких временных файлов рядом и ничего сверх записи.
    left = [f for f in os.listdir(models) if not f.endswith(".json")]
    c.check(f"temporary files left behind: {left}", not (left))
    # Успешный /model тем же стоком не ломает прежний: hook пишет и запись
    # хода, и запись модели.
    r = run_hook(home, json.dumps(STATUS["switch"]))
    try:
        rec = json.load(open(os.path.join(models, f'{STATUS["switch"]["session_id"]}.json')))
    except (OSError, ValueError) as e:
        c.fail(f"PostModelSwitch through hook must write the model record: {e}")
        return
    c.check(f"PostModelSwitch model record: {rec!r}, exit {r.returncode}",
            not (r.returncode != 0 or rec.get("model") != "glm-5.3[1m]"))
    # Провал перехода модели не пишет: события нет -- записи нет.
    os.remove(os.path.join(models, f'{STATUS["switch"]["session_id"]}.json'))
    run_hook(home, json.dumps(FAIL["StopFailure"]))
    c.check("failed/cancelled switch must leave no model record",
            not os.path.exists(os.path.join(models, f'{STATUS["switch"]["session_id"]}.json')))


def check_model_race(c):
    """Гонка: запаздывающий statusLine не откатывает переключение модели.

    По одному session_id писатели двое (statusline периодичен, hook --
    событиен), и порядок их приходов не гарантируется: claude шлёт
    statusLine и на старте, и вслед за переходом. У statusLine нет своего
    времени наблюдения (в его JSON поля нет), «сейчас» стока -- только
    момент прихода, поэтому опрос может оказаться «новее» события, неся
    старую модель. Отсюда правило: успешный переход ставит модель, и опрос
    не вправе вернуть прежнюю -- окно стагнации от события
    (STALE_WINDOW): внутри окна опрос с ДРУГОЙ моделью отбрасывается как
    запоздалый, с той же -- обновляет at. Опрос вне окна -- правда как
    есть: модель могла смениться вовсе без события (ручной /model с «s:
    use this session only»). Сценарии -- каждый в своём каталоге:
    предыдущий сценарий не должен оставлять winning-запись, иначе он же и
    выиграет следующий."""
    sl = STATUS["statusline"]
    # Одна сессия: у снятых входов session_id разных сессий, а гонка -- про
    # писателей одной. sl_new подтверждает модель перехода, sw_back возвращает
    # прежнюю: обе -- та же пара моделей с обеих сторон окна.
    sid = sl["session_id"]
    sw = dict(STATUS["switch"], session_id=sid)
    sl_new = dict(sl, model={"id": "glm-5.3[1m]", "display_name": "glm-5.3[1m]"})
    sw_back = dict(sw, from_model="glm-5.3[1m]", to_model="claude-opus-5-5")

    def race(name, steps, want_model, want_at=None):
        home = tempfile.mkdtemp(prefix="mop-test-453-race-")
        for payload, now in steps:
            session.write_model(payload, now, root=home)
        rec = json.load(open(os.path.join(home, f"{sid}.json")))
        bad = rec.get("model") != want_model or \
            (want_at is not None and rec.get("at") != want_at)
        c.check(f"{name}: {rec!r}, wanted model {want_model!r}"
                + (f" at {want_at!r}" if want_at is not None else ""), not (bad))

    # Форма записи: опрос -- {model, at}, событие помнит прежнюю модель и
    # свой момент (окно стагнации). Проверяется формой множеств, не точным
    # словарём: лишнее поле -- повод для теста, а не для молчаливого пропуска.
    home = tempfile.mkdtemp(prefix="mop-test-453-shape-")
    session.write_model(sl, NOW, root=home)
    rec = json.load(open(os.path.join(home, f"{sid}.json")))
    c.check(f"poll record shape must be {{model, at}}: {rec!r}",
            not (set(rec) != {"model", "at"}))
    session.write_model(sw, NOW + 5, root=home)
    rec = json.load(open(os.path.join(home, f"{sid}.json")))
    c.check(f"event record must add from_model/event_at: {rec!r}",
            not (set(rec) != {"model", "at", "from_model", "event_at"}
                 or rec.get("from_model") != "claude-opus-5-5"
                 or rec.get("event_at") != NOW + 5))
    # switch (t0+5) уже записан, statusline (t0) опоздал.
    race("late statusline must not roll back the switch",
         [(sl, NOW), (sw, NOW + 5), (sl, NOW)], "glm-5.3[1m]", NOW + 5)
    # Отчёт мастера: событие(new) при T, затем опоздавший опрос(old) при том
    # же T -- ручной откат по случайному порядку приходов.
    race("equal at: stale poll must not roll back the switch",
         [(sw, NOW), (sl, NOW)], "glm-5.3[1m]", NOW)
    # То же, но опрос опоздал сильнее: его «сейчас» на секунду позже события,
    # а модель в нём -- прежняя. Откат «новее по времени прихода» -- та же
    # дыра, только шире.
    race("stale poll arriving later (at+1) must not roll back the switch",
         [(sw, NOW), (sl, NOW + 1)], "glm-5.3[1m]", NOW)
    # Подтверждённая статус-строкой новая модель проходит: опрос с ТОЙ же
    # моделью внутри окна обновляет at -- колонка дашборда живая.
    race("statusline confirming the new model refreshes at",
         [(sw, NOW), (sl_new, NOW + 1)], "glm-5.3[1m]", NOW + 1)
    # Новый успешный switch обратно -- событие поверх окна: новое событие
    # всегда право, чья бы модель в нём ни была.
    race("a new switch beats the stale window",
         [(sw, NOW), (sl, NOW + 1), (sw_back, NOW + 2)], "claude-opus-5-5", NOW + 2)
    # Опрос вне окна с другой моделью -- правда: сессия могла уйти с /model
    # вручную (s: session only), и события не было.
    race("poll far after the window is the truth",
         [(sw, NOW), (sl, NOW + 301)], "claude-opus-5-5", NOW + 301)
    # Тот же ответ опроса перезаписи не стоит: запись и так верна.
    race("same poll answer rewrites nothing",
         [(sl, NOW), (sl, NOW)], "claude-opus-5-5", NOW)


def check_model_in_state(c):
    """state <cwd>: model.id только живой сессии каталога, чужие -- мимо."""
    root = tempfile.mkdtemp(prefix="mop-test-453-state-")
    sessions, models, cwd = (os.path.join(root, d) for d in ("sessions", "models", "work"))
    for d in (sessions, models, cwd):
        os.makedirs(d)
    with patched(session, SESSIONS=sessions, MODELS=models):

        def put_session(pid, sid, at, **more):
            with open(os.path.join(sessions, f"{pid}.json"), "w") as f:
                json.dump({"pid": pid, "sessionId": sid, "cwd": cwd, "statusUpdatedAt": at,
                           "messagingSocketPath": os.path.join(root, f"{pid}.sock"),
                           **more}, f)

        def put_model(sid, model, at):
            with open(os.path.join(models, f"{sid}.json"), "w") as f:
                json.dump({"model": model, "at": at}, f)

        # Базовая диагностика умершей сессии не меняется: прежние ключи, те
        # же значения, model -- null без записи.
        put_session(999999, "old-sid", 1000, status="idle")
        put_model("old-sid", "claude-opus-5-5", NOW)
        want = {"status": "idle", "waitingFor": None, "alive": False, "listen": False,
                "turn": None, "model": None}
        got = session.state(cwd)
        c.check(f"dead session: model must be null, diagnostics intact: {got!r}",
                not ({k: got.get(k) for k in want} != want))
        # Живая сессия каталога с записью: model.id отдаётся.
        import socket as _socket
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(os.path.join(root, "live.sock"))
        # Бэклог как у инбокса: каждая проба state() -- это connect без
        # accept, и очередь listen(1) исчерпывается уже второй пробой.
        srv.listen(8)
        try:
            put_session(999998, "live-sid", 2000, status="busy",
                        messagingSocketPath=os.path.join(root, "live.sock"))
            put_model("live-sid", "glm-5.3[1m]", NOW)
            got = session.state(cwd)
            c.check(f"live session must yield its model: {got!r}",
                    not (got.get("model") != "glm-5.3[1m]"))
            # Живая, но записи ещё нет (сессия старее развертывания стоков):
            # null, а не модель мёртвой соседки.
            os.remove(os.path.join(models, "live-sid.json"))
            got = session.state(cwd)
            c.check(f"no record -> null, not the dead one's model: {got!r}",
                    not (got.get("model") is not None))
            # Запись мусора вместо JSON игнорируется молча.
            with open(os.path.join(models, "live-sid.json"), "w") as f:
                f.write("{oops")
            got = session.state(cwd)
            c.check(f"broken record must read as null: {got!r}",
                    not (got.get("model") is not None))
            # CLI: одна строка JSON, model внутри. Сокет жив ровно на время
            # подпроцесса: проверка живости -- коннект, а после srv.close()
            # живых нет и model читался бы null.
            put_model("live-sid", "glm-5.3[1m]", NOW)
            env = dict(os.environ, HOME=root)
            r = subprocess.run([sys.executable, "-c",
                                "import sys; sys.path.insert(0, sys.argv[1]); "
                                "from mop import session as s; "
                                "s.SESSIONS, s.MODELS = sys.argv[2], sys.argv[3]; "
                                "sys.exit(s.main(['state', sys.argv[4]]))",
                                os.path.dirname(HERE), sessions, models, cwd],
                               env=env, capture_output=True, text=True, timeout=30)
            lines = r.stdout.splitlines()
            c.check(f"state CLI must print one JSON line with model: exit {r.returncode}, "
                    f"{r.stdout!r}, {r.stderr!r}",
                    not (r.returncode != 0 or len(lines) != 1
                         or json.loads(lines[0]).get("model") != "glm-5.3[1m]"))
        finally:
            srv.close()


def main():
    c = Checks()
    for fn in (check_turn_record, check_hook_cli, check_state,
               check_model_record, check_statusline_cli, check_model_race,
               check_model_in_state):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("session")


if __name__ == "__main__":
    sys.exit(main())
