#!/usr/bin/env python3
"""Общий транспорт ролей без шины: python3 tests/transport.py

HYPOTHESIS (#603): ролям (#578) нужен один общий транспорт принципалов, а
клиент (#586) и канал (#585) держали бы каждый свою таблицу субъектов и
кредов: грант на человеческом rpc читался бы старым агентом как человек,
ответ узла летел бы в публичный _INBOX, сервер не отличил бы грант от
легаси-запроса, а каталог ролей собирался бы двумя разными таблицами.
SOLUTION: протокольный забор в busnames (канал role для грантов --
mop.<p>.node.<узел>.role.<grant-…> и mop.<p>.cluster.role.<grant-…>,
dispatch полным принципалом, квалифицированный server.rpc.node-<узел>,
приватные ответы _MOP_REPLY.<user>, канал startup своего узла),
bus.principal/caller_token/node_name, ops.add(soul=, request_id=) без
повторов, receives_dispatch в строке ростера с серверного MopSoul и корни
каталога ролей MOP_SOUL_DIRS (os.pathsep, доверенные внешние раньше
пакетных). Легаси-провода байт в байт прежние.
STATUS: RED
"""
import asyncio
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from _lib import Checks, patched, patched_env  # noqa: E402
from mop.common import bus, busnames, config, ops, puppets, service, soul  # noqa: E402

ID = "ab" * 16
GRANT = soul.grant_user(ID)            # полный принципал grant-<32 hex>
NODE_USER = busnames.node_user("hyper")


def group(fn):
    """Группа проверок без краша файла: недостающий символ -- провал группы
    (контракта нет), а не обрыв всего прогона."""
    def run(c):
        try:
            fn(c)
        except Exception as e:
            c.check(f"{fn.__name__}: the contract is implementable", False,
                    repr(e))
    return run


def matches(pattern, subj):
    """Матчер NATS по токенам: `*` -- один токен, `>` -- хвост."""
    p, s = pattern.split("."), subj.split(".")
    for i, tok in enumerate(p):
        if tok == ">":
            return len(s) >= i + 1
        if i >= len(s) or (tok != "*" and tok != s[i]):
            return False
    return len(s) == len(p)


def refused(c, what, fn, *a, **kw):
    """Вызов обязан отказать ValueError -- и не отказывает сейчас."""
    try:
        fn(*a, **kw)
    except ValueError:
        c.check(what, True)
    except Exception as e:                     # не тот отказ -- не забор
        c.check(what, False, repr(e))
    else:
        c.check(what, False, "no refusal")


class Recorder:
    """Подставной _ask: помнит субъекты, отвечает пустым словарём."""

    def __init__(self):
        self.subjects = []

    def __call__(self, subj, who, dead, verb, timeout, **fields):
        self.subjects.append(subj)
        return {}


def creds(user):
    return lambda: {"user": user, "url": "nats://x", "password": "p"}


def check_fence_603(c):
    """Грант не ходит по человеческому rpc: свой канал, полный принципал.

    Забор протокольный (#603): старый агент, подписанный только на
    `...rpc.*`, не должен получать запросы гранта и читать их как
    человеческие без резолвера -- смешанная раскатка."""
    c.expect("a grant rides the role channel, not human rpc",
             busnames.node("mop", "hyper", "rpc", login=GRANT),
             f"mop.mop.node.hyper.role.{GRANT}")
    c.expect("a cluster grant rides cluster.role",
             busnames.cluster("mop", login=GRANT),
             f"mop.mop.cluster.role.{GRANT}")
    # Люди и машины -- прежние субъекты байт в байт.
    c.expect("human rpc is unchanged",
             busnames.node("mop", "hyper", "rpc", login="alice"),
             "mop.mop.node.hyper.rpc.alice")
    c.expect("anonymous node rpc is unchanged",
             busnames.node("mop", "hyper", "rpc"), "mop.mop.node.hyper.rpc")
    c.expect("anonymous cluster is unchanged",
             busnames.cluster("mop"), "mop.mop.cluster.rpc")
    c.expect("human cluster is unchanged",
             busnames.cluster("mop", login="alice"), "mop.mop.cluster.rpc.alice")
    # Dispatch -- полным принципалом, голый hex на проводе не появляется.
    c.expect("dispatch carries the full principal",
             busnames.dispatch_subject("mop", "hyper", GRANT),
             f"mop.mop.node.hyper.dispatch.{GRANT}")
    refused(c, "a bare hex id is refused in dispatch subjects",
            busnames.dispatch_subject, "mop", "hyper", ID)
    # Новый субъект не совпадает ни с одним легаси-шаблоном подписок.
    role = busnames.node("mop", "hyper", "rpc", login=GRANT)
    for pattern in ("mop.*.node.*.rpc", "mop.*.node.*.rpc.*",
                    "mop.*.cluster.rpc", "mop.*.cluster.rpc.*"):
        c.check(f"fence: {role} must not match the legacy {pattern}",
                not matches(pattern, role))
    # Грант не ездит и по легаси-машинным каналам: только role и dispatch.
    refused(c, "a grant cannot ride the public msg channel",
            busnames.node, "mop", "hyper", "msg", login=GRANT)


