#!/usr/bin/env python3
"""Агент узла без пула: python3 tests/agent.py

Права глаголов агента жили в четырёх параллельных структурах (VERBS,
PUBLIC_VERBS, ADMIN_VERBS, NAMED_VERBS): новый глагол правился в четырёх
местах, и забытое место молча давало или отнимало право. Адресация tmux
`tmux -L <имя> ... -t <имя>` была набрана девять раз (#150).

HYPOTHESIS: права -- четыре структуры, которые обязаны сходиться, и сходятся
только внимательностью.
SOLUTION: одна таблица {глагол: Verb(fn, scope, named)}, прежние наборы
выводятся из неё; решение о праве -- чистая функция. Характеризация: наборы
и решение по каждому глаголу, субъекту и проекту те же, что до правки --
образцы ниже переписаны из агента до #150. Строки tmux -- те же символ в
символ.
STATUS: FIXED — see #150
"""
import dataclasses
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, Msg, bash, canned, restored, GATE_NOW, gate_table_267  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.node import agent  # noqa: E402
from mop.common import busnames  # noqa: E402

# ─── до #150: наборы и логика handle, переписанные дословно ──────────────
OLD_VERBS = ("ping", "local", "state", "states", "sizes", "send", "tail",
             "type", "write", "disk", "wipe", "usage", "junk")
OLD_PUBLIC = ("ping", "local", "state", "states", "send", "tail")
OLD_ADMIN = ("disk", "junk")
OLD_NAMED = ("state", "send", "tail", "type", "wipe")


def old_decision(verb, public, project, name, mine):
    """Отказ, как его давал handle до #150, либо None -- глагол исполняется.
    Правила прежние; таблица -- с добавленными после (ADDED, ниже): они
    мастерские и именующие."""
    verbs, named = OLD_VERBS + ADDED, OLD_NAMED + ADDED
    if verb not in verbs:
        return f"no such verb {verb}; available: {', '.join(sorted(verbs))}"
    if public and verb not in OLD_PUBLIC:
        return f"verb {verb} is available to the master only"
    if verb in OLD_ADMIN and project != busnames.ADMIN:
        return f"verb {verb} is node-level, not given to project {project}"
    if verb in named and not mine:
        return f"puppet {name} is not in project {project}"
    return None


def new_decision(verb, public, project, name, mine):
    """То же через таблицу -- в том порядке, в каком это делает handle."""
    why = agent.refusal(verb, public, project)
    if why is None and agent.VERBS[verb].named and not mine:
        why = agent.foreign(name, project)
    return why


# После #150 -- глаголы, добавленные с тех пор, в конце таблицы: clone --
# факты клона для ворот сервиса кластера (#40), мастерский и именующий.
ADDED = ("clone",)


def check_sets(c):
    """Прежние четыре набора выводятся из таблицы -- те же и в том же порядке;
    добавленные после (ADDED) -- в конце."""
    for got, want, what in ((tuple(agent.VERBS), OLD_VERBS + ADDED, "VERBS"),
                            (agent.PUBLIC_VERBS, OLD_PUBLIC, "PUBLIC_VERBS"),
                            (agent.ADMIN_VERBS, OLD_ADMIN, "ADMIN_VERBS"),
                            (agent.NAMED_VERBS, OLD_NAMED + ADDED, "NAMED_VERBS")):
        c.expect(what, tuple(got), want)


def check_decisions(c):
    """Каждый глагол x субъект (публичный .msg / мастерский .rpc) x проект
    (admin / проект) x папет свой или чужой: тот же ответ, что до #150.
    Отказ, ставший допуском, -- смена прав, а не рефакторинг."""
    for verb in OLD_VERBS + ADDED + ("nosuch", None, ""):
        for public in (True, False):
            for project in (busnames.ADMIN, "mop"):
                for mine in (True, False):
                    want = old_decision(verb, public, project, "pu-mop-1", mine)
                    got = new_decision(verb, public, project, "pu-mop-1", mine)
                    c.expect(f"{verb} public={public} project={project} mine={mine}", got, want)


def check_tmux(c):
    """Места адресации tmux -- те же строки, что до #150. Снимок экрана для
    фактов ушёл вместе с ключом screen (#236)."""
    n = "pu-mop-1"
    t = agent.Tmux(n)
    for got, want in (
            # tmux_alive
            (t.alive(), f"tmux -L {n} has-session -t {n} 2>/dev/null"),
            # pane_lines
            (t.buffer(), f"tmux -L {n} capture-pane -p -t {n} -S -"),
            # v_type: голая клавиша
            (t.press("Escape"), f"tmux -L {n} send-keys -t {n} Escape; "
                                f"sleep 1; tmux -L {n} capture-pane -p -t {n}"),
            # v_type: слэш-команда -- очистка строки, команда, Enter, экран
            (t.type("/model opus"),
             f"tmux -L {n} send-keys -t {n} C-u; sleep 0.3; "
             f"tmux -L {n} send-keys -t {n} '/model opus'; sleep 0.3; "
             f"tmux -L {n} send-keys -t {n} Enter; "
             f"sleep 2; tmux -L {n} capture-pane -p -t {n}"),
            # v_type: пустая команда -- только Enter и экран
            (t.type(""), f"tmux -L {n} send-keys -t {n} Enter; "
                         f"sleep 2; tmux -L {n} capture-pane -p -t {n}")):
        c.expect("tmux", got, want)


