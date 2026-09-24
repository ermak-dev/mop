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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import agent, busnames  # noqa: E402

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


def check_sets():
    """Прежние четыре набора выводятся из таблицы -- те же и в том же порядке;
    добавленные после (ADDED) -- в конце."""
    out = []
    for got, want, what in ((tuple(agent.VERBS), OLD_VERBS + ADDED, "VERBS"),
                            (agent.PUBLIC_VERBS, OLD_PUBLIC, "PUBLIC_VERBS"),
                            (agent.ADMIN_VERBS, OLD_ADMIN, "ADMIN_VERBS"),
                            (agent.NAMED_VERBS, OLD_NAMED + ADDED, "NAMED_VERBS")):
        if tuple(got) != want:
            out.append(f"{what} -> {tuple(got)}, wanted {want}")
    return out


def check_decisions():
    """Каждый глагол x субъект (публичный .msg / мастерский .rpc) x проект
    (admin / проект) x папет свой или чужой: тот же ответ, что до #150.
    Отказ, ставший допуском, -- смена прав, а не рефакторинг."""
    out = []
    for verb in OLD_VERBS + ADDED + ("nosuch", None, ""):
        for public in (True, False):
            for project in (busnames.ADMIN, "mop"):
                for mine in (True, False):
                    want = old_decision(verb, public, project, "pu-mop-1", mine)
                    got = new_decision(verb, public, project, "pu-mop-1", mine)
                    if got != want:
                        out.append(f"{verb} public={public} project={project} mine={mine}: "
                                   f"{got!r}, wanted {want!r}")
    return out


def check_tmux():
    """Места адресации tmux -- те же строки, что до #150. Снимок экрана для
    фактов ушёл вместе с ключом screen (#236)."""
    n = "pu-mop-1"
    t = agent.Tmux(n)
    out = []
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
        if got != want:
            out.append(f"tmux -> {got!r}, wanted {want!r}")
    return out


def check_quiet():
    """Библиотека не печатает и не выходит: программа -- командлет
    `mop agent` (mop/cli/service/agent.py)."""
    out = []
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "mop", "agent.py")
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
                out.append(f"mop/agent.py:{n}: {line.strip()}")
    if hasattr(agent, "_conn"):
        out.append("mop/agent.py keeps a global connection: pass it explicitly")
    return out


# ── таймаут шелла -- не успех (#171) ─────────────────────────────────────
# HYPOTHESIS: bsh() на таймауте отдаёт ("", None), а мутирующие глаголы
# агента читали это как успех: запись владельца «прошла», type и Escape
# «напечатаны» и возвращали экран.
# SOLUTION: для записи владельца и type None -- отказ «timed out». Пробы
# только для чтения (буфер пейна, проба сессии) -- как были.
# STATUS: FIXED — see #171
def check_timeouts_171():
    import asyncio
    out = []
    saved = agent.bsh, agent.clone_facts

    def fake(code):
        async def bsh(name, script, timeout=20):
            return "", code
        return bsh

    async def facts(name):
        return {"owner": None, "dirty": 0, "ahead": 0}
    try:
        agent.clone_facts = facts
        for code in (None, 0):
            agent.bsh = fake(code)
            for cmd in ("/status", "Escape"):
                got = asyncio.run(agent.v_type(None, {"name": "pu-mop-1", "command": cmd}))
                if code is None and "timed out" not in (got.get("error") or ""):
                    out.append(f"type {cmd}, timeout -> {got!r}")
                if code == 0 and got != {"screen": ""}:
                    out.append(f"type {cmd}, success -> {got!r}")
            refused, undo, _ = asyncio.run(agent._claim("pu-mop-1", {"owner": "m-1"}))
            if code is None and "timed out" not in (refused or ""):
                out.append(f"owner write, timeout -> {refused!r}")
            if code == 0 and (refused or not undo):
                out.append(f"owner write, success -> {refused!r} {undo!r}")
        # Кривое имя до шелла не доходит: bsh отдал бы None, и это прочиталось
        # бы как таймаут, а то и как успех.
        got = asyncio.run(agent.v_type(None, {"name": "pu-x; rm", "command": "/status"}))
        if "doesn't look like" not in (got.get("error") or ""):
            out.append(f"type with a bad name -> {got!r}")
        # Пробы только для чтения: ответ прежний.
        agent.bsh = fake(None)
        if asyncio.run(agent.pane_lines("pu-mop-1")) != []:
            out.append("pane_lines must tolerate a timeout")
        if asyncio.run(agent.session_probe("pu-mop-1")) != "none":
            out.append("session_probe must read a timeout as none")
    finally:
        agent.bsh, agent.clone_facts = saved
    return out