def check_caller_603(c):
    """caller() читает новые субъекты полным принципалом; ограда форм."""
    c.expect("caller reads the node role channel",
             busnames.caller(f"mop.mop.node.hyper.role.{GRANT}"), GRANT)
    c.expect("caller reads cluster.role",
             busnames.caller(f"mop.mop.cluster.role.{GRANT}"), GRANT)
    c.expect("caller reads dispatch",
             busnames.caller(f"mop.mop.node.hyper.dispatch.{GRANT}"), GRANT)
    c.expect("caller reads the qualified server subject",
             busnames.caller(f"mop.mop.server.rpc.{NODE_USER}"), NODE_USER)
    # Квалифицированный server.rpc -- узловая штука: человек там не вызывающий.
    c.expect("a human token on server.rpc is not a caller",
             busnames.caller("mop.mop.server.rpc.alice"), None)
    c.expect("a malformed grant on the role channel is not a caller",
             busnames.caller("mop.mop.node.hyper.role.grant-ZZ"), None)
    c.expect("a bare hex on the dispatch channel is not a caller",
             busnames.caller(f"mop.mop.node.hyper.dispatch.{ID}"), None)
    # Забор с обеих сторон: грант, проехавший по человеческому rpc, не
    # читается человеком -- старый агент не примет его без резолвера.
    c.expect("a grant on the human rpc channel is no caller",
             busnames.caller(f"mop.mop.node.hyper.rpc.{GRANT}"), None)
    c.expect("a grant on the human cluster channel is no caller",
             busnames.caller(f"mop.mop.cluster.rpc.{GRANT}"), None)
    # Легаси-разбор байт в байт прежний.
    c.expect("human rpc caller is unchanged",
             busnames.caller("mop.mop.node.hyper.rpc.alice"), "alice")
    c.expect("anonymous caller is unchanged",
             busnames.caller("mop.mop.node.hyper.rpc"), None)
    c.expect("startup has no caller",
             busnames.caller("mop.mop.node.hyper.startup"), None)
    # Переход: без токена квалифицированный server-субъект -- прежний
    # анонимный, старый сервер отвечает ему и при смешанной раскатке.
    c.expect("without_caller strips the qualified server token",
             busnames.without_caller(f"mop.mop.server.rpc.{NODE_USER}"),
             "mop.mop.server.rpc")
    c.expect("without_caller strips the role token",
             busnames.without_caller(f"mop.mop.node.hyper.role.{GRANT}"),
             "mop.mop.node.hyper.role")


def check_principals_603(c):
    """Кто этот процесс на шине: login -- только человек, principal --
    только принципал роли, caller_token -- тот, кто оставляет токен."""
    with patched(bus, config=creds(GRANT)):
        c.expect("principal of a role process", bus.principal(), GRANT)
        c.expect("caller_token of a role process", bus.caller_token(), GRANT)
        c.expect("login stays human-only", bus.login(), None)
    with patched(bus, config=creds("alice")):
        c.expect("a human has no principal", bus.principal(), None)
        c.expect("a human's caller_token is the login", bus.caller_token(), "alice")
        c.expect("a human's login is the login", bus.login(), "alice")
        c.expect("a human is no node", bus.node_name(), None)
    for user in (NODE_USER, "service", "puppet-mop"):
        with patched(bus, config=creds(user)):
            c.expect(f"{user}: no principal", bus.principal(), None)
            c.expect(f"{user}: no caller token", bus.caller_token(), None)
    with patched(bus, config=creds(NODE_USER)):
        c.expect("node_name of a node process", bus.node_name(), "hyper")
    # Грамматика принципала -- одна: busnames и soul согласны.
    for user in (GRANT, "grant-ab", "grant-" + "AB" * 16, "grant-",
                 "puppet-mop", "alice", "", None):
        c.expect(f"busnames.is_grant agrees with soul for {user!r}",
                 busnames.is_grant(user), soul.is_grant(user))