def check_quiet(c):
    """Библиотека не печатает и не выходит: программа -- командлет
    `mop agent` (mop/cli/service/agent.py)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "mop", "node", "agent.py")
    in_main = False
    with open(path) as f:
        for n, line in enumerate(f, 1):
            # Переход (#150): блок __main__ -- вход `python3 -m mop.agent` из
            # юнитов узлов; его строки -- программа, а не библиотека.
            if line.startswith('if __name__ == "__main__":'):
                in_main = True
                continue
            if in_main and (line.startswith((" ", "\t")) or not line.strip()):
                continue
            in_main = False
            code = line.split("#")[0]
            if "print(" in code or "sys.exit(" in code:
                c.fail(f"mop/node/agent.py:{n} prints or exits", line.strip())
    c.check("mop/node/agent.py keeps no global connection: pass it explicitly",
            not (hasattr(agent, "_conn")))


# ── write: дом подставляет узел (#279) ───────────────────────────────────
# HYPOTHESIS: v_write сверял присланный абсолютный путь с белым списком под
# своим домом; клиент с чужой установки слал путь со своим MOP_HOME и
# получал отказ. SOLUTION: paths.writable(HOME, path): относительное имя
# из белого списка ложится под дом агента; чужой дом -- отказ как прежде.
# STATUS: FIXED — see #279
def check_write_home_279(c):
    import asyncio
    import base64
    written = []

    async def bodies_apart(_drv):
        return []
    from mop import driver
    with restored(driver, "write_private", "bodies_apart"):
        driver.write_private = lambda path, data: written.append((path, data))
        driver.bodies_apart = bodies_apart
        b64 = base64.b64encode(b"{}").decode()
        got = asyncio.run(agent.v_write(None, {"files": [[".claude/.credentials.json", b64]]}))
        c.expect("write: a relative name lands under the agent's home",
                 written, [(os.path.join(agent.HOME, ".claude/.credentials.json"), b"{}")])
        c.expect("write: the answer names the node's file",
                 got.get("written"), [os.path.join(agent.HOME, ".claude/.credentials.json")])
        written.clear()
        got = asyncio.run(agent.v_write(None, {"files": [["/home/nobody/.claude/.credentials.json", b64]]}))
        c.check("write: another home is refused", "not allowed to write" in (got.get("error") or ""), got)
        c.check("write: nothing written on refusal", written == [])


# ── таймаут шелла -- не успех (#171) ─────────────────────────────────────
# HYPOTHESIS: bsh() на таймауте отдаёт ("", None), а мутирующие глаголы
# агента читали это как успех: запись владельца «прошла», type и Escape
# «напечатаны» и возвращали экран.
# SOLUTION: для записи владельца и type None -- отказ «timed out». Пробы
# только для чтения (буфер пейна, проба сессии) -- как были.
# STATUS: FIXED — see #171
def check_timeouts_171(c):
    import asyncio

    async def facts(name):
        return {"owner": None, "dirty": 0, "ahead": 0}
    with restored(agent, "bsh", "clone_facts"):
        agent.clone_facts = facts
        for code in (None, 0):
            agent.bsh = canned("", code)
            for cmd in ("/status", "Escape"):
                got = asyncio.run(agent.v_type(None, {"name": "pu-mop-1", "command": cmd}))
                if code is None:
                    c.check(f"type {cmd}, timeout", not ("timed out" not in (got.get("error") or "")),
                            repr(got))
                if code == 0:
                    c.check(f"type {cmd}, success", not (got != {"screen": ""}), repr(got))
            refused, undo, _ = asyncio.run(agent._claim("pu-mop-1", {"owner": "m-1"}))
            if code is None:
                c.check("owner write, timeout", not ("timed out" not in (refused or "")),
                        repr(refused))
            if code == 0:
                c.check("owner write, success", not (refused or not undo),
                        f"{refused!r} {undo!r}")
        # Кривое имя до шелла не доходит: bsh отдал бы None, и это прочиталось
        # бы как таймаут, а то и как успех.
        got = asyncio.run(agent.v_type(None, {"name": "pu-x; rm", "command": "/status"}))
        c.check("type with a bad name", not ("doesn't look like" not in (got.get("error") or "")),
                repr(got))
        # Пробы только для чтения: ответ прежний.
        agent.bsh = canned("", None)
        c.check("pane_lines must tolerate a timeout",
                not (asyncio.run(agent.pane_lines("pu-mop-1")) != []))
        c.check("session_probe must read a timeout as none",
                not (asyncio.run(agent.session_probe("pu-mop-1")) != "none"))


# ── откат владельца -- с результатом (#181) ──────────────────────────────
# HYPOTHESIS: _unclaim пишет прежнего владельца назад (или rm -f) и код
# шелла не смотрит: на таймауте (#171) или ошибке аренда остаётся за
# мастером, чей send не доехал, и следующему мастеру send отказывает,
# называя не того владельца -- а отказ первого об этом молчит.
# SOLUTION: _unclaim отдаёт причину неудачи (driver.why, как в #171), и
# отказ v_send дописывает «owner not restored: <причина>»; удачный откат
# текст не меняет.
# STATUS: FIXED — see #181
def check_unclaim_181(c):
    import asyncio
    delivery = {"error": "pu-mop-1: no live session"}

    def fake(rollback):
        calls = []

        async def bsh(name, script, timeout=20):
            calls.append(script)
            # Первый шелл -- запись аренды, второй -- откат.
            return ("", 0) if len(calls) == 1 else rollback
        return bsh, calls

    async def facts(name):
        return {"owner": None, "dirty": 0, "ahead": 0}

    async def session_json(name, cmd, timeout=20):
        return dict(delivery)
    with restored(agent, "bsh", "clone_facts", "session_json"):
        agent.clone_facts, agent.session_json = facts, session_json
        for rollback, want in ((("", None), delivery["error"] + "; owner not restored: timed out"),
                               (("rm: Permission denied", 1),
                                delivery["error"] + "; owner not restored: rm: Permission denied"),
                               (("", 0), delivery["error"])):
            agent.bsh, calls = fake(rollback)
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "owner": "m-1",
                                                  "message": "hi"}))
            c.check(f"send refused, rollback {rollback}: a rollback shell",
                    not (len(calls) != 2 or "rm -f" not in calls[-1]), calls)
            c.expect(f"send refused, rollback {rollback}: error", got.get("error"), want)


def check_intake(c):
    """HYPOTHESIS (#168): тело-JSON не объект (`[1]`, `"x"`) роняет задачу
    handle на req["_project"], а нехэшируемый глагол (`{"verb": [1]}`) -- на
    поиске в таблице внутри refusal(): ответа нет, проситель ждёт таймаут.
    SOLUTION: handle отказывает телу-не-объекту до таблицы; refusal читает
    нестроковый глагол как неизвестный -- тот же отказ, что у неизвестной
    строки.
    STATUS: FIXED — see #168"""
    import asyncio
    import json
    called = []

    def spy(verb):
        async def fn(conn, req):
            called.append(verb)
            return {"verb": verb}
        return fn

    keep = dict(agent.VERBS)
    try:
        for v, d in keep.items():
            agent.VERBS[v] = dataclasses.replace(d, fn=spy(v))

        def ask(body, subject="mop.admin.node.hyper.rpc"):
            called.clear()
            msg = Msg(subject, body)
            asyncio.run(agent.handle(None, msg, public=subject.endswith(".msg")))
            return msg.replies

        for body in (b"[1]", b'"x"', b"7", b"null"):
            got = ask(body)
            c.check(f"body {body!r}: refused before any verb",
                    not (got != [{"error": "request is not a JSON object"}] or called),
                    f"replies {got!r}, verbs called {called}")

        def unknown(v):
            return f"no such verb {v}; available: {', '.join(sorted(keep))}"
        for verb in ([1], {}, 7, True):
            got = ask(json.dumps({"verb": verb}).encode())
            c.check(f"verb {verb!r}: the unknown-verb refusal",
                    not (got != [{"error": unknown(verb)}] or called),
                    f"replies {got!r}, verbs called {called}")
        # Прежнее не меняется: не-JSON, неизвестная строка, обычный запрос.
        c.expect("a non-JSON body must keep its old refusal",
                 ask(b"junk"), [{"error": "request is not JSON"}])
        c.expect("an unknown string verb must keep its refusal",
                 ask(b'{"verb": "nosuch"}'), [{"error": unknown("nosuch")}])
        got = ask(b'{"verb": "ping"}', "mop.mop.node.hyper.msg")
        c.check("a well-formed ping must reach its verb",
                not (got != [{"verb": "ping"}] or called != ["ping"]), f"{got!r}, {called}")
    finally:
        agent.VERBS.clear()
        agent.VERBS.update(keep)


def check_main_169(c):
    """HYPOTHESIS (#169): как только bus при импорте без nats-py бросает, а не
    выходит, переходный вход юнитов `python3 -m mop.agent` печатал бы трассу:
    импорты модуля идут раньше его блока __main__.
    SOLUTION: блок __main__ -- до импортов пакета; командлет проверяет nats
    первым и отказывает одной строкой.
    STATUS: FIXED — see #169"""
    import subprocess
    import tempfile
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    shadow = tempfile.mkdtemp(prefix="mop-test-nonats-")
    os.makedirs(os.path.join(shadow, "nats"))
    with open(os.path.join(shadow, "nats", "__init__.py"), "w") as f:
        f.write("raise ImportError('no nats')\n")
    r = subprocess.run([sys.executable, "-m", "mop.node.agent", "--check"], cwd=root,
                       env=dict(os.environ, PYTHONPATH=shadow),
                       capture_output=True, text=True)
    want = "bus library needed: pip install --user --break-system-packages nats-py\n"
    c.check("python3 -m mop.node.agent without nats refuses in one line",
            not (r.returncode != 1 or r.stdout or r.stderr != want),
            f"code {r.returncode}, stdout {r.stdout!r}, stderr {r.stderr[-300:]!r}")


def check_subject_173(c):
    """HYPOTHESIS (#173): агент брал проект из субъекта своим правилом
    (`parts[1] if len(parts) > 1`), а не service.project_from_subject (#149):
    на его субъектах они сходятся, но это второе определение.
    SOLUTION: агент зовёт общую функцию; на его субъектах ответ тот же.
    STATUS: FIXED — see #173"""
    from mop.common import service

    def old(subject):
        parts = subject.split(".")
        return parts[1] if len(parts) > 1 else ""
    for p in ("mop", busnames.ADMIN, "a-b_c"):
        subs = [busnames.node(p, "hyper", "rpc"), busnames.node(p, "hyper", "msg"),
                busnames.broadcast(p)]
        for subj in subs:
            c.check(f"{subj}: the shared project rule",
                    not (service.project_from_subject(subj) != old(subj) or old(subj) != p),
                    f"shared {service.project_from_subject(subj)!r}, "
                    f"old {old(subj)!r}, wanted {p!r}")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "mop", "node", "agent.py")
    text = open(path).read()
    c.check("mop/node/agent.py must take the project via service.project_from_subject",
            not ("parts[1]" in text or "service.project_from_subject(" not in text))


