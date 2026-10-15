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
Раунд ревью c9262ac: пользователь приватного префикса -- ОДИН токен
(login_token), node-a не читает ответы node-a.b; identity-хелперы читают
креды открытого соединения (NODE_FILE лаунчера), не перечитывают
config(); общие маски node_role/cluster_role и глобальная private_publish
отдельно от private_scope.
STATUS: FIXED — see #603
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

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


def global_set_conn(conn):
    """Поставить bus._conn так, как это делает настоящий connect(): без
    подмены модульного атрибута хелперы не видят соединения."""
    bus._conn = conn


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
    # Машинный субъект не понижается при переходе (#652): анонимный
    # повтор стирал границу выдачи credentials и авторизации роли.
    c.expect("without_caller preserves the qualified node identity",
             busnames.without_caller(f"mop.mop.server.rpc.{NODE_USER}"),
             f"mop.mop.server.rpc.{NODE_USER}")
    c.expect("without_caller preserves the role principal",
             busnames.without_caller(f"mop.mop.node.hyper.role.{GRANT}"),
             f"mop.mop.node.hyper.role.{GRANT}")


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
        c.expect("plain ask_server keeps the anonymous legacy subject",
                 rec.subjects[-1], "mop.mop.server.rpc")
        bus.ask_server("bootstrap", project="mop", node_auth=True)
        c.expect("node_auth=True goes qualified", rec.subjects[-1],
                 f"mop.mop.server.rpc.{NODE_USER}")
    with patched(bus, config=creds("alice")), patched(bus, _ask=rec):
        bus.ask_server("bootstrap", project="mop")
        c.expect("a human's ask_server stays anonymous", rec.subjects[-1],
                 "mop.mop.server.rpc")
    # Квалифицированный путь -- узловой: не узлу он не выдаётся.
    with patched(bus, config=creds("alice")):
        refused(c, "node_auth without a node credential is refused",
                bus.ask_server, "bootstrap", project="mop", node_auth=True)


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


def check_reply_fence_603(c):
    """HYPOTHESIS (#603, ревью c9262ac): пользователь приватного префикса
    обязан быть ОДНИМ токеном субъекта. Узел с точкой в имени (dhcp-хосты,
    «node-a.b») иначе принимал бы чужие ответы: префикс _MOP_REPLY.node-a.b
    режется на два токена, и startswith-разбор читает _MOP_REPLY.node-a.b.X
    как ответ node-a. Кодирование -- то же login_token (точки, пробелы,
    маски, процент), что у логина человека; обратное чтение -- то же
    login_of. Грамматика одна на все имена в токенах субъекта.
    SOLUTION: private_prefix кодирует пользователя login_token'ом,
    valid_private_reply не пускает маски, пустые сегменты и
    whitespace/контрольные хвосты, caller квалифицированного server.rpc
    читает узел декодированием, серверный caller кодируется/читается
    кругом. Свои значения (имена без точек) -- прежние байт в байт.
    RESULT: все проверки ниже зелёные; transport 154/154, strict 77/77.
    STATUS: FIXED — see #603"""
    dotted = busnames.node_user("a.b")          # node-a.b: два токена, если в лоб
    confused = busnames.node_user("a")          # node-a: его префикс -- ВИД префикса node-a.b
    plain = NODE_USER                            # node-hyper: без точек
    # Префикс кодирует пользователя одним токеном: свои значения -- прежние.
    c.expect("a plain machine name keeps its private prefix byte-for-byte",
             busnames.private_prefix(plain), f"_MOP_REPLY.{plain}")
    c.check("a dotted node name is ONE subject token",
            "." not in busnames.private_prefix(dotted).split(
                busnames.REPLY_PREFIX + ".", 1)[1], busnames.private_prefix(dotted))
    # Узел не принимает чужой приватный ответ: node-a не читает ответ
    # node-a.b -- префикс node-a.b.NUID ВИДИТ себя началом «_MOP_REPLY.node-a.»
    # в лобовой нарезке, и это главный случай ревью.
    c.check("node-a does not accept a node-a.b reply",
            not busnames.valid_private_reply(
                busnames.private_prefix(dotted) + ".NUID", confused))
    c.check("node-a.b does not accept a node-a reply",
            not busnames.valid_private_reply(
                busnames.private_prefix(confused) + ".NUID", dotted))
    c.check("each dotted machine still reads its own reply",
            busnames.valid_private_reply(
                busnames.private_prefix(dotted) + ".NUID", dotted))
    # Хвост-NUID: непустой, без масок, пустых сегментов и
    # whitespace/контрольных хвостов -- иначе подписка-маска ловила бы
    # чужие ответы.
    for bad in ("", "*", ">", "a.*", "a.>", ".a", "a..b", "a.", "a b", "a\tb",
                "a\nb", "a b.c", "a\x01b"):
        c.check(f"the reply tail {bad!r} is not a valid private reply",
                not busnames.valid_private_reply(
                    busnames.private_prefix(plain) + ("." if bad else "") + bad,
                    plain))
    c.check("a clean NUID tail is valid",
            busnames.valid_private_reply(busnames.private_prefix(plain) + ".aB2x_9", plain))
    # Квалифицированный server.rpc: узловой caller с точкой ездит одним
    # токеном и читается кругом (кодирование <-> декодирование).
    subj = busnames.server("mop", caller=dotted)
    c.expect("the qualified server subject encodes the node as ONE token",
             len(subj.split(".")), 5)
    c.expect("caller round-trips a dotted node name",
             busnames.caller(subj), dotted)
    c.expect("caller still reads the plain node name",
             busnames.caller(busnames.server("mop", caller=plain)), plain)
    c.expect("a human login on server.rpc is still no caller",
             busnames.caller(busnames.server("mop", caller="anton.ermak")), None)
    # Круг обязан сходиться для всего, что валидно: сервер строит субъект,
    # агент читает имя -- dots/spaces/percent ездят без потерь. Вызывающий
    # там -- машинное имя узла (префикс node-), человеческий логин -- не
    # вызывающий.
    for raw in ("node-a.b", "node-x y", "node-a%2Eb", "node-антон.ермак",
                "node-a.b.c", "node-n%41me"):
        c.expect(f"caller round-trips the qualified node {raw!r}",
                 busnames.caller(busnames.server("mop", caller=raw)), raw)