def check_subject_routing_603(c):
    """bus.request строит субъект по принципалу процесса автоматически."""
    rec = Recorder()
    with patched(bus, config=creds(GRANT)), patched(bus, _ask=rec):
        bus.request("hyper", "state", project="mop")
        c.expect("a grant's rpc lands on the role channel", rec.subjects[-1],
                 f"mop.mop.node.hyper.role.{GRANT}")
        bus.request("hyper", "take", channel="dispatch", project="mop")
        c.expect("a grant's dispatch carries the full principal", rec.subjects[-1],
                 f"mop.mop.node.hyper.dispatch.{GRANT}")
        bus.call_cluster("pool", project="mop")
        c.expect("a grant's cluster call lands on cluster.role", rec.subjects[-1],
                 f"mop.mop.cluster.role.{GRANT}")
    with patched(bus, config=creds("alice")), patched(bus, _ask=rec):
        bus.request("hyper", "state", project="mop")
        c.expect("a human's rpc is byte-for-byte old", rec.subjects[-1],
                 "mop.mop.node.hyper.rpc.alice")
        bus.call_cluster("pool", project="mop")
        c.expect("a human's cluster call is byte-for-byte old", rec.subjects[-1],
                 "mop.mop.cluster.rpc.alice")
    with patched(bus, config=creds(NODE_USER)), patched(bus, _ask=rec):
        bus.request("hyper", "state", project="mop")
        c.expect("a node machine keeps the anonymous rpc", rec.subjects[-1],
                 "mop.mop.node.hyper.rpc")
    # Диспетчер -- принципал роли: человек канал dispatch не получает.
    with patched(bus, config=creds("alice")):
        refused(c, "a human cannot build a dispatch subject",
                bus.request, "hyper", "take", channel="dispatch", project="mop")


def check_private_603(c):
    """Приватные ответы машин: _MOP_REPLY.<user>, вне публичного _INBOX.>."""
    c.expect("the node's private prefix", busnames.private_prefix(NODE_USER),
             f"_MOP_REPLY.{NODE_USER}")
    c.expect("the grant's private prefix", busnames.private_prefix(GRANT),
             f"_MOP_REPLY.{GRANT}")
    c.expect("the node's private scope", busnames.private_scope(NODE_USER),
             f"_MOP_REPLY.{NODE_USER}.>")
    c.check("a node's own reply inbox is valid",
            busnames.valid_private_reply(f"_MOP_REPLY.{NODE_USER}.NUID", NODE_USER))
    c.check("a grant's own reply inbox is valid",
            busnames.valid_private_reply(f"_MOP_REPLY.{GRANT}.NUID", GRANT))
    c.check("someone else's private inbox is not the node's",
            not busnames.valid_private_reply(f"_MOP_REPLY.{GRANT}.NUID", NODE_USER))
    c.check("the public _INBOX is not a private reply",
            not busnames.valid_private_reply("_INBOX.abc.1", NODE_USER))
    c.check("an empty reply is not valid",
            not busnames.valid_private_reply("", NODE_USER))
    # Соединение узла и принципала отвечает приватным префиксом; люди,
    # сервисы и легаси-папеты остаются на _INBOX (проверенный connect-флаг
    # nats-py 2.16 inbox_prefix).
    c.expect("a node connects with its private inbox prefix",
             bus.auth({"user": NODE_USER, "url": "nats://x", "password": "p"})
             .get("inbox_prefix"), busnames.private_prefix(NODE_USER))
    c.expect("a grant connects with its private inbox prefix",
             bus.auth({"user": GRANT, "url": "nats://x", "password": "p"})
             .get("inbox_prefix"), busnames.private_prefix(GRANT))
    for user in ("alice", "service", "puppet-mop"):
        c.check(f"{user} keeps the public _INBOX",
                "inbox_prefix" not in bus.auth(
                    {"user": user, "url": "nats://x", "password": "p"}))