def check_unclaim_race_189(c):
    """HYPOTHESIS (#189): v_send ставит аренду под _owner_locks, а откат
    неудачной доставки делает ВНЕ замка и не глядя: второй мастер (force)
    успевает взять папета, пока первый ждёт доставки, и откат первого
    перетирает его запись прежним владельцем или стирает файл.
    SOLUTION: откат -- под тем же замком и только если в файле всё ещё наша
    запись. STATUS: FIXED — see #189

    Шелл настоящий: bsh исполняет скрипт агента bash'ем над временным
    клоном, так что сравнение-и-запись проверяется как есть, а не заглушкой."""
    import asyncio
    import tempfile
    from mop.common import lease
    from mop.common.domain import Owner
    root = tempfile.mkdtemp(prefix="mop-test-189-")
    os.makedirs(os.path.join(root, ".git"))
    path = os.path.join(root, lease.FILE)

    async def facts(name):
        text = open(path).read() if os.path.exists(path) else ""
        owner = Owner.parse(text)
        return {"owner": owner and owner.to_dict(), "dirty": 0, "ahead": 0,
                "cur": "master", "def": "master"}

    async def no_event(*a, **k):
        return None

    def owner_now():
        return Owner.parse(open(path).read()) if os.path.exists(path) else None
    with restored(agent, "bsh", "clone_facts", "clone_dir", "session_json", "_event"):
        agent.bsh, agent.clone_facts, agent._event = bash, facts, no_event
        agent.clone_dir = lambda name: root

        # Гонка: A взял аренду и ждёт доставки; B берёт с force и доставляет;
        # доставка A падает, и A откатывает.
        async def race():
            b_done = asyncio.Event()

            async def deliver(name, cmd, timeout=20):
                if "--from-name a " in cmd:
                    await b_done.wait()
                    return {"error": "not delivered"}
                return {"msg_id": "m-b"}
            agent.session_json = deliver

            async def a():
                return await agent.v_send(None, {"name": "pu-mop-1", "message": "x",
                                                 "owner": "alice", "from_name": "a"})

            async def b():
                while owner_now() is None:
                    await asyncio.sleep(0.01)
                got = await agent.v_send(None, {"name": "pu-mop-1", "message": "y",
                                                "owner": "bob", "from_name": "b",
                                                "force": True})
                b_done.set()
                return got
            return await asyncio.gather(a(), b())
        ra, rb = asyncio.run(race())
        c.check("race: a must fail, b must deliver",
                not (not ra.get("error") or rb.get("error")), f"a {ra!r}, b {rb!r}")
        c.check("race: the lease must stay with bob",
                not (getattr(owner_now(), "user", None) != "bob"), repr(owner_now()))

        # Одна неудачная доставка -- откат как прежде: к прежнему владельцу,
        # а без него файл снимается.
        async def undelivered(name, cmd, timeout=20):
            return {"error": "not delivered"}
        agent.session_json = undelivered
        stale = Owner("carol", 1000).render()
        for before, want in ((None, None), (stale, stale)):
            if before is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                open(path, "w").write(before)
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "message": "z",
                                                  "owner": "dave", "from_name": "d"}))
            now = open(path).read() if os.path.exists(path) else None
            c.check(f"single failed send over {before!r}",
                    not (not got.get("error") or now != want),
                    f"{got!r}, file {now!r}, wanted {want!r}")


