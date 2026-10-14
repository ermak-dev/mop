#!/usr/bin/env python3
"""Фронтенды управления ролями #618: python3 tests/roleops.py

HYPOTHESIS (#618): командлеты add/update/recycle и их MCP-объявления ролей
не знают. MCP add принимает только origin, CLI add не принимает роль, а
грант-мастер (#582) обязан звать add с ролью и БЕЗ клиентского workspace --
сегодня ops.add всегда собирает политику рабочей копии, сервер отказывает, и
грант не может поднять ребёнка. Старому серверу такой запрос завёл бы
безролевого папета молча.
SOLUTION: тонкий клей командлетов против уже собранного сервера 582:
`mop add --role R` и MCP-аргумент role, ping-предзапрос свода повторов
(client.roles.protocol), эхо тройки (client.roles.echo), request_id на
каждое создание; грант зовёт ops.add вовсе без политики (API #617:
workspace=None -- ни одного поля на провод), update/recycle гранта -- без
чтения политики (ops.update(preserve_workspace=True), recycle
workspace_of=None, контракт уже есть). Сервер не правится: проверки гоняют
РЕАЛЬНЫЕ обработчики cluster.answer с FakeNomad и реальным реестром grants.
STATUS: FIXED — see #618
"""
import os
import sys
import uuid

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, FakeNomad, patched, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("MOP_SERVER_LAN", "127.0.0.1")

from mop.cli.core import add as cli_add  # noqa: E402
from mop.cli.core import recycle as cli_recycle  # noqa: E402
from mop.cli.core import update as cli_update  # noqa: E402
from mop.client import roles  # noqa: E402
from mop.common import bus, context, ops  # noqa: E402

# Среда сервера 582 без пула: общие швы soul_env/soul_parent/soul_answer
# (#617 вынес их в _lib) -- те же реальные обработчики, что проверяет #582.
from _lib import soul_answer, soul_env, soul_parent  # noqa: E402

ALPHA = "git@h:g/alpha.git"
ORIGIN = ALPHA
# Согласованная политика родителя: наследуемый снапшот (PLAY из #582).
POLICY = "- vars:\n    answer: 42\n  tasks:\n  - name: greet\n    shell: echo hi\n"
# Что отдаёт подмена workspace_text: валидный один-плей YAML, сервер его
# принимает (проверка не о тексте политики, а о том, ЕДЕТ ли она).
PLAY = "- name: greet\n  tasks: []\n"


class Wire:
    """Провод командлета -> РЕАЛЬНЫЙ cluster.answer.

    call_cluster командлета (verb, **fields) оборачивается в req с
    _caller/_subject, как их кладёт каркас сервиса по субъекту провода:
    человек -- легаси-rpc, грант -- role-канал. fields едут на сервер как
    есть: sanitize здесь не место, лишнее поле обязан увидеть сервер (и
    отказать) -- иначе проверка фронтенда проверяла бы саму себя."""

    def __init__(self, api, who, grant=False, project="alpha"):
        self.api, self.project, self.who = api, project, who
        self.subject = (f"mop.{project}.cluster.role.{who}" if grant
                        else f"mop.{project}.cluster.rpc")
        self.seen = []
        self.marker = None

    def __call__(self, verb, timeout=None, project=None, **fields):
        self.seen.append((verb, fields))
        if verb == "bootstrap_result":
            # Прогон мгновенно сыгран: проверка фронтенда и сервера, не
            # узла; метку отдаёт регистрация, провод её помнит.
            return {"ok": True, "name": fields.get("name"),
                    "result": {"sent": self.marker, "played": True, "ok": True,
                               "seconds": 0}}
        if verb == "alloc":
            # Аллокация фейкового Nomad'а: джоб зарегистрирован -- он и
            # «поднят» (проверка ждёт провода и сервера, не планировщика).
            return {"alloc": {"ID": "a-1", "JobID": fields.get("name"),
                              "NodeName": "n1", "ClientStatus": "running",
                              "TaskStates": {"claude": {"State": "running"}}}}
        req = {"verb": verb, "_caller": self.who, "_subject": self.subject,
               **fields}
        got = soul_answer(self.api, self.project, req)
        if got.get("error"):
            raise bus.Refused(got["error"])
        if verb == "add" and got.get("bootstrap_marker"):
            self.marker = got["bootstrap_marker"]
        return got

    def sent(self, verb):
        """Поля последнего (или единственного) вызова глагола."""
        got = [f for v, f in self.seen if v == verb]
        return got[-1] if got else {}