# ── откат владельца -- с результатом (#181) ──────────────────────────────
# HYPOTHESIS: _unclaim пишет прежнего владельца назад (или rm -f) и код
# шелла не смотрит: на таймауте (#171) или ошибке аренда остаётся за
# мастером, чей send не доехал, и следующему мастеру send отказывает,
# называя не того владельца -- а отказ первого об этом молчит.
# SOLUTION: _unclaim отдаёт причину неудачи (driver.why, как в #171), и
# отказ v_send дописывает «owner not restored: <причина>»; удачный откат
# текст не меняет.
# STATUS: FIXED — see #181
def check_unclaim_181():
    import asyncio
    out = []
    saved = agent.bsh, agent.clone_facts, agent.session_json
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
    try:
        agent.clone_facts, agent.session_json = facts, session_json
        for rollback, want in ((("", None), delivery["error"] + "; owner not restored: timed out"),
                               (("rm: Permission denied", 1),
                                delivery["error"] + "; owner not restored: rm: Permission denied"),
                               (("", 0), delivery["error"])):
            agent.bsh, calls = fake(rollback)
            got = asyncio.run(agent.v_send(None, {"name": "pu-mop-1", "owner": "m-1",
                                                  "message": "hi"}))
            if len(calls) != 2 or "rm -f" not in calls[-1]:
                out.append(f"send refused, rollback {rollback}: no rollback shell ({calls})")
            if got.get("error") != want:
                out.append(f"send refused, rollback {rollback}: {got.get('error')!r}, "
                           f"wanted {want!r}")
    finally:
        agent.bsh, agent.clone_facts, agent.session_json = saved
    return out


def check_intake():
    """HYPOTHESIS (#168): тело-JSON не объект (`[1]`, `"x"`) роняет задачу
    handle на req["_project"], а нехэшируемый глагол (`{"verb": [1]}`) -- на
    поиске в таблице внутри refusal(): ответа нет, проситель ждёт таймаут.
    SOLUTION: handle отказывает телу-не-объекту до таблицы; refusal читает
    нестроковый глагол как неизвестный -- тот же отказ, что у неизвестной
    строки.
    STATUS: FIXED — see #168"""
    import asyncio
    import json
    out, called = [], []

    class Msg:
        def __init__(self, data, subject):
            self.subject, self.data, self.replies = subject, data, []

        async def respond(self, data):
            self.replies.append(json.loads(data))

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
            msg = Msg(body, subject)
            asyncio.run(agent.handle(None, msg, public=subject.endswith(".msg")))
            return msg.replies

        for body in (b"[1]", b'"x"', b"7", b"null"):
            got = ask(body)
            if got != [{"error": "request is not a JSON object"}] or called:
                out.append(f"body {body!r}: replies {got!r}, verbs called {called}")

        def unknown(v):
            return f"no such verb {v}; available: {', '.join(sorted(keep))}"
        for verb in ([1], {}, 7, True):
            got = ask(json.dumps({"verb": verb}).encode())
            if got != [{"error": unknown(verb)}] or called:
                out.append(f"verb {verb!r}: replies {got!r}, wanted the unknown-verb "
                           f"refusal, verbs called {called}")
        # Прежнее не меняется: не-JSON, неизвестная строка, обычный запрос.
        if ask(b"junk") != [{"error": "request is not JSON"}]:
            out.append("a non-JSON body must keep its old refusal")
        if ask(b'{"verb": "nosuch"}') != [{"error": unknown("nosuch")}]:
            out.append("an unknown string verb must keep its refusal")
        got = ask(b'{"verb": "ping"}', "mop.mop.node.hyper.msg")
        if got != [{"verb": "ping"}] or called != ["ping"]:
            out.append(f"a well-formed ping must reach its verb: {got!r}, {called}")
    finally:
        agent.VERBS.clear()
        agent.VERBS.update(keep)
    return out


