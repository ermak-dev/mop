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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import agent, busnames  # noqa: E402

# ─── до #150: наборы и логика handle, переписанные дословно ──────────────
OLD_VERBS = ("ping", "local", "state", "states", "sizes", "send", "tail",
             "type", "write", "disk", "wipe", "usage", "junk")
OLD_PUBLIC = ("ping", "local", "state", "states", "send", "tail")
OLD_ADMIN = ("disk", "junk")
OLD_NAMED = ("state", "send", "tail", "type", "wipe")


def old_decision(verb, public, project, name, mine):
    """Отказ, как его давал handle до #150, либо None -- глагол исполняется."""
    if verb not in OLD_VERBS:
        return f"no such verb {verb}; available: {', '.join(sorted(OLD_VERBS))}"
    if public and verb not in OLD_PUBLIC:
        return f"verb {verb} is available to the master only"
    if verb in OLD_ADMIN and project != busnames.ADMIN:
        return f"verb {verb} is node-level, not given to project {project}"
    if verb in OLD_NAMED and not mine:
        return f"puppet {name} is not in project {project}"
    return None


def new_decision(verb, public, project, name, mine):
    """То же через таблицу -- в том порядке, в каком это делает handle."""
    why = agent.refusal(verb, public, project)
    if why is None and agent.VERBS[verb].named and not mine:
        why = agent.foreign(name, project)
    return why


def check_sets():
    """Прежние четыре набора выводятся из таблицы -- те же и в том же порядке."""
    out = []
    for got, want, what in ((tuple(agent.VERBS), OLD_VERBS, "VERBS"),
                            (agent.PUBLIC_VERBS, OLD_PUBLIC, "PUBLIC_VERBS"),
                            (agent.ADMIN_VERBS, OLD_ADMIN, "ADMIN_VERBS"),
                            (agent.NAMED_VERBS, OLD_NAMED, "NAMED_VERBS")):
        if tuple(got) != want:
            out.append(f"{what} -> {tuple(got)}, wanted {want}")
    return out


def check_decisions():
    """Каждый глагол x субъект (публичный .msg / мастерский .rpc) x проект
    (admin / проект) x папет свой или чужой: тот же ответ, что до #150.
    Отказ, ставший допуском, -- смена прав, а не рефакторинг."""
    out = []
    for verb in OLD_VERBS + ("nosuch", None, ""):
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
    """Девять мест адресации tmux -- те же строки, что до #150."""
    n = "pu-mop-1"
    t = agent.Tmux(n)
    out = []
    for got, want in (
            # tmux_alive
            (t.alive(), f"tmux -L {n} has-session -t {n} 2>/dev/null"),
            # screen: порядок -t/-p прежний, сохранён как был
            (t.screen(20), f"tmux -L {n} capture-pane -t {n} -p -S - "
                           f"| grep -v '^$' | tail -20"),
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
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if line.startswith('if __name__ == "__main__":'):
                break       # переход (#150): `python3 -m mop.agent` в юнитах узлов
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
            agent.VERBS[v] = d._replace(fn=spy(v))

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


def main():
    failed = []
    for check in (check_sets, check_decisions, check_tmux, check_quiet,
                  check_timeouts_171, check_intake):
        try:
            failed += check()
        except Exception as e:
            failed.append(f"{check.__name__}: {type(e).__name__}: {e}")
    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("agent: FAILED" if failed else "agent: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