def check_server_qualified_603(c):
    """server.rpc с узловым токеном: сервер видит, ЧТО за узел, по субъекту;
    анонимный server.rpc остаётся легаси-путём."""
    c.expect("the qualified server subject",
             busnames.server("mop", caller=NODE_USER),
             f"mop.mop.server.rpc.{NODE_USER}")
    c.expect("the anonymous server subject is unchanged",
             busnames.server("mop"), "mop.mop.server.rpc")
    rec = Recorder()
    with patched(bus, config=creds(NODE_USER)), patched(bus, _ask=rec):
        bus.ask_server("bootstrap", project="mop")
        c.expect("a node's ask_server goes qualified", rec.subjects[-1],
                 f"mop.mop.server.rpc.{NODE_USER}")
    with patched(bus, config=creds("alice")), patched(bus, _ask=rec):
        bus.ask_server("bootstrap", project="mop")
        c.expect("a human's ask_server stays anonymous", rec.subjects[-1],
                 "mop.mop.server.rpc")


def check_startup_603(c):
    """Канал startup -- только своему узлу, и только узлу."""
    c.expect("the startup subject", busnames.startup_subject("mop", "hyper"),
             "mop.mop.node.hyper.startup")
    rec = Recorder()
    with patched(bus, config=creds(NODE_USER)), patched(bus, _ask=rec):
        bus.request("hyper", "initial_identity", channel="startup", project="mop")
        c.expect("own-node startup reaches the agent", rec.subjects[-1],
                 "mop.mop.node.hyper.startup")
        refused(c, "a node cannot ask another node's startup",
                bus.request, "mini", "initial_identity", channel="startup",
                project="mop")
    with patched(bus, config=creds(GRANT)):
        refused(c, "a grant cannot publish startup",
                bus.request, "hyper", "initial_identity", channel="startup",
                project="mop")
    with patched(bus, config=creds("alice")):
        refused(c, "a human cannot publish startup",
                bus.request, "hyper", "initial_identity", channel="startup",
                project="mop")


def check_add_fields_603(c):
    """ops.add везёт поля роли транспортом, легаси-провод не меняет, повторов
    не делает."""
    class Fake:
        def __init__(self, answers):
            self.answers, self.calls = answers, []

        def __call__(self, verb, **fields):
            self.calls.append((verb, fields))
            return self.answers.get(verb) or {}

    running = {"alloc": {"ClientStatus": "running",
                         "TaskStates": {"claude": {"State": "running"}}}}
    fake = Fake({"add": {"name": "pu-mop-1", "bootstrap_marker": None},
                 "alloc": running})
    v = ops.add(fake, "git@h:g/mop.git", ("text", "sent"),
                soul="master", request_id="r1")
    fields = dict(fake.calls[0][1])
    c.expect("the add call carries the role fields",
             (fields.get("soul"), fields.get("request_id")), ("master", "r1"))
    c.expect("a role add registers", v.get("outcome"), ops.REGISTERED)
    # Легаси-вызов без роли: тех же ключей на проводе нет -- байт в байт.
    plain = Fake({"add": {"name": "pu-mop-1", "bootstrap_marker": None},
                  "alloc": running})
    ops.add(plain, "git@h:g/mop.git", ("text", "sent"))
    c.check("a legacy add carries no role keys",
            not ("soul" in plain.calls[0][1] or "request_id" in plain.calls[0][1]),
            plain.calls[0][1])
    c.expect("one add call, no retries inside ops.add",
             [v for v, _ in plain.calls if v == "add"], ["add"])