def check_main_169():
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
    r = subprocess.run([sys.executable, "-m", "mop.agent", "--check"], cwd=root,
                       env=dict(os.environ, PYTHONPATH=shadow),
                       capture_output=True, text=True)
    want = "bus library needed: pip install --user --break-system-packages nats-py\n"
    if r.returncode != 1 or r.stdout or r.stderr != want:
        return [f"python3 -m mop.agent without nats: code {r.returncode}, "
                f"stdout {r.stdout!r}, stderr {r.stderr[-300:]!r}"]
    return []


def check_subject_173():
    """HYPOTHESIS (#173): агент брал проект из субъекта своим правилом
    (`parts[1] if len(parts) > 1`), а не service.project_from_subject (#149):
    на его субъектах они сходятся, но это второе определение.
    SOLUTION: агент зовёт общую функцию; на его субъектах ответ тот же.
    STATUS: FIXED — see #173"""
    from mop import service
    out = []

    def old(subject):
        parts = subject.split(".")
        return parts[1] if len(parts) > 1 else ""
    for p in ("mop", busnames.ADMIN, "a-b_c"):
        subs = [busnames.node(p, "hyper", "rpc"), busnames.node(p, "hyper", "msg"),
                busnames.broadcast(p)]
        for subj in subs:
            if service.project_from_subject(subj) != old(subj) or old(subj) != p:
                out.append(f"{subj}: shared {service.project_from_subject(subj)!r}, "
                           f"old {old(subj)!r}, wanted {p!r}")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "mop", "agent.py")
    text = open(path).read()
    if "parts[1]" in text or "service.project_from_subject(" not in text:
        out.append("mop/agent.py must take the project via service.project_from_subject")
    return out


def check_unclaim_race_189():
    """HYPOTHESIS (#189): v_send ставит аренду под _owner_locks, а откат
    неудачной доставки делает ВНЕ замка и не глядя: второй мастер (force)
    успевает взять папета, пока первый ждёт доставки, и откат первого
    перетирает его запись прежним владельцем или стирает файл.
    SOLUTION: откат -- под тем же замком и только если в файле всё ещё наша
    запись. STATUS: FIXED — see #189

    Шелл настоящий: bsh исполняет скрипт агента bash'ем над временным
    клоном, так что сравнение-и-запись проверяется как есть, а не заглушкой."""
    import asyncio
    import subprocess
    import tempfile
    from mop import lease
    from mop.domain import Owner
    out = []
    root = tempfile.mkdtemp(prefix="mop-test-189-")
    os.makedirs(os.path.join(root, ".git"))
    path = os.path.join(root, lease.FILE)
    saved = (agent.bsh, agent.clone_facts, agent.clone_dir, agent.session_json, agent._event)

    async def bsh(name, script, timeout=20):
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return r.stdout + r.stderr, r.returncode

    async def facts(name):
        text = open(path).read() if os.path.exists(path) else ""
        owner = Owner.parse(text)
        return {"owner": owner and owner.to_dict(), "dirty": 0, "ahead": 0,
                "cur": "master", "def": "master"}

    async def no_event(*a, **k):
        return None

    def owner_now():
        return Owner.parse(open(path).read()) if os.path.exists(path) else None
    try:
        agent.bsh, agent.clone_facts, agent._event = bsh, facts, no_event
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
        if not ra.get("error") or rb.get("error"):
            out.append(f"race: a {ra!r}, b {rb!r} -- a must fail, b must deliver")
        if getattr(owner_now(), "user", None) != "bob":
            out.append(f"race: the lease must stay with bob, got {owner_now()!r}")

        # Одна неудачная доставка -- откат как прежде: к прежнему владельцу,
        # а без него файл снимается.
        async def fail(name, cmd, timeout=20):
            return {"error": "not delivered"}
        agent.session_json = fail
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
            if not got.get("error") or now != want:
                out.append(f"single failed send over {before!r}: {got!r}, file {now!r}, "
                           f"wanted {want!r}")
    finally:
        (agent.bsh, agent.clone_facts, agent.clone_dir, agent.session_json,
         agent._event) = saved
    return out