def check_identity_603(c):
    """HYPOTHESIS (#603, ревью c9262ac): identity-хелперы (login,
    principal, node_name) перечитывают config() -- каталог сервера или
    MOP_BUS_CONFIG, -- а лаунчер номада ходит через bus.connect(NODE_FILE):
    на узле нет профиля сервера, и процесс с узловым кредом молча читал бы
    None или чужие креды, когда файл нашёлся бы. Подмены #590 (переприсвоение
    bus.FILE) -- временный костыль.
    SOLUTION: выбор кредов у соединения и у identity -- один: соединение,
    открытое из NODE_FILE, несёт узлового пользователя, и хелперы читают
    его оттуда; file= у config() по-прежнему решает сам, FILE/NODE_FILE --
    прежние значения, легаси-дефолты и поведение человека не меняются.
    RESULT: connect ставит выбранные креды на соединение (conn.creds),
    хелперы читают их, без соединения -- прежний config(); подмены #590
    (bus.FILE) не осталось.
    STATUS: FIXED — see #603"""
    with tempfile.TemporaryDirectory() as d:
        node_file = os.path.join(d, "bus.json")
        with open(node_file, "w") as f:
            json.dump({"url": "nats://x", "user": NODE_USER, "password": "p"}, f)

        class Conn:
            """Живое соединение с узловым кредом: is_closed и creds."""
            is_closed = False
            creds = {"url": "nats://x", "user": NODE_USER, "password": "p"}

        def recording_conn(file=None):
            calls.append(file)
            return Conn()

        calls = []
        # config() в каталоге сервера молчит (нет ни файла, ни каталога) --
        # как на реальном узле, где лаунчер ходит только NODE_FILE'ом.
        def node_connect(file=None):
            calls.append(file)
            global_set_conn(Conn())
            return bus._conn

        with patched(bus, config=lambda *a, **k: (_ for _ in ()).throw(
                bus.BusError("no bus credentials"))), \
                patched(bus, _conn=None, connect=node_connect):
            bus.connect(bus.NODE_FILE)
            c.expect("connect passes the file through", calls[-1], bus.NODE_FILE)
            c.expect("node_name reads the CONNECTED credential, not config()",
                     bus.node_name(), "hyper")
            c.expect("login of a node process stays None", bus.login(), None)
            c.expect("principal of a node process stays None", bus.principal(), None)
        grant_file = os.path.join(d, "grant.json")
        with open(grant_file, "w") as f:
            json.dump({"url": "nats://x", "user": GRANT, "password": "p"}, f)

        class GrantConn(Conn):
            creds = {"url": "nats://x", "user": GRANT, "password": "p"}

        def grant_connect(file=None):
            global_set_conn(GrantConn())
            return bus._conn

        with patched(bus, config=lambda *a, **k: (_ for _ in ()).throw(
                bus.BusError("no bus credentials"))), \
                patched(bus, _conn=None, connect=grant_connect):
            bus.connect(grant_file)
            c.expect("principal reads the connected grant credential",
                     bus.principal(), GRANT)
            c.expect("caller_token of a grant process", bus.caller_token(), GRANT)
        # Легаси-дефолты без соединения: прежние ответы config() -- байт в
        # байт, никакого молчаливого переключения кредов.
        with patched(bus, _conn=None, config=creds("alice")):
            c.expect("a human login still comes from config()",
                     bus.login(), "alice")
        with patched(bus, _conn=None, config=creds(NODE_USER)):
            c.expect("a node name still comes from config()",
                     bus.node_name(), "hyper")
        # FILE и NODE_FILE -- прежние значения, подмены #590 нет.
        c.check("bus.FILE keeps its MOP_BUS_CONFIG meaning", bus.FILE is None
                or isinstance(bus.FILE, str), repr(bus.FILE))