def check_gates_40(c):
    """HYPOTHESIS (#40): владельца сверяет только send; type (slash) и wipe
    пускают любого мастера проекта к папету, которого ведёт другой.
    SOLUTION: те же ворота (lease.may_touch) под тем же замком на папета,
    что у send; оператор (субъект admin) проходит всегда, force -- называя,
    у кого. Глагол clone отдаёт факты клона и при мёртвой сессии: по ним
    решает сервис кластера.
    STATUS: FIXED — see #40"""
    import asyncio
    import time
    from mop.common.domain import Owner
    olga = Owner("olga", int(time.time()) - 60).to_dict()
    clone = {"cur": "bug/1-x", "def": "master", "dirty": 2, "ahead": 0, "owner": olga}
    shelled, destroyed = [], []

    async def facts(name):
        return dict(clone)

    async def dead(name):
        return False

    async def no_event(*a, **k):
        return None

    class Driver:
        async def destroy(self, name, branch=None):
            destroyed.append(name if branch is None else (name, branch))
            return {"target": "gone"}
    with restored(agent, "bsh", "clone_facts", "tmux_alive", "DRIVER", "_event"):
        agent.bsh, agent.clone_facts, agent.tmux_alive = canned(calls=shelled), facts, dead
        agent.DRIVER, agent._event = Driver(), no_event
        base = {"name": "pu-mop-1", "_project": "mop"}
        # #257: ветка мастера едет глаголом wipe до драйвера. STATUS: FIXED — see #257
        destroyed.clear()
        got = asyncio.run(agent.v_wipe(None, {**base, "owner": "olga", "branch": "swarm"}))
        c.check("#257 wipe must hand the branch to the driver",
                not (got.get("error") or destroyed != [("pu-mop-1", "swarm")]),
                f"{got} {destroyed}")
        for verb, fn, extra, touched in (("type", agent.v_type, {"command": "/status"}, shelled),
                                         ("wipe", agent.v_wipe, {}, destroyed)):
            for who, more, allowed in (("another master", {"owner": "anton"}, False),
                                       ("anonymous", {}, False),
                                       ("the owner", {"owner": "olga"}, True),
                                       ("force", {"owner": "anton", "force": True}, True),
                                       ("the operator", {"owner": "anton", "_project": "admin"}, True)):
                touched.clear()
                got = asyncio.run(fn(None, {**base, **extra, **more}))
                if allowed:
                    c.check(f"{verb} by {who} must pass",
                            not (got.get("error") or not touched), repr(got))
                if not allowed:
                    c.check(f"{verb} by {who} must be refused naming olga, touching nothing",
                            not ("olga" not in (got.get("error") or "") or touched),
                            f"{got!r}, {touched}")
                if who == "force":
                    c.check(f"{verb} with force must name whom",
                            not ("olga" not in (got.get("owner_note") or "")), repr(got))
        # Ничей -- как до #40: запрос без owner проходит.
        clone["owner"] = None
        shelled.clear()
        got = asyncio.run(agent.v_type(None, {**base, "command": "/status"}))
        c.check("type on nobody's puppet must pass as before",
                not (got.get("error") or not shelled), repr(got))
        fn = getattr(agent, "v_clone", None)
        if c.check("agent.v_clone exists", not (fn is None)):
            got = asyncio.run(fn(None, dict(base)))
            c.expect("clone must return the clone's facts", got, {"clone": clone})


