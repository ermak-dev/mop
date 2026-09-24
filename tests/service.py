#!/usr/bin/env python3
"""Каркас RPC-сервиса сервера без пула: python3 tests/service.py

Три сервиса на шине (cluster, bootstrap, builder) были тремя копиями одного
каркаса: подключение, разбор JSON, исполнитель, строка журнала, ответ. Проект
из субъекта разбирался двумя способами, и все трое печатали из библиотеки
(#149).

HYPOTHESIS: каркас скопирован, и копии уже разошлись -- в разборе проекта
(cluster хотел больше двух токенов, bootstrap -- больше одного) и в том, кто
печатает (библиотека, вопреки правилу проекта).
SOLUTION: mop/service.py -- один serve и один project_from_subject; строку
журнала сервис отдаёт данными (journal, banner), печатает командлет.
Характеризация: строки журнала те же символ в символ, что до правки --
образцы ниже переписаны из прежних print.
STATUS: FIXED — see #149
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import busnames  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


# ─── прежние правила, переписанные из кода до #149 ───────────────────────
def old_cluster_project(subject):
    parts = (subject or "").split(".")
    return parts[1] if len(parts) > 2 else ""


def old_bootstrap_project(subject):
    parts = subject.split(".")
    return parts[1] if len(parts) > 1 else ""


def old_cluster_line(project, req, out):
    return (f"{project}.{req.get('verb')} {req.get('name') or req.get('node') or req.get('origin') or ''}: "
            f"{out.get('error') or 'ok'}")


def old_bootstrap_lines(project, req, out):
    verb = req.get("verb")
    lines = [f"{project}.{verb} {req.get('name', '')}: "
             f"{out.get('error') or ('ok' if out.get('ok') else out)}"
             + (f" in {out['seconds']}s" if out.get("seconds") is not None else "")]
    if not out.get("ok", True):
        lines.append(f"{out.get('tail', '')}")
    return lines


def old_builder_line(req, out):
    return (f"build {req.get('origin', '')} {req.get('mode', '')}: "
            f"{out.get('error') or ('skipped' if out.get('skipped') else 'ok')}")


def check_project():
    """Проект -- второй токен субъекта для каждой формы, что сервисы
    получают сегодня. Субъекты -- из busnames, как у подписок."""
    from mop import service
    out = []
    for p in ("rugent", "mop", busnames.ADMIN, "a-b_c"):
        for subj, old in ((busnames.cluster(p), old_cluster_project),
                          (busnames.server(p), old_bootstrap_project)):
            got = service.project_from_subject(subj)
            if got != p or got != old(subj):
                out.append(f"project_from_subject({subj}) -> {got!r}, "
                           f"wanted {p!r} (today {old(subj)!r})")
    if service.project_from_subject(busnames.build()) != busnames.ADMIN:
        out.append(f"the build subject is the operator's: {busnames.build()}")
    # Где прежние правила расходились: субъект из двух токенов. Подписки
    # `mop.*.cluster.rpc` и `mop.*.server.rpc` ловят ровно четыре токена, так
    # что такой субъект не доходит ни до одного сервиса, и ответ не меняется.
    # Правило одно -- как у cluster: нет третьего токена -- нет проекта.
    for subj in ("mop.x", "mop", "", None):
        if service.project_from_subject(subj) != "":
            out.append(f"project_from_subject({subj!r}) must be empty")
    return out


REQS = [
    {"verb": "roster", "name": "pu-mop-1"},
    {"verb": "drain", "node": "hyper"},
    {"verb": "add", "origin": "git@x:a/mop.git"},
    {"verb": "projects"},
    {},
]
OUTS = [
    {"ok": True},
    {"error": "no job pu-mop-1 in the cluster"},
    {"ok": True, "seconds": 3},
    {"ok": False, "seconds": 12, "tail": "TASK [x]\nfatal: boom", "error": ""},
    {"ok": False, "tail": ["a", "b"]},
    {"ok": True, "skipped": True},
    {"puppets": ["pu-mop-1"]},
    {},
]


def check_journal():
    """Строка журнала из ответа -- та же символ в символ, что до #149."""
    from mop import bootstrap, builder, cluster
    out = []
    for req in REQS:
        for o in OUTS:
            got, want = cluster.journal("mop", req, o), [old_cluster_line("mop", req, o)]
            if got != want:
                out.append(f"cluster.journal({req}, {o}) -> {got!r}, wanted {want!r}")
            breq = {**req, "verb": "bootstrap"}
            got, want = bootstrap.journal("mop", breq, o), old_bootstrap_lines("mop", breq, o)
            if got != want:
                out.append(f"bootstrap.journal({breq}, {o}) -> {got!r}, wanted {want!r}")
            qreq = {**req, "verb": "build", "mode": "update"}
            got, want = builder.journal("admin", qreq, {**o, "done": True}), [old_builder_line(qreq, o)]
            if got != want:
                out.append(f"builder.journal({qreq}, {o}) -> {got!r}, wanted {want!r}")
    # Образцы глазами: так строки выглядят в journalctl.
    for got, want in (
            (cluster.journal("rugent", {"verb": "restart", "name": "pu-rugent-2"}, {"ok": True}),
             ["rugent.restart pu-rugent-2: ok"]),
            (cluster.journal("admin", {"verb": "drain", "node": "hyper"}, {"error": "no node"}),
             ["admin.drain hyper: no node"]),
            (bootstrap.journal("mop", {"verb": "bootstrap", "name": "pu-mop-1"},
                               {"ok": True, "seconds": 4}),
             ["mop.bootstrap pu-mop-1: ok in 4s"]),
            (bootstrap.journal("mop", {"verb": "bootstrap", "name": "pu-mop-1"},
                               {"ok": False, "error": "play failed", "seconds": 9, "tail": "fatal"}),
             ["mop.bootstrap pu-mop-1: play failed in 9s", "fatal"]),
            (bootstrap.journal("mop", {"verb": "ping"}, {"ok": True, "puppets": []}),
             ["mop.ping : ok"]),
            (builder.journal("admin", {"verb": "build", "origin": "git@x:a/mop.git", "mode": "missing"},
                             {"ok": True, "skipped": True, "done": True}),
             ["build git@x:a/mop.git missing: skipped"])):
        if got != want:
            out.append(f"journal -> {got!r}, wanted {want!r}")
    return out