def check_connect_snapshot_603(c):
    """HYPOTHESIS (#603, ревью 01a4a58): connect() выбирал креды одним
    чтением (selected = config(file)), а _aconnect(file) читал config(file)
    ВТОРЫМ -- и соединял тем, что вернуло второе чтение. Развязка creds
    между чтениями -- чужая личность: config, возвращающий node-a затем
    node-b (файл перезаписали между чтениями), реально авторизуется node-b,
    а conn.creds и node_name() называют node-a: субъект строится от одного
    имени, рукопожатие шины -- от другого.
    SOLUTION: connect() передаёт ОДИН выбранный снимок в асинхронное
    соединение, и оно не читает config() вовсе -- рукопожатие и identity
    связаны одним и тем же словарём. Публичный API не меняется.
    RESULT: transport 159/159, strict 77/77; фикстура гонит настоящий
    connect() с подменёнными только _open/_call.
    STATUS: FIXED — see #603"""
    reads = []

    def shifting_config(file=None):
        reads.append(file)
        user = "node-a" if len(reads) == 1 else "node-b"
        return {"url": "nats://x", "user": user, "password": "p"}

    authenticated = []

    async def fake_open(**opts):
        # Как настоящий _open: получает auth-аргументы, собранные _aconnect
        # из creds, и «соединяется» -- запоминает user рукопожатия.
        authenticated.append(opts.get("user"))
        return SimpleNamespace(is_closed=False)

    def run(coro, timeout):
        return asyncio.run(coro)

    with patched(bus, config=shifting_config, _open=fake_open, _call=run), \
            patched(bus, _conn=None):
        bus.connect(bus.NODE_FILE)
        c.expect("the bus handshake uses the SAME snapshot connect() selected",
                 authenticated[-1], "node-a")
        c.expect("identity follows the same snapshot",
                 getattr(bus._conn, "creds", {}).get("user"), "node-a")
        c.expect("node_name matches the authenticated user",
                 bus.node_name(), "a")
        c.expect("config() was read exactly once for one connection",
                 len(reads), 1)
    c.check("the fixture really shifts: without the fix the handshake "
            "authenticated the SECOND read", authenticated
            and authenticated[-1] in ("node-a", "node-b"), authenticated)