class Quiet:
    """Прогресс без терминала: тихий, как у командлетов вне TTY."""

    def __init__(self, *a):
        pass

    def step(self, text):
        pass

    def clear(self):
        pass


def drive(module, argv, api, who, grant=False):
    """Командлет с проводом на реальный сервер -> провод (code в w.code).

    bus.call_cluster ведёт в cluster.answer (FakeNomad и реестр grants --
    уже подняты soul_env'ом вызывающего), login/principal -- человек или
    принципал, Progress молчит, guard выключен (команда вне проекта),
    workspace_text виден: если фронтенд гранта его прочитал -- провод это
    увидит вызовом, а проверка скажет «не должен был»."""
    w = Wire(api, who, grant)
    reads = []
    with patched(bus, call_cluster=w, login=lambda: None if grant else who,
                 principal=lambda: who if grant else None), \
            patched(cli_add.lib, project_ready=lambda p: True,
                    origin=lambda *a: ORIGIN, Progress=Quiet), \
            patched(cli_update.lib, Progress=Quiet, guard=lambda n: None), \
            patched(cli_recycle.lib, Progress=Quiet, guard=lambda n: None), \
            patched(cli_add._common, workspace_text=lambda o: reads.append(("add", o)) or ("- name: greet\n  tasks: []\n", {"source": "origin", "present": True, "tasks": 1})), \
            patched(cli_update._common, workspace_text=lambda o: reads.append(("update", o)) or ("- name: greet\n  tasks: []\n", {"source": "origin", "present": True, "tasks": 1})), \
            patched(cli_recycle._common, workspace_text=lambda o: reads.append(("recycle", o)) or ("- name: greet\n  tasks: []\n", {"source": "origin", "present": True, "tasks": 1})), \
            patched(cli_recycle.puppets, _wait_stopped=lambda n: None,
                    wipe=lambda *a, **k: None), \
            context.isolated(context.Context()):
        w.code = run_command(module.main, argv, via_cli=True)[2]
    w.reads = reads
    return w


def grant_env():
    """Живой грант-мастер на реальном сервере -> (api, ctx, principal, name).

    Родителя поднимает ЧЕЛОВЕК с согласованной политикой (текст человека и
    есть согласование, ревью #582): без неё делегированный add отказан
    «нечего наследовать» (#582). Дальше командлеты действуют от лица его
    принципала -- тем же путём, каким живой грант-мастер зовёт сервис."""
    api = FakeNomad()
    g = soul_env(api)
    root = g.__enter__()
    principal, parent, _got = soul_parent(api, root, workspace=POLICY)
    return api, g, principal, parent


def child_of(api, parent):
    """Джобы-дети родителя (все, кроме него)."""
    return [n for n in api.jobs if n != parent]