def check_banner():
    """Строка старта -- та же, что печатал прежний serve."""
    from mop import bootstrap, builder, cluster
    out = []
    for got, want in (
            (cluster.banner("mop.*.cluster.rpc", "http://10.0.0.1:4646"),
             "mop-cluster: subscribed to mop.*.cluster.rpc, Nomad at http://10.0.0.1:4646"),
            (bootstrap.banner("mop.*.server.rpc", "/srv/ws", ["pu-mop-1", "pu-mop-2"]),
             "mop-bootstrap: subscribed to mop.*.server.rpc, workspaces in /srv/ws: pu-mop-1, pu-mop-2"),
            (bootstrap.banner("mop.*.server.rpc", "/srv/ws", []),
             "mop-bootstrap: subscribed to mop.*.server.rpc, workspaces in /srv/ws: none"),
            (builder.banner("mop.admin.build.rpc"),
             "mop-builder: subscribed to mop.admin.build.rpc")):
        if got != want:
            out.append(f"banner -> {got!r}, wanted {want!r}")
    return out


def check_silent():
    """Библиотека не печатает: печатает командлет (CLAUDE.md)."""
    out = []
    for name in ("service", "cluster", "bootstrap", "builder"):
        with open(os.path.join(ROOT, "mop", f"{name}.py")) as f:
            for n, line in enumerate(f, 1):
                if "print(" in line.split("#")[0]:
                    out.append(f"mop/{name}.py:{n} prints: {line.strip()}")
    return out


def check_errors():
    """HYPOTHESIS (#162): обработчик, бросивший вне своего try (cluster.refusal
    при лежащем Nomad), и тело-JSON не объект (`[1]` -> req.get на списке)
    роняют задачу serve: ответа нет, проситель ждёт таймаут и видит молчание,
    в журнале ни строки.
    SOLUTION: service.answer -- ответ на одно сообщение: тело не объект --
    отказ без обработчика; исключение обработчика -- отказ с его причиной и
    строка журнала.
    STATUS: FIXED — see #162"""
    import asyncio
    from mop import service
    out = []
    lines, called = [], []

    def journal(project, req, reply):
        return [f"{project}.{req.get('verb')}: {reply.get('error') or 'ok'}"]

    def ask(handler, body):
        lines.clear()
        called.clear()
        return asyncio.run(service.answer("mop-test", "mop", body, handler,
                                          journal, lines.append, lambda **ev: None))

    def boom(project, req, send):
        called.append(req)
        raise ConnectionError("Nomad unreachable")

    got = ask(boom, b'{"verb": "restart", "name": "pu-mop-1"}')
    err = got.get("error") or ""
    if "Nomad unreachable" not in err or "ConnectionError" not in err \
            or not err.startswith("mop-test"):
        out.append(f"a raising handler must answer its reason, got {got!r}")
    if len(lines) != 1 or "Nomad unreachable" not in lines[0]:
        out.append(f"a raising handler must leave exactly one journal line, got {lines!r}")
    # Ответ каркаса -- последний: проситель потока (сборщик, bus.ask_stream)
    # узнаёт итог только по done, без него причина ушла бы в таймаут 120 с.
    if got.get("done") is not True:
        out.append(f"the skeleton's own error must be final (done), got {got!r}")

    def fine(project, req, send):
        called.append(req)
        return {"ok": True, "p": project}

    for body in (b"[1]", b'"x"', b"7", b"null"):
        got = ask(fine, body)
        if got != {"error": "request is not a JSON object", "done": True}:
            out.append(f"body {body!r} must be refused as not an object, got {got!r}")
        if called:
            out.append(f"body {body!r} must not reach the handler")
        if len(lines) != 1:
            out.append(f"body {body!r} must leave one journal line, got {lines!r}")

    # Прежнее поведение не меняется: обычный запрос -- ответ обработчика как
    # есть; не-JSON вовсе -- пустой запрос в обработчик, как было до #162.
    got = ask(fine, b'{"verb": "projects"}')
    if got != {"ok": True, "p": "mop"} or called != [{"verb": "projects"}] \
            or lines != ["mop.projects: ok"]:
        out.append(f"a normal request must answer as before, got {got!r}, {lines!r}")
    got = ask(fine, b"not json")
    if got != {"ok": True, "p": "mop"} or called != [{}]:
        out.append(f"a non-JSON body must still reach the handler as {{}}, got {got!r}")
    return out


def main():
    failed = []
    for check in (check_project, check_journal, check_banner, check_silent,
                  check_errors):
        try:
            failed += check()
        except Exception as e:
            failed.append(f"{check.__name__}: {type(e).__name__}: {e}")
    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("service: FAILED" if failed else "service: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