def check_gates_40():
    """HYPOTHESIS (#40): владельца сверяет только send; type (slash) и wipe
    пускают любого мастера проекта к папету, которого ведёт другой.
    SOLUTION: те же ворота (lease.may_touch) под тем же замком на папета,
    что у send; оператор (субъект admin) проходит всегда, force -- называя,
    у кого. Глагол clone отдаёт факты клона и при мёртвой сессии: по ним
    решает сервис кластера.
    STATUS: FIXED — see #40"""
    import asyncio
    import time
    from mop.domain import Owner
    out = []
    olga = Owner("olga", int(time.time()) - 60).to_dict()
    clone = {"cur": "bug/1-x", "def": "master", "dirty": 2, "ahead": 0, "owner": olga}
    shelled, destroyed = [], []
    saved = (agent.bsh, agent.clone_facts, agent.tmux_alive, agent.DRIVER, agent._event)

    async def bsh(name, script, timeout=20):
        shelled.append(script)
        return "", 0

    async def facts(name):
        return dict(clone)

    async def dead(name):
        return False

    async def no_event(*a, **k):
        return None

    class Driver:
        async def destroy(self, name):
            destroyed.append(name)
            return {"target": "gone"}
    try:
        agent.bsh, agent.clone_facts, agent.tmux_alive = bsh, facts, dead
        agent.DRIVER, agent._event = Driver(), no_event
        base = {"name": "pu-mop-1", "_project": "mop"}
        for verb, fn, extra, touched in (("type", agent.v_type, {"command": "/status"}, shelled),
                                         ("wipe", agent.v_wipe, {}, destroyed)):
            for who, more, allowed in (("another master", {"owner": "anton"}, False),
                                       ("anonymous", {}, False),
                                       ("the owner", {"owner": "olga"}, True),
                                       ("force", {"owner": "anton", "force": True}, True),
                                       ("the operator", {"owner": "anton", "_project": "admin"}, True)):
                touched.clear()
                got = asyncio.run(fn(None, {**base, **extra, **more}))
                if allowed and (got.get("error") or not touched):
                    out.append(f"{verb} by {who} must pass: {got!r}")
                if not allowed and ("olga" not in (got.get("error") or "") or touched):
                    out.append(f"{verb} by {who} must be refused naming olga, "
                               f"touching nothing: {got!r}, {touched}")
                if who == "force" and "olga" not in (got.get("owner_note") or ""):
                    out.append(f"{verb} with force must name whom: {got!r}")
        # Ничей -- как до #40: запрос без owner проходит.
        clone["owner"] = None
        shelled.clear()
        got = asyncio.run(agent.v_type(None, {**base, "command": "/status"}))
        if got.get("error") or not shelled:
            out.append(f"type on nobody's puppet must pass as before: {got!r}")
        fn = getattr(agent, "v_clone", None)
        if fn is None:
            out.append("agent.v_clone is missing")
        else:
            got = asyncio.run(fn(None, dict(base)))
            if got != {"clone": clone}:
                out.append(f"clone must return the clone's facts: {got!r}")
    finally:
        (agent.bsh, agent.clone_facts, agent.tmux_alive, agent.DRIVER, agent._event) = saved
    return out