def check_caller_207(c):
    """HYPOTHESIS (#207): агент видит из субъекта только проект, а кто
    просит -- поле owner в теле, которое пишет сам отправитель; ворота (#40)
    и аренда send (#161) стоят на названном, а не на проверенном.
    SOLUTION: логин человека -- токеном субъекта (mop.<p>.node.<n>.rpc.<логин>),
    публиковать туда NATS даёт только ему (natsconf, операторы); агент берёт
    вызывающего из субъекта и тело в этом не участвует. Переход: прежний rpc
    без логина принимается, и логин там -- названный телом (self-declared)
    до уборки; публичный msg (папеты) владельца не несёт вовсе.
    STATUS: FIXED — see #207"""
    import asyncio
    from mop.common import bus, lease
    got = {s: busnames.caller(s) for s in (
        "mop.mop.node.hyper.rpc.alice", "mop.mop.node.hyper.rpc",
        "mop.mop.node.hyper.msg", "mop.mop.cluster.rpc.alice", "mop.mop.cluster.rpc",
        "mop.mop.node.hyper.msg.alice", "mop.mop.master.h-1.inbox",
        "mop.mop.node.hyper.rpc.anton%2Eermak", "mop.mop.cluster.rpc.anton%2Eermak",
        "mop.mop.node.hyper.rpc.a%2eb", "mop.mop.node.hyper.rpc.*")}
    want = {"mop.mop.node.hyper.rpc.alice": "alice", "mop.mop.node.hyper.rpc": None,
            "mop.mop.node.hyper.msg": None, "mop.mop.cluster.rpc.alice": "alice",
            "mop.mop.cluster.rpc": None, "mop.mop.node.hyper.msg.alice": None,
            "mop.mop.master.h-1.inbox": None,
            "mop.mop.node.hyper.rpc.anton%2Eermak": "anton.ermak",
            "mop.mop.cluster.rpc.anton%2Eermak": "anton.ermak",
            "mop.mop.node.hyper.rpc.a%2eb": None, "mop.mop.node.hyper.rpc.*": None}
    c.expect("busnames.caller", got, want)
    # Логин -- любой, кроме пустого и управляющих символов: в LDAP/AD
    # anton.ermak -- норма (#208). В токен субъекта он кодируется обратимо.
    for bad in ("", None, "a\tb", "a\nb", "a\x00b", "a\x7fb"):
        c.check(f"{bad!r} must not be a login", not (busnames.valid_login(bad)))
    for fine in ("anton", "ivan_p-2", "anton.ermak", "a b", "a*", "a>", "a%b", "Антон"):
        c.check(f"{fine!r} must be a login", not (not busnames.valid_login(fine)))
    for login, token in (("anton", "anton"), ("anton.ermak", "anton%2Eermak"),
                         ("a b", "a%20b"), ("a*>", "a%2A%3E"), ("50%", "50%25"),
                         ("Антон", "Антон"), ("a b", "a%C2%A0b")):
        c.expect(f"login_token({login!r})", busnames.login_token(login), token)
        c.expect(f"login_of({token!r})", busnames.login_of(token), login)
        c.check(f"token {token!r} is a single literal NATS token",
                not (any(ch in token for ch in ".*> ") or any(ch.isspace() for ch in token)))
    # Инъективно: «a.b» и буквальное «a%2Eb» -- разные токены.
    c.check("two logins must never share a token: a.b vs a%2Eb",
            not (busnames.login_token("a.b") == busnames.login_token("a%2Eb")))
    # Неканоничный токен -- не логин: иначе один логин читался бы из двух.
    for token in ("a%2eb", "a%41", "a%", "a%zz", "*"):
        c.check(f"login_of({token!r}) must be None: not a canonical token",
                not (busnames.login_of(token) is not None))
    subs = busnames.agent_subscriptions("hyper")
    c.check("the agent must listen on both the login and the old rpc",
            not ("mop.*.node.hyper.rpc.*" not in subs["rpc"]
                 or "mop.*.node.hyper.rpc" not in subs["rpc"]), subs)

    # Разбор в handle: глагол-заглушка отдаёт то, что видят ворота.
    seen = []

    async def probe(_conn, req):
        seen.append(lease.caller(req))
        return {"ok": True}

    async def mine(name):
        return "mop"
    keep = dict(agent.VERBS)
    with restored(agent, "puppet_project"):
        try:
            agent.VERBS["send"] = dataclasses.replace(agent.VERBS["send"], fn=probe)
            agent.puppet_project = mine
            body = {"verb": "send", "name": "pu-mop-1", "owner": "bob", "_caller": "bob"}
            for subj, public, want in (
                    ("mop.mop.node.hyper.rpc.alice", False, ("alice", True)),
                    ("mop.mop.node.hyper.rpc", False, ("bob", False)),
                    ("mop.mop.node.hyper.msg", True, (None, False))):
                seen.clear()
                asyncio.run(agent.handle(None, Msg(subj, body=body), public))
                c.expect(f"caller over {subj} with a forged body", seen, [want])
        finally:
            agent.VERBS.clear()
            agent.VERBS.update(keep)

    # Клиент: человек спрашивает по субъекту со своим логином, машина -- без.
    with restored(bus, "login"):
        bus.login = lambda: "alice"
        c.expect("a human's rpc subject must carry the login",
                 bus.subject("hyper", project="mop"), "mop.mop.node.hyper.rpc.alice")
        c.check("msg carries no login",
                not (bus.subject("hyper", "msg", project="mop") != "mop.mop.node.hyper.msg"))
        c.expect("a human's cluster subject must carry the login",
                 bus.cluster_subject("mop"), "mop.mop.cluster.rpc.alice")
        bus.login = lambda: "anton.ermak"
        c.check("a dotted login must travel encoded",
                not (bus.subject("hyper", project="mop") != "mop.mop.node.hyper.rpc.anton%2Eermak"
                     or bus.cluster_subject("mop") != "mop.mop.cluster.rpc.anton%2Eermak"))
        bus.login = lambda: None
        c.check("a machine asks without a login token",
                not (bus.subject("hyper", project="mop") != "mop.mop.node.hyper.rpc"
                     or bus.cluster_subject("mop") != "mop.mop.cluster.rpc"))
    c.check("without_caller must strip exactly the login token (the fallback to "
            "an agent from before #207)",
            not (busnames.without_caller("mop.mop.node.hyper.rpc.alice") != "mop.mop.node.hyper.rpc"
                 or busnames.without_caller("mop.mop.cluster.rpc.alice") != "mop.mop.cluster.rpc"
                 or busnames.without_caller("mop.mop.events") != "mop.mop.events"))