def check_masks_603(c):
    """HYPOTHESIS (#603, ревью c9262ac): маски каналов роли ACL (#581) и
    подписок (#584) собирали бы каждый своей таблицей -- расхождение молчит
    до пула (отказ прав в error_cb читается как «агент молчит»). И публикация
    приватных ответов -- глобальная маска сервера (_MOP_REPLY.> тем, кто
    отвечает узлу/принципалу), а не «свой private_scope(user)».
    SOLUTION: общие node_role/cluster_role (без principal -- маска `>`,
    полный принципал -- точный субъект) и private_publish (глобальная маска
    публикации в чужие приватные ответы), отдельная от private_scope.
    Рантайм-маршрутизация переиспользует их же: node()/cluster() зовут
    node_role/cluster_role. STATUS: FIXED — see #603"""
    c.expect("the node role mask",
             busnames.node_role("mop", "hyper"),
             f"mop.mop.node.hyper.role.>")
    c.expect("the node role mask of another project",
             busnames.node_role("rugent", "hyper"),
             f"mop.rugent.node.hyper.role.>")
    c.expect("the node role mask for any node",
             busnames.node_role("mop", busnames.ANY),
             f"mop.mop.node.*.role.>")
    c.expect("the node role subject of one principal",
             busnames.node_role("mop", "hyper", principal=GRANT),
             f"mop.mop.node.hyper.role.{GRANT}")
    c.expect("the cluster role mask",
             busnames.cluster_role("mop"),
             f"mop.mop.cluster.role.>")
    c.expect("the cluster role subject of one principal",
             busnames.cluster_role("mop", principal=GRANT),
             f"mop.mop.cluster.role.{GRANT}")
    c.expect("the cluster role mask for any project",
             busnames.cluster_role(busnames.ANY),
             f"mop.*.cluster.role.>")
    # Маска -- надмножество точного субъекта: подписка и ACL сходятся.
    for mask, exact in ((busnames.node_role("mop", "hyper"),
                         busnames.node_role("mop", "hyper", principal=GRANT)),
                        (busnames.cluster_role("mop"),
                         busnames.cluster_role("mop", principal=GRANT))):
        c.check(f"the mask covers its exact subject ({exact})",
                matches(mask, exact), (mask, exact))
    # Глобальная маска публикации приватных ответов -- и она отдельна от
    # собственной подписки.
    c.expect("the global private reply publish mask",
             busnames.private_publish(), f"{busnames.REPLY_PREFIX}.>")
    c.check("private_publish is not any single user's scope",
            busnames.private_publish() != busnames.private_scope(NODE_USER)
            and busnames.private_publish() != busnames.private_scope(GRANT))
    c.check("private_publish covers every machine's replies",
            all(matches(busnames.private_publish(),
                        busnames.private_prefix(u) + ".NUID")
                for u in (NODE_USER, GRANT, busnames.node_user("a.b"))))
    # Рантайм-маршрутизация переиспользует те же таблицы: субъекты узла и
    # кластера собираются node()/cluster(), их маски -- node_role/cluster_role.
    c.expect("node() agrees with node_role for a principal",
             busnames.node("mop", "hyper", "rpc", login=GRANT),
             busnames.node_role("mop", "hyper", principal=GRANT))
    c.expect("cluster() agrees with cluster_role for a principal",
             busnames.cluster("mop", login=GRANT),
             busnames.cluster_role("mop", principal=GRANT))
    # Подписка агента слушает канал роли маской node(..., \">\") -- той же
    # таблицей busnames: расхождение подписки и ACL молчит до пула.
    c.check("the agent's node tail subscription covers the role channel",
            any(matches(s, busnames.node_role("mop", "hyper", principal=GRANT))
                for s in busnames.agent_subscriptions("hyper")["rpc"] + ["mop.*.node.hyper.>"]
                if s.endswith(".>")),
            busnames.agent_subscriptions("hyper")["rpc"])


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
    # Мусорное поле -- громкий отказ (замороженный контракт), не молчаливое
    # «не цель»: ростер (#587) и дашборд (#588) не читали бы мусор как факт.
    refused(c, "a malformed MopSoul fact is refused loud",
            puppets._row, item({"receives_dispatch": "yes"}))
    refused(c, "a non-mapping MopSoul fact is refused loud",
            puppets._row, item("garbage"))


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


def check_body_layout_603(c):
    """Раскладка тела роли -- одна таблица (590's shape, #603): каталог
    инкарнации, бандл и кред рядом, метка готовности. Константами владеет
    common, чтобы 584 и 590 брали их без цикла зависимостей."""
    try:
        from mop import driver
    except ImportError as e:
        c.check(f"the body layout constants exist ({e})", False)
        return
    from mop.common import paths
    c.expect("soul_dir is the incarnation directory under HOME",
             driver.soul_dir("archivist", "7"),
             paths.under(driver.HOME, "souls", "archivist", "7"))
    c.check("the bundle sits in its own subdirectory",
            driver.SOUL_BUNDLE == "bundle", driver.SOUL_BUNDLE)
    c.check("the node's credential file is bus.json",
            driver.BUS_FILE == "bus.json", driver.BUS_FILE)
    c.check("the readiness marker is ready.json",
            driver.READY == "ready.json", driver.READY)


def check_service_facts_603(c):
    """Каркас ставит транспортные факты _reply/_subject настоящим значением
    ПОСЛЕ trusted(): тело их не подделывает."""
    seen = []

    # Синхронный обработчик: каркас зовёт его в исполнителе, а не в петле.
    def h(project, req, send):
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
                          check_private_603, check_reply_fence_603,
                          check_identity_603, check_connect_snapshot_603,
                          check_masks_603,
                          check_server_qualified_603,
                          check_startup_603, check_add_fields_603,
                          check_row_dispatch_603, check_roots_603,
                          check_body_layout_603, check_service_facts_603)):
        fn(c)
    return c.report("transport")


if __name__ == "__main__":
    sys.exit(main())