def check_caller_207():
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
    import json
    from mop import bus, lease
    out = []
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
    if got != want:
        out.append(f"busnames.caller -> {got}, wanted {want}")
    # Логин -- любой, кроме пустого и управляющих символов: в LDAP/AD
    # anton.ermak -- норма (#208). В токен субъекта он кодируется обратимо.
    for bad in ("", None, "a\tb", "a\nb", "a\x00b", "a\x7fb"):
        if busnames.valid_login(bad):
            out.append(f"{bad!r} must not be a login")
    for fine in ("anton", "ivan_p-2", "anton.ermak", "a b", "a*", "a>", "a%b", "Антон"):
        if not busnames.valid_login(fine):
            out.append(f"{fine!r} must be a login")
    for login, token in (("anton", "anton"), ("anton.ermak", "anton%2Eermak"),
                         ("a b", "a%20b"), ("a*>", "a%2A%3E"), ("50%", "50%25"),
                         ("Антон", "Антон"), ("a\u00a0b", "a%C2%A0b")):
        got = busnames.login_token(login)
        if got != token:
            out.append(f"login_token({login!r}) -> {got!r}, wanted {token!r}")
        if busnames.login_of(token) != login:
            out.append(f"login_of({token!r}) -> {busnames.login_of(token)!r}, wanted {login!r}")
        if any(c in token for c in ".*> ") or any(c.isspace() for c in token):
            out.append(f"token {token!r} is not a single literal NATS token")
    # Инъективно: «a.b» и буквальное «a%2Eb» -- разные токены.
    if busnames.login_token("a.b") == busnames.login_token("a%2Eb"):
        out.append("two logins must never share a token: a.b vs a%2Eb")
    # Неканоничный токен -- не логин: иначе один логин читался бы из двух.
    for token in ("a%2eb", "a%41", "a%", "a%zz", "*"):
        if busnames.login_of(token) is not None:
            out.append(f"login_of({token!r}) must be None: not a canonical token")
    subs = busnames.agent_subscriptions("hyper")
    if "mop.*.node.hyper.rpc.*" not in subs["rpc"] or "mop.*.node.hyper.rpc" not in subs["rpc"]:
        out.append(f"the agent must listen on both the login and the old rpc: {subs}")

    # Разбор в handle: глагол-заглушка отдаёт то, что видят ворота.
    seen = []

    async def probe(_conn, req):
        seen.append(lease.caller(req))
        return {"ok": True}

    class Msg:
        def __init__(self, subject, body):
            self.subject, self.data = subject, json.dumps(body).encode()

        async def respond(self, data):
            pass

    async def mine(name):
        return "mop"
    saved = (dict(agent.VERBS), agent.puppet_project)
    try:
        agent.VERBS["send"] = dataclasses.replace(agent.VERBS["send"], fn=probe)
        agent.puppet_project = mine
        body = {"verb": "send", "name": "pu-mop-1", "owner": "bob", "_caller": "bob"}
        for subj, public, want in (
                ("mop.mop.node.hyper.rpc.alice", False, ("alice", True)),
                ("mop.mop.node.hyper.rpc", False, ("bob", False)),
                ("mop.mop.node.hyper.msg", True, (None, False))):
            seen.clear()
            asyncio.run(agent.handle(None, Msg(subj, body), public))
            if seen != [want]:
                out.append(f"caller over {subj} with a forged body -> {seen}, wanted {want}")
    finally:
        agent.VERBS.clear()
        agent.VERBS.update(saved[0])
        agent.puppet_project = saved[1]

    # Клиент: человек спрашивает по субъекту со своим логином, машина -- без.
    keep = bus.login
    try:
        bus.login = lambda: "alice"
        if bus.subject("hyper", project="mop") != "mop.mop.node.hyper.rpc.alice":
            out.append(f"a human's rpc subject must carry the login: {bus.subject('hyper', project='mop')}")
        if bus.subject("hyper", "msg", project="mop") != "mop.mop.node.hyper.msg":
            out.append("msg carries no login")
        if bus.cluster_subject("mop") != "mop.mop.cluster.rpc.alice":
            out.append(f"a human's cluster subject must carry the login: {bus.cluster_subject('mop')}")
        bus.login = lambda: "anton.ermak"
        if bus.subject("hyper", project="mop") != "mop.mop.node.hyper.rpc.anton%2Eermak" \
                or bus.cluster_subject("mop") != "mop.mop.cluster.rpc.anton%2Eermak":
            out.append("a dotted login must travel encoded")
        bus.login = lambda: None
        if bus.subject("hyper", project="mop") != "mop.mop.node.hyper.rpc" \
                or bus.cluster_subject("mop") != "mop.mop.cluster.rpc":
            out.append("a machine asks without a login token")
    finally:
        bus.login = keep
    if busnames.without_caller("mop.mop.node.hyper.rpc.alice") != "mop.mop.node.hyper.rpc" \
            or busnames.without_caller("mop.mop.cluster.rpc.alice") != "mop.mop.cluster.rpc" \
            or busnames.without_caller("mop.mop.events") != "mop.mop.events":
        out.append("without_caller must strip exactly the login token (the fallback to "
                   "an agent from before #207)")
    return out