def main():
    c = Checks()

    # ── 1. Грант поднимает ребёнка: `add --role` без клиентской политики ──
    # HYPOTHESIS: ops.add всегда везёт workspace, а делегированный add (#582)
    # клиентский текст отвергает -- грант-мастер не может создать ребёнка.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, ["--role", "puppet"], api, principal, grant=True)
        add = w.sent("add")
        c.check("#618 grant add carries the role, a request_id and no policy",
                add.get("soul") == "puppet" and add.get("request_id")
                and "workspace" not in add and "bootstrap_sent" not in add,
                add)
        c.check("#618 grant add reads no client policy",
                not [r for r in w.reads if r[0] == "add"], w.reads)
        kids = child_of(api, parent)
        c.check("#618 the real server creates the child job with the triple",
                len(kids) == 1
                and api.jobs[kids[0]]["Meta"].get("soul") == "puppet",
                list(api.jobs))
        c.check("#618 the echo is validated and the commandlet succeeds",
                w.code == 0, (w.code, w.seen[:1]))
    finally:
        g.__exit__(None, None, None)

    # ── 2. Роль обязательна для гранта: отказ ДО аллокации ────────────────
    # HYPOTHESIS: `add` без роли от гранта сегодня уехал бы на сервер,
    # который отказал бы уже после резерва; SOLUTION: ранний отказ
    # командлета до первого сетевого вызова.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, [], api, principal, grant=True)
        c.check("#618 grant add without a role refuses before any call",
                w.seen == [] and w.code not in (0, None), (w.code, w.seen))
    finally:
        g.__exit__(None, None, None)

    # ── 3. Человек: явная роль и прежний путь без изменений ───────────────
    # HYPOTHESIS: у человека add без роли -- байт в байт прежний вызов
    # ops.add с политикой; с --role -- та же политика плюс поля роли.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, [], api, "anton")
        add = w.sent("add")
        c.check("#618 human legacy add still sends the policy and no role",
                add.get("workspace") == PLAY
                and "soul" not in add and "request_id" not in add, add)
        w = drive(cli_add, ["--role", "master"], api, "anton")
        add = w.sent("add")
        c.check("#618 human add --role sends policy plus role fields",
                add.get("workspace") == PLAY and add.get("soul") == "master"
                and add.get("request_id"), add)
    finally:
        g.__exit__(None, None, None)

    # ── 4. Старый сервер (без протокола ролей) -- громкий отказ ───────────
    # HYPOTHESIS: `--role` против сервера без soul_protocol молча завёл бы
    # безролевого папета (роль уехала бы в никуда); SOLUTION: ping-запрос
    # (client.roles.protocol) и отказ до add.
    api = FakeNomad()
    g = soul_env(api, roles=False)
    try:
        root = g.__enter__()
        said = []
        with patched(cli_add.lib, fail=said.append):
            w = drive(cli_add, ["--role", "puppet"], api, "anton")
        c.check("#618 add --role on a legacy server refuses before add",
                not [f for v, f in w.seen if v == "add"] and w.code != 0,
                (w.code, w.seen))
    finally:
        g.__exit__(None, None, None)

    # ── 5. Запрещённая роль цели: отказ сервера доезжает до гранта ────────
    # HYPOTHESIS: spawn-allowlist проверяет сервер; SOLUTION: фронтенд не
    # дублирует список -- отказ сервера едет причиной отказа командлета.
    api, g, principal, parent = grant_env()
    try:
        w = drive(cli_add, ["--role", "master"], api, principal, grant=True)
        add = w.sent("add")
        c.check("#618 a forbidden target role is refused by the server",
                "soul" in add and len(api.jobs) == 1, (add, list(api.jobs)))
        c.check("#618 the refusal surfaces as the commandlet's failure",
                w.code != 0, w.code)
    finally:
        g.__exit__(None, None, None)

    # ── 6. MCP-объявление add: аргумент role ─────────────────────────────
    # HYPOTHESIS: MCP add принимает только origin; SOLUTION: args role
    # (флаг --role), маршрут -- тот же командлет.
    import json
    decl = cli_add.MCP
    args = {a["name"]: a for a in decl.get("args", [])}
    c.check("#618 the MCP add declares the role argument",
            args.get("role", {}).get("flag") == "--role", decl)
    c.check("#618 the MCP add route keeps ping for the preflight",
            "ping" in decl.get("cluster_verbs", []), decl)

    # ── 7. Update гранта: политика не читается и не едет ──────────────────
    # HYPOTHESIS: update командлет всегда читает политику рабочей копии и
    # везёт её; SOLUTION: preserve_workspace=True (#617) -- ни чтения, ни
    # полей; сервер держит положенную политику сам.
    api, g, principal, parent = grant_env()
    try:
        api.allocs[parent] = {"ID": "a1", "NodeName": "n1",
                              "ClientStatus": "running"}
        w = drive(cli_update, [parent, "--fresh"], api, principal, grant=True)
        up = w.sent("update")
        c.check("#618 grant update sends no policy fields",
                "workspace" not in up and "bootstrap_sent" not in up, up)
        c.check("#618 grant update reads no client policy",
                not [r for r in w.reads if r[0] == "update"], w.reads)
    finally:
        g.__exit__(None, None, None)

    # ── 8. Recycle гранта: рабочая копия не читается ──────────────────────
    # HYPOTHESIS: recycle всегда собирает workspace_of; SOLUTION: грант
    # зовёт puppets.recycle(workspace_of=None) -- контракт «рецикл без
    # рабочей копии», как у mop server gc.
    api, g, principal, parent = grant_env()
    try:
        api.allocs[parent] = {"ID": "a1", "NodeName": "n1",
                              "ClientStatus": "running"}
        w = drive(cli_recycle, [parent], api, principal, grant=True)
        up = w.sent("update")
        c.check("#618 grant recycle re-registers without policy fields",
                "workspace" not in up and "bootstrap_sent" not in up, up)
        c.check("#618 grant recycle reads no client policy",
                not [r for r in w.reads if r[0] == "recycle"], w.reads)
    finally:
        g.__exit__(None, None, None)

    return c.report("roleops")


if __name__ == "__main__":
    sys.exit(main())