def check_git_identity_167(c):
    """HYPOTHESIS (#167): в клоне папета нет user.name/user.email, и пул
    коммитит разовым `git -c`, который назвал мастер; оператор решил, что
    автор коммита -- человек, прошедший проверку на шине.
    SOLUTION: записав ПРОВЕРЕННОГО владельца (логин из субъекта, #207) после
    доставки, агент спрашивает профиль у сервиса сервера (глагол identity,
    server.rpc) и ставит repo-local user.name/user.email в клон. Правило одно:
    после проверенной аренды в клоне identity нового владельца или никакой --
    профиля нет или сервер не ответил, значит identity снята, а не оставлена
    прежнему (иначе коммиты нового владельца шли бы под чужим именем), и
    заметка говорит мастеру коммитить с -c. Названный телом владелец (прежний
    субъект) identity не трогает; неудачная доставка -- тоже: аренда
    откатывается, и identity остаётся той, что была.
    STATUS: FIXED — see #167

    Шелл и git настоящие: bsh исполняет скрипт агента bash'ем над временным
    клоном, identity читается `git config --local`."""
    import asyncio
    import json
    import subprocess
    import tempfile
    from mop.common import lease
    from mop.common.domain import Owner
    profiles = {"olga": {"name": "Ольга Петрова", "email": "olga@example.dev"},
                "petr": {"name": "Pyotr O'Neil", "email": "petr@example.dev"}}
    asked = []

    class Reply:
        def __init__(self, body):
            self.data = json.dumps(body).encode()

    class Conn:
        async def request(self, subject, data, timeout=None):
            req = json.loads(data.decode())
            asked.append((subject, req))
            if req.get("login") == "down":
                raise asyncio.TimeoutError()
            return Reply({"login": req["login"], **profiles.get(req["login"], {})})

    root = tempfile.mkdtemp(prefix="mop-test-167-")

    def clone():
        """Свежий клон: git init, без identity."""
        d = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", d], check=True)
        return d

    state = {}

    async def facts(name):
        path = os.path.join(state["clone"], lease.FILE)
        owner = Owner.parse(open(path).read()) if os.path.exists(path) else None
        return {"owner": owner and owner.to_dict(), "dirty": 1, "ahead": 0,
                "cur": "feat/1", "def": "master"}

    async def delivered(name, cmd, timeout=20):
        return {"error": "not delivered"} if state.get("fail") else {"msg_id": "m-1"}

    async def no_event(*a, **k):
        return None

    async def project(name):
        return "mop"

    def ident():
        got = {}
        for key in ("name", "email"):
            r = subprocess.run(["git", "-C", state["clone"], "config", "--local", f"user.{key}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                got[key] = r.stdout.strip()
        return got

    def send(caller=None, owner=None, force=False):
        req = {"name": "pu-mop-1", "message": "x", "from_name": "m"}
        if caller:
            req["_caller"] = caller
        if owner:
            req["owner"] = owner
        if force:
            req["force"] = True
        return asyncio.run(agent.v_send(Conn(), req))

    with restored(agent, "bsh", "clone_facts", "clone_dir", "session_json",
                  "_event", "puppet_project"):
        agent.bsh, agent.clone_facts, agent.session_json = bash, facts, delivered
        agent._event, agent.puppet_project = no_event, project
        agent.clone_dir = lambda name: state["clone"]

        # Проверенный владелец с профилем -- identity в клоне; спросили
        # сервер своего проекта его логином.
        state["clone"] = clone()
        got = send(caller="olga")
        c.expect("verified owner: identity", ident(), profiles["olga"])
        c.expect("verified owner: the server's identity verb asked",
                 asked, [("mop.mop.server.rpc", {"verb": "identity", "login": "olga"})])
        c.check("verified owner: the reply must name the identity",
                not ("olga@example.dev" not in (got.get("owner_note") or "")), got)

        # Названный телом (прежний субъект) -- identity не трогаем, сервер
        # не спрашиваем.
        state["clone"], asked[:] = clone(), []
        got = send(owner="bob")
        c.check("self-declared: identity untouched, server not asked",
                not (ident() or asked or got.get("error")),
                f"identity {ident()}, asked {asked}, reply {got}")

        # Профиля нет -- identity не ставится, заметка велит -c.
        state["clone"] = clone()
        got = send(caller="ivan")
        note = got.get("owner_note") or ""
        c.check("no profile: no identity, the note says git -c",
                not (ident() or got.get("error") or "ivan" not in note or "git -c" not in note),
                f"identity {ident()}, reply {got}")

        # Сервер не ответил -- доставка не страдает, identity нет, заметка.
        state["clone"] = clone()
        got = send(caller="down")
        c.check("server silent: no identity, the note says git -c",
                not (ident() or got.get("error") or "git -c" not in (got.get("owner_note") or "")),
                f"identity {ident()}, reply {got}")

        # force: аренда olga (работа в клоне) переходит к petr -- и identity.
        state["clone"] = clone()
        send(caller="olga")
        got = send(caller="petr", force=True)
        c.check("force: identity moves to petr",
                not (ident() != profiles["petr"]),
                f"identity {ident()}, wanted {profiles['petr']}; reply {got}")
        # ...к ivan без профиля -- identity olga'и снята, не оставлена ему.
        send(caller="olga", force=True)
        got = send(caller="ivan", force=True)
        c.check("force to a login without a profile: the old identity is removed",
                not (ident()), f"stays {ident()}")
        # Названный телом с force -- identity не трогает.
        send(caller="olga", force=True)
        send(owner="bob", force=True)
        c.expect("self-declared force: identity must stay", ident(), profiles["olga"])

        # Доставка не удалась -- аренда откатывается, identity прежняя.
        state["clone"] = clone()
        send(caller="olga")
        state["fail"] = True
        got = send(caller="petr", force=True)
        state["fail"] = False
        c.check("failed delivery: identity stays",
                not (not got.get("error") or ident() != profiles["olga"]),
                f"identity {ident()}, reply {got}")


# ── факт state: исход хода едет мастеру (#224) ───────────────────────────
# HYPOTHESIS: исход хода (StopFailure и код ошибки) пишут хуки в запись,
# которую отдаёт `session.py state` (#222), а агент звал только probe и
# отдавал мастеру строку "<status> <alive> <listen>" — записи хода мастер не
# видел, и провал хода посреди работы читался «idle».
# SOLUTION: facts зовёт state (один процесс вместо probe) и шлёт разобранный
# словарь новым ключом state, а строку session собирает из него же — мастер со
# старой библиотекой читает только session. Старый session.py на узле (state
# не отвечает) — откат на probe ровно как раньше, без ключа state.
# STATUS: FIXED — see #224
def check_state_fact_224(c):
    import asyncio
    import json
    rec = {"status": "idle", "waitingFor": None, "alive": True, "listen": False,
           "turn": {"event": "StopFailure", "at": 1790245436,
                    "error": "authentication_failed", "detail": "Login expired"}}
    none = {"status": None, "waitingFor": None, "alive": False, "listen": False, "turn": None}
    calls = []

    # Своя заглушка: отвечает по глаголу session.py, а не одним ответом.
    def fake(answers):
        async def bsh(name, script, timeout=20):
            verb = script.split()[2]
            calls.append(verb)
            return answers.get(verb, ("", 1))
        return bsh

    async def alive(name):
        return True

    async def clone(name):
        return {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
    with restored(agent, "bsh", "clone_facts", "tmux_alive"):
        agent.clone_facts, agent.tmux_alive = clone, alive
        for what, answers, want_session, want_state, want_calls in (
                ("new session.py", {"state": ("banner\n" + json.dumps(rec), 0)},
                 "idle 1 0", rec, ["state"]),
                ("no session", {"state": (json.dumps(none), 0)}, "none", none, ["state"]),
                ("old session.py: state is an unknown verb",
                 {"state": ("usage: session.py ...", 2), "probe": ("busy 1 1", 0)},
                 "busy 1 1", None, ["state", "probe"]),
                # Таймаут -- тело не ответило, и probe ждал бы те же 20 секунд
                # впустую: ответ "none", как у таймаута probe до #224.
                ("state timed out", {"state": ("", None)}, "none", None, ["state"])):
            calls.clear()
            agent.bsh = fake(answers)
            got = asyncio.run(agent.facts("pu-mop-1"))
            c.check(f"facts, {what}",
                    not (got.get("session") != want_session or got.get("state") != want_state
                         or ("state" in got) != (want_state is not None) or calls != want_calls),
                    f"{got!r}, calls {calls}")


# ── факты без экрана (#236) ──────────────────────────────────────────────
# HYPOTHESIS: после #235 вердикт facts["screen"] не читает, а агент всё равно
# снимает пейн (capture-pane) у каждого папета на каждом опросе ростера — в
# теле pve это лишний заход в тело и 20 строк по шине. Держали его на переход
# для мастеров со старой библиотекой; все они теперь не старше b0aca2c.
# SOLUTION: facts не снимает экран и не шлёт ключ screen; вердикт по тем же
# фактам прежний. tail/slash/attach снимают пейн своими глаголами.
# STATUS: FIXED — see #236
def check_no_screen_fact_236(c):
    import asyncio
    import json
    from mop.common.state import verdict
    scripts = []
    rec = {"status": "idle", "waitingFor": None, "alive": True, "listen": True,
           "turn": {"event": "Stop", "at": 1790245436, "error": None, "detail": None}}

    async def bsh(name, script, timeout=20):
        scripts.append(script)
        if "has-session" in script:
            return "", 0
        if "capture-pane" in script:
            return "Herding bytes", 0
        return json.dumps(rec), 0

    async def clone(name):
        return {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
    with restored(agent, "bsh", "clone_facts"):
        agent.bsh, agent.clone_facts = bsh, clone
        got = asyncio.run(agent.facts("pu-mop-1"))
        c.check("facts must carry no screen key", not ("screen" in got), repr(got))
        c.check("facts must not capture the pane",
                not (any("capture-pane" in s for s in scripts)), repr(scripts))
        with_screen = {**got, "screen": "Not logged in · Run /login"}
        c.check("verdict must not change",
                not (str(verdict(got)) != "free (master)"
                     or str(verdict(with_screen)) != str(verdict(got))),
                f"{verdict(got)} vs {verdict(with_screen)}")


# ── расход по логину в глаголе usage (#244) ───────────────────────────────
# HYPOTHESIS: глагол usage отдаёт расход по папету и дню, но не по логину
# мастера — оператор не видит, кто сколько тратит.
# SOLUTION: usage.py зовётся с --by-login, агент кладёт рядом с прежним полем
# usage новое by_login: {папет: {логин: {дата: {вид: n}}}}; usage не
# меняется. Старый usage.py в теле флага не знает и печатает прежнюю форму —
# тогда весь расход папета за «-»: инвариант (сумма по логинам == usage)
# держится, а приписать его некому.
# STATUS: FIXED — see #244
def check_usage_by_login_244(c):
    import asyncio
    import json
    row = {"input": 1, "output": 2, "cache_write": 3, "cache_read": 4}
    new = {"usage": {"2026-09-24": row},
           "by_login": {"anton": {"2026-09-24": row}}}
    answers = {"pu-mop-1": (json.dumps(new), 0),
               "pu-mop-2": (json.dumps({"2026-09-24": row}), 0),   # старый usage.py
               "pu-mop-3": ("boom", 1)}
    scripts = []

    async def bsh(name, script, timeout=20):
        scripts.append(script)
        return answers[name]

    class Driver:
        SESSION_PY = "/x/mop/session.py"

        async def bodies(self):
            return list(answers)

        def projects_dir(self, name):
            return "/p"

    async def yes(*a, **k):
        return True
    with restored(agent, "bsh", "DRIVER", "_mine"):
        agent.bsh, agent.DRIVER, agent._mine = bsh, Driver(), yes
        got = asyncio.run(agent.v_usage(None, {"days": 7}))
    want_usage = {"pu-mop-1": {"2026-09-24": row}, "pu-mop-2": {"2026-09-24": row},
                  "pu-mop-3": {}}
    want_by = {"pu-mop-1": {"anton": {"2026-09-24": row}},
               "pu-mop-2": {"-": {"2026-09-24": row}}, "pu-mop-3": {}}
    c.expect("usage must stay as it was", got.get("usage"), want_usage)
    c.expect("by_login", got.get("by_login"), want_by)
    c.check("usage.py must be asked --by-login",
            not (not all(s.endswith(" 7 --by-login") for s in scripts)), repr(scripts))


def check_owner_gate_267(c):
    """HYPOTHESIS (#267): ворота владения агента (_gate) -- вторая копия
    cluster.gate: свой вызов lease.may_touch, своя сборка отказа, своя
    owner_note. SOLUTION: обе стороны -- lease.gate и lease.noted; агент
    держит только своё: факты клона из тела и оператора по субъекту.
    Проверка: по той же таблице, что tests/cluster.py, агент отвечает ровно
    lease.gate. STATUS: FIXED — see #267"""
    import asyncio
    from mop.common import lease
    gate = getattr(lease, "gate", None)
    if not c.check("lease.gate exists (else the gate is written on each side)",
                   not (gate is None)):
        return
    with restored(agent, "clone_facts"), restored(agent.time, "time"):
        agent.time.time = lambda: GATE_NOW
        for name, clone, caller, force in gate_table_267():
            async def facts(_n, clone=clone):
                return clone.to_dict()
            agent.clone_facts = facts
            for project, operator in (("mop", False), (busnames.ADMIN, True)):
                req = {"name": name, "_project": project,
                       **({"_caller": caller} if caller else {}),
                       **({"force": True} if force else {})}
                got = asyncio.run(agent._gate(name, req))
                want = gate(name, clone, caller, GATE_NOW, force, operator)
                c.expect(f"agent._gate {caller} over {clone.owner} (force {force}, "
                         f"{project}) vs lease.gate", got, want)
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    src = open(os.path.join(root, "mop", "node", "agent.py")).read()
    c.check("agent.py must gate through lease.gate and note through lease.noted",
            not ("lease.may_touch(" in src or "lease.gate(" not in src
                 or "lease.noted(" not in src))


def check_junk_without_templates_276(c):
    """HYPOTHESIS (#276): v_junk звал DRIVER.templates() у любого драйвера, и
    host держал пустой глагол только ради этого вызова. SOLUTION: глагол
    гипервизора берётся через driver.hypervisor_verb; драйвер без него
    отвечает прежним [] -- ответ по шине тот же. STATUS: FIXED — see #276"""
    import asyncio
    import types

    async def bodies():
        return ["pu-mop-1"]

    async def facts(_n):
        return {}
    bare = types.SimpleNamespace(bodies=bodies)
    with restored(agent, "DRIVER", "clone_facts", "node_name"):
        agent.DRIVER, agent.clone_facts = bare, facts
        agent.node_name = lambda: "n1"
        try:
            got = asyncio.run(agent.v_junk(None, {}))
        except AttributeError as e:
            got = {"raised": str(e)}
    c.expect("junk from a driver without templates: the old reply",
             {k: got.get(k) for k in ("bodies", "work", "templates", "raised")},
             {"bodies": ["pu-mop-1"], "work": {}, "templates": [], "raised": None})


def check_owner_hook_313(c):
    """HYPOTHESIS (#313): агент ставит в клон identity владельца (#167), но
    `git -c user.name=… -c user.email=…` старше .git/config, и ничто этому
    не мешает: 27.09 коммиты #307 ушли как Claude <noreply@anthropic.com>,
    #308/#310 -- как anton@ermak.us, в master 46 коммитов Claude с 25.09;
    сам mop советовал «commit with git -c».
    SOLUTION: вместе с identity агент ставит в клон хуки pre-commit и
    pre-merge-commit (чистое слияние pre-commit не зовёт): есть локальная
    почта клона (`git config --local`, -c её не видит), а у автора или
    коммитера (`git var`, видит -c) другая -- отказ с подсказкой. Нет
    локальной identity -- пропуск: владелец без профиля коммитит с -c.
    Хук помечен второй строкой (первая -- shebang); чужой хук без метки не
    перезаписывается, и заметка это говорит; снятие identity снимает только
    свои хуки. STATUS: FIXED — see #313

    git и bash настоящие: хук ставит identity_script во временный клон, и
    решает он на настоящих коммитах и слиянии."""
    import asyncio
    import subprocess
    import tempfile
    hook = getattr(agent, "owner_hook", None)
    if hook is None:
        c.fail("#313 no agent.owner_hook: the owner's identity is not enforced")
        return
    text = hook()
    c.expect("#313 hook: shebang first, the marker second",
             text.splitlines()[:2], ["#!/bin/sh", agent.OWNER_GUARD])
    root = tempfile.mkdtemp(prefix="mop-test-313-")
    olga = {"name": "Ольга Петрова", "email": "olga@example.dev"}

    def repo(own_hook=None, hooks_path=None):
        d = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", "-b", "master", d], check=True)
        if own_hook:
            with open(os.path.join(d, ".git", "hooks", "pre-commit"), "w") as f:
                f.write(own_hook)
            os.chmod(os.path.join(d, ".git", "hooks", "pre-commit"), 0o755)
        if hooks_path:
            subprocess.run(["git", "-C", d, "config", "core.hooksPath", hooks_path], check=True)
        lease_file = os.path.join(d, ".git", "mop-owner")
        with open(lease_file, "w") as f:
            f.write("olga\t1\n")
        return d, lease_file

    def install(d, lease_file, profile):
        r = subprocess.run(["bash", "-c", agent.identity_script(d, lease_file, "olga\t1\n",
                                                                profile)],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def git(d, *args):
        r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def hooks(d):
        h = os.path.join(d, ".git", "hooks")
        return sorted(f for f in os.listdir(h) if not f.endswith(".sample"))

    d, lf = repo()
    code, out = install(d, lf, olga)
    c.expect("#313 install: both hooks in the clone", (code, hooks(d)),
             (0, ["pre-commit", "pre-merge-commit"]))
    code, out = git(d, "commit", "-q", "--allow-empty", "-m", "plain")
    c.expect("#313 a plain commit (the owner's identity) passes", (code, out), (0, ""))
    code, out = git(d, "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
                    "commit", "-q", "--allow-empty", "-m", "forged")
    c.check("#313 git -c user.* is refused, naming the owner",
            code != 0 and "drop -c user.*" in out and "olga@example.dev" in out, (code, out))
    code, out = git(d, "-c", "user.email=olga@example.dev", "commit", "-q", "--allow-empty",
                    "-m", "same")
    c.expect("#313 -c with the owner's own email passes", (code, out), (0, ""))
    # Слияние без конфликтов pre-commit не зовёт: его держит pre-merge-commit.
    git(d, "checkout", "-q", "-b", "side")
    git(d, "commit", "-q", "--allow-empty", "-m", "side")
    git(d, "checkout", "-q", "master")
    code, out = git(d, "-c", "user.email=noreply@anthropic.com", "merge", "-q", "--no-ff",
                    "side", "-m", "forged merge")
    c.check("#313 a merge with -c user.* is refused", code != 0 and "drop -c user.*" in out,
            (code, out))
    git(d, "merge", "--abort")
    code, out = git(d, "merge", "-q", "--no-ff", "side", "-m", "merge")
    c.expect("#313 a plain merge passes", (code, out), (0, ""))

    # Снятие identity снимает свои хуки; без локальной почты -c снова можно.
    code, out = install(d, lf, None)
    c.expect("#313 unset: our hooks are gone", (code, hooks(d)), (0, []))
    code, out = git(d, "-c", "user.name=X", "-c", "user.email=x@y.z", "commit", "-q",
                    "--allow-empty", "-m", "no owner")
    c.expect("#313 no local identity: git -c passes (owner without a profile)", (code, out), (0, ""))

    # Свой хук проекта не перезаписывается, и заметка это говорит.
    own = "#!/bin/sh\n# the project's own\nexit 0\n"
    d, lf = repo(own_hook=own)
    code, out = install(d, lf, olga)
    c.expect("#313 a project's pre-commit is kept",
             open(os.path.join(d, ".git", "hooks", "pre-commit")).read(), own)
    c.check("#313 the skip is said", code == 0 and "pre-commit" in out and "skipped" in out,
            (code, out))
    c.expect("#313 the merge hook still goes in", hooks(d), ["pre-commit", "pre-merge-commit"])
    code, out = install(d, lf, None)
    c.expect("#313 unset keeps the project's hook", hooks(d), ["pre-commit"])

    # core.hooksPath: .git/hooks git не читает -- ставить туда незачем, сказать.
    d, lf = repo(hooks_path=".githooks")
    code, out = install(d, lf, olga)
    c.check("#313 core.hooksPath: nothing written, the skip is said",
            code == 0 and hooks(d) == [] and "hooksPath" in out
            and not os.path.exists(os.path.join(d, ".githooks")), (code, out, hooks(d)))

    # Заметка мастеру называет пропуск.
    d, lf = repo(own_hook=own)

    async def profile(conn, name, login):
        return olga, None
    with restored(agent, "bsh", "owner_profile", "clone_dir"):
        agent.bsh, agent.owner_profile = bash, profile
        agent.clone_dir = lambda name: d
        note = asyncio.run(agent._follow_owner(None, "pu-mop-1", "olga",
                                               (lf, None, "olga\t1\n")))
    c.check("#313 the master's note names the skipped guard",
            "olga@example.dev" in note and "skipped" in note, note)


def main():
    c = Checks()
    for check in (check_sets, check_decisions, check_tmux, check_quiet,
                  check_write_home_279, check_timeouts_171, check_unclaim_181, check_intake,
                  check_main_169, check_subject_173, check_unclaim_race_189,
                  check_gates_40, check_caller_207, check_git_identity_167,
                  check_state_fact_224, check_no_screen_fact_236,
                  check_usage_by_login_244, check_owner_gate_267,
                  check_junk_without_templates_276, check_owner_hook_313):
        try:
            check(c)
        except Exception as e:
            c.fail(f"{check.__name__} raised", f"{type(e).__name__}: {e}")
    return c.report("agent")


if __name__ == "__main__":
    sys.exit(main())