def check_git_identity_167():
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
    from mop import lease
    from mop.domain import Owner
    out = []
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
    saved = (agent.bsh, agent.clone_facts, agent.clone_dir, agent.session_json,
             agent._event, agent.puppet_project)

    async def bsh(name, script, timeout=20):
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return r.stdout + r.stderr, r.returncode

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

    try:
        agent.bsh, agent.clone_facts, agent.session_json = bsh, facts, delivered
        agent._event, agent.puppet_project = no_event, project
        agent.clone_dir = lambda name: state["clone"]

        # Проверенный владелец с профилем -- identity в клоне; спросили
        # сервер своего проекта его логином.
        state["clone"] = clone()
        got = send(caller="olga")
        if ident() != profiles["olga"]:
            out.append(f"verified owner: identity {ident()}, wanted {profiles['olga']}")
        if asked != [("mop.mop.server.rpc", {"verb": "identity", "login": "olga"})]:
            out.append(f"verified owner: asked {asked}, wanted the server's identity verb")
        if "olga@example.dev" not in (got.get("owner_note") or ""):
            out.append(f"verified owner: the reply must name the identity, got {got}")

        # Названный телом (прежний субъект) -- identity не трогаем, сервер
        # не спрашиваем.
        state["clone"], asked[:] = clone(), []
        got = send(owner="bob")
        if ident() or asked or got.get("error"):
            out.append(f"self-declared: identity {ident()}, asked {asked}, reply {got}")

        # Профиля нет -- identity не ставится, заметка велит -c.
        state["clone"] = clone()
        got = send(caller="ivan")
        note = got.get("owner_note") or ""
        if ident() or got.get("error") or "ivan" not in note or "git -c" not in note:
            out.append(f"no profile: identity {ident()}, reply {got}")

        # Сервер не ответил -- доставка не страдает, identity нет, заметка.
        state["clone"] = clone()
        got = send(caller="down")
        if ident() or got.get("error") or "git -c" not in (got.get("owner_note") or ""):
            out.append(f"server silent: identity {ident()}, reply {got}")

        # force: аренда olga (работа в клоне) переходит к petr -- и identity.
        state["clone"] = clone()
        send(caller="olga")
        got = send(caller="petr", force=True)
        if ident() != profiles["petr"]:
            out.append(f"force: identity {ident()}, wanted {profiles['petr']}; reply {got}")
        # ...к ivan без профиля -- identity olga'и снята, не оставлена ему.
        send(caller="olga", force=True)
        got = send(caller="ivan", force=True)
        if ident():
            out.append(f"force to a login without a profile: the old identity stays {ident()}")
        # Названный телом с force -- identity не трогает.
        send(caller="olga", force=True)
        send(owner="bob", force=True)
        if ident() != profiles["olga"]:
            out.append(f"self-declared force: identity must stay, got {ident()}")

        # Доставка не удалась -- аренда откатывается, identity прежняя.
        state["clone"] = clone()
        send(caller="olga")
        state["fail"] = True
        got = send(caller="petr", force=True)
        state["fail"] = False
        if not got.get("error") or ident() != profiles["olga"]:
            out.append(f"failed delivery: identity {ident()}, reply {got}")
    finally:
        (agent.bsh, agent.clone_facts, agent.clone_dir, agent.session_json,
         agent._event, agent.puppet_project) = saved
    return out


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
def check_state_fact_224():
    import asyncio
    import json
    out = []
    saved = (agent.bsh, agent.clone_facts, agent.tmux_alive)
    rec = {"status": "idle", "waitingFor": None, "alive": True, "listen": False,
           "turn": {"event": "StopFailure", "at": 1790245436,
                    "error": "authentication_failed", "detail": "Login expired"}}
    none = {"status": None, "waitingFor": None, "alive": False, "listen": False, "turn": None}
    calls = []

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
    try:
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
            if got.get("session") != want_session or got.get("state") != want_state \
                    or ("state" in got) != (want_state is not None) or calls != want_calls:
                out.append(f"facts, {what} -> {got!r}, calls {calls}")
    finally:
        (agent.bsh, agent.clone_facts, agent.tmux_alive) = saved
    return out