def check_row_dispatch_603(c):
    """receives_dispatch в строке ростера -- с серверного MopSoul, без
    местной политики; легаси-провод без ключа."""
    def item(mop_soul=None):
        job = {"ID": "pu-mop-1", "Status": "running",
               "Meta": {"origin": "git@h:g/mop.git", "soul": "master",
                        "soul_digest": "ab" * 32,
                        "soul_inc": "01234567-89ab-cdef-0123-456789abcdef"}}
        if mop_soul is not None:
            job["MopSoul"] = mop_soul
        return {"job": job, "alloc": None, "error": None,
                "state": "free", "kind": "free", "owner": None}

    row = puppets._row(item({"receives_dispatch": True}))
    c.expect("the row carries receives_dispatch from the server",
             row.receives_dispatch, True)
    c.expect("the wire carries the flag",
             row.to_dict().get("receives_dispatch"), True)
    row = puppets._row(item({"receives_dispatch": False}))
    c.expect("a non-target role carries False",
             row.to_dict().get("receives_dispatch"), False)
    legacy = puppets._row(item()).to_dict()
    c.check("a legacy row carries no receives_dispatch key",
            "receives_dispatch" not in legacy, legacy)
    # Данные, не политика: что сервер прислал, то и в строке.
    row = puppets._row(item({"receives_dispatch": "yes"}))
    c.expect("no local evaluation of the server's fact",
             row.receives_dispatch, "yes")


def check_roots_603(c):
    """Корни каталога ролей -- одна таблица: MOP_SOUL_DIRS (os.pathsep,
    доверенные внешние раньше), затем каталог пакета."""
    try:
        from mop.common import souldirs
    except ImportError as e:
        c.check(f"mop.common.souldirs exists ({e})", False)
        return
    c.check("MOP_SOUL_DIRS is a declared setting",
            "MOP_SOUL_DIRS" in config.SETTINGS)
    ext = os.pathsep.join(("/srv/roles", "/opt/roles"))
    with patched_env(MOP_SOUL_DIRS=ext):
        c.expect("external roots keep their order, the package is last",
                 souldirs.roots(), ["/srv/roles", "/opt/roles",
                                    souldirs.PACKAGE_DIR])
    with patched_env(MOP_SOUL_DIRS=""):
        c.expect("without external roots the package catalogue stands alone",
                 souldirs.roots(), [souldirs.PACKAGE_DIR])
    c.check("the package catalogue is the shipped skills directory",
            os.path.basename(os.path.dirname(souldirs.PACKAGE_DIR)) == "mop"
            and os.path.isdir(souldirs.PACKAGE_DIR), souldirs.PACKAGE_DIR)


def check_service_facts_603(c):
    """Каркас ставит транспортные факты _reply/_subject настоящим значением
    ПОСЛЕ trusted(): тело их не подделывает."""
    seen = []

    async def h(project, req, send):
        seen.append(req)
        return {"ok": True}

    body = b'{"verb": "ping", "_reply": "_INBOX.forged", ' \
           b'"_subject": "mop.mop.cluster.rpc.forged"}'
    asyncio.run(service.answer("t", "mop", body, h,
                               lambda p, r, o: [], lambda s: None,
                               lambda **ev: None, caller="alice",
                               reply=f"_MOP_REPLY.{NODE_USER}.1",
                               subject=f"mop.mop.cluster.role.{GRANT}"))
    c.check("the framework stamps the real reply subject",
            seen and seen[-1].get("_reply") == f"_MOP_REPLY.{NODE_USER}.1", seen)
    c.check("the framework stamps the real request subject",
            seen and seen[-1].get("_subject") == f"mop.mop.cluster.role.{GRANT}",
            seen)
    # Без параметров -- прежний прямой вызов проверок: ключей нет вовсе.
    seen.clear()
    asyncio.run(service.answer("t", "mop", b'{"verb": "ping"}', h,
                               lambda p, r, o: [], lambda s: None,
                               lambda **ev: None, caller="alice"))
    c.check("direct legacy calls carry no transport facts",
            seen and "_reply" not in seen[-1] and "_subject" not in seen[-1], seen)


def main():
    c = Checks()
    for fn in map(group, (check_fence_603, check_caller_603,
                          check_principals_603, check_subject_routing_603,
                          check_private_603, check_server_qualified_603,
                          check_startup_603, check_add_fields_603,
                          check_row_dispatch_603, check_roots_603,
                          check_service_facts_603)):
        fn(c)
    return c.report("transport")


if __name__ == "__main__":
    sys.exit(main())