# ── факты без экрана (#236) ──────────────────────────────────────────────
# HYPOTHESIS: после #235 вердикт facts["screen"] не читает, а агент всё равно
# снимает пейн (capture-pane) у каждого папета на каждом опросе ростера — в
# теле pve это лишний заход в тело и 20 строк по шине. Держали его на переход
# для мастеров со старой библиотекой; все они теперь не старше b0aca2c.
# SOLUTION: facts не снимает экран и не шлёт ключ screen; вердикт по тем же
# фактам прежний. tail/slash/attach снимают пейн своими глаголами.
# STATUS: FIXED — see #236
def check_no_screen_fact_236():
    import asyncio
    import json
    from mop.state import verdict
    out = []
    saved = (agent.bsh, agent.clone_facts)
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
    try:
        agent.bsh, agent.clone_facts = bsh, clone
        got = asyncio.run(agent.facts("pu-mop-1"))
        if "screen" in got:
            out.append(f"facts must carry no screen key: {got!r}")
        if any("capture-pane" in s for s in scripts):
            out.append(f"facts must not capture the pane: {scripts!r}")
        with_screen = {**got, "screen": "Not logged in · Run /login"}
        if str(verdict(got)) != "free (master)" or str(verdict(with_screen)) != str(verdict(got)):
            out.append(f"verdict must not change: {verdict(got)} vs {verdict(with_screen)}")
    finally:
        agent.bsh, agent.clone_facts = saved
    return out


# ── расход по логину в глаголе usage (#244) ───────────────────────────────
# HYPOTHESIS: глагол usage отдаёт расход по папету и дню, но не по логину
# мастера — оператор не видит, кто сколько тратит.
# SOLUTION: usage.py зовётся с --by-login, агент кладёт рядом с прежним полем
# usage новое by_login: {папет: {логин: {дата: {вид: n}}}}; usage не
# меняется. Старый usage.py в теле флага не знает и печатает прежнюю форму —
# тогда весь расход папета за «-»: инвариант (сумма по логинам == usage)
# держится, а приписать его некому.
# STATUS: FIXED — see #244
def check_usage_by_login_244():
    import asyncio
    import json
    out = []
    saved = (agent.bsh, agent.DRIVER, agent._mine)
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
    try:
        agent.bsh, agent.DRIVER, agent._mine = bsh, Driver(), yes
        got = asyncio.run(agent.v_usage(None, {"days": 7}))
    finally:
        agent.bsh, agent.DRIVER, agent._mine = saved
    want_usage = {"pu-mop-1": {"2026-09-24": row}, "pu-mop-2": {"2026-09-24": row},
                  "pu-mop-3": {}}
    want_by = {"pu-mop-1": {"anton": {"2026-09-24": row}},
               "pu-mop-2": {"-": {"2026-09-24": row}}, "pu-mop-3": {}}
    if got.get("usage") != want_usage:
        out.append(f"usage must stay as it was: {got.get('usage')!r}")
    if got.get("by_login") != want_by:
        out.append(f"by_login: {got.get('by_login')!r}")
    if not all(s.endswith(" 7 --by-login") for s in scripts):
        out.append(f"usage.py must be asked --by-login: {scripts!r}")
    return out


def main():
    failed = []
    for check in (check_sets, check_decisions, check_tmux, check_quiet,
                  check_timeouts_171, check_unclaim_181, check_intake,
                  check_main_169, check_subject_173, check_unclaim_race_189,
                  check_gates_40, check_caller_207, check_git_identity_167,
                  check_state_fact_224, check_no_screen_fact_236,
                  check_usage_by_login_244):
        try:
            failed += check()
        except Exception as e:
            failed.append(f"{check.__name__}: {type(e).__name__}: {e}")
    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("agent: FAILED" if failed else "agent: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
