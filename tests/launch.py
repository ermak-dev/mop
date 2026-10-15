#!/usr/bin/env python3
"""Лаунчер ролей без пула: python3 tests/launch.py

`mop <роль>` поднимает нового папета с ролью и подключает к нему терминал
(#586, docs/SOUL.md «CLI, имена, упаковка»). Пул здесь заменён заглушками
сервиса кластера, шины и ssh: настоящий ops.add идёт по фальшивому
транспорту, и проверяется то, что уходит по проводу, и что видит человек.
Живой подъём роли -- только на пуле (сервер #582, обёртка #583).
"""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, ROOT)

from mop import cli  # noqa: E402
from mop.cli import lib  # noqa: E402
from mop.cli.core import _common, attach, master  # noqa: E402
from mop.cli.pool import login  # noqa: E402
from mop.client import roles  # noqa: E402
from mop.common import bus, config, context, ops, puppets  # noqa: E402

ORIGIN = "git@h:g/mop.git"
NAME = "pu-mop-3"
NODE = "hyper"
DIGEST = "0" * 64
INC = "00000000-0000-4000-8000-000000000000"
# Ответ ping сервера с ролями (#582): версия протокола и сведение повторов.
PROTOCOL = {"version": 1, "request_id": True}


def raises(fn, *a, **kw):
    """Вызов поднял ValueError (или названный тип / подстроку)? -> строка
    неудачи или False (поднял ожидаемое). Не поднял -- тоже неудача."""
    want_type = kw.pop("want_type", ValueError)
    want_text = kw.pop("want_text", None)
    try:
        fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 -- проверка исключения
        if not isinstance(e, want_type):
            return f"{type(e).__name__}: {e}"
        if want_text and want_text not in str(e):
            return f"{type(e).__name__}: {e}"
        return False
    return "no exception"


def role_dir(root, role, prompt="soul.md", extra=None):
    """Каталог роли во временном каталоге -> путь."""
    d = os.path.join(root, role)
    os.makedirs(d)
    with open(os.path.join(d, "soul.yaml"), "w") as f:
        f.write(f"soul: {role}\nversion: 1\n"
                "runtime: {driver: any, receives_dispatch: true}\n"
                "capabilities: {node_verbs: [send], cluster_verbs: [],"
                " dispatch: false, spawn: []}\n"
                f"prompt: {prompt}\n" + (extra or ""))
    with open(os.path.join(d, prompt), "w") as f:
        f.write("You are a role.\n")
    return d


class Pool:
    """Сервис кластера, узел и ssh в одном: записывает, что спросили.

    add -- ответ регистрации (эхо роли сервера #582); alloc -- сразу
    running; states -- present после ready_after опросов; ssh -- код."""

    def __init__(self, echo=True, refuse=None, timeouts=0, ready_after=0,
                 ssh_rc=0, gone=False, bootstrap=None, protocol=PROTOCOL,
                 marker="match"):
        self.calls, self.adds, self.ssh = [], [], []
        self.echo, self.refuse, self.timeouts = echo, refuse, timeouts
        self.ready_after, self.ssh_rc, self.gone = ready_after, ssh_rc, gone
        self.bootstrap, self.protocol, self.marker = bootstrap, protocol, marker
        self.polls = 0
        self.triple = None

    def cluster(self, verb, timeout=None, project=None, **fields):
        self.calls.append(verb)
        if verb == "ping":
            return {"soul_protocol": self.protocol} if self.protocol else {}
        if verb == "add":
            self.adds.append(fields)
            if self.timeouts:
                self.timeouts -= 1
                raise bus.BusError("cluster: no answer in 30s")
            if self.refuse:
                raise bus.Refused(self.refuse)
            got = {"name": NAME}
            if self.bootstrap is not None:
                got["bootstrap_marker"] = "m-1"
            self.triple = {"soul": fields.get("soul"), "soul_digest": DIGEST,
                           "soul_inc": INC}
            if self.echo is True:
                got.update(self.triple)
            elif self.echo == "partial":
                got["soul"] = fields.get("soul")
            return got
        if verb == "alloc":
            return {"alloc": {"ID": "a-1", "JobID": NAME, "NodeName": NODE,
                              "ClientStatus": "running"}, "driver": "host"}
        if verb == "bootstrap_result":
            return {"result": {"sent": "m-1", **self.bootstrap}}
        raise AssertionError(f"unexpected cluster verb {verb}")

    def states(self, verb, nodes, timeout=None, **kw):
        self.calls.append(verb)
        self.polls += 1
        present = not self.gone and self.polls > self.ready_after
        facts = {"present": present}
        # Факт узла (#584): вложенный soul с ключом role, как маркер #590.
        if present and self.marker == "match":
            facts["soul"] = {"ready": True, "role": (self.triple or {}).get("soul"),
                             **{k: v for k, v in (self.triple or {}).items()
                                if k != "soul"}}
        elif present and self.marker == "other":
            facts["soul"] = {"ready": True,
                             "role": (self.triple or {}).get("soul"),
                             **{k: v for k, v in (self.triple or {}).items()
                                if k not in ("soul", "soul_digest")},
                             "soul_digest": "f" * 32}
        return {node: {"puppets": {NAME: facts}} for node in nodes}

    def run_ssh(self, argv):
        self.ssh.append(argv)
        return self.ssh_rc


def launch(argv, pool, tty=True, catalogue_dirs=None, login_calls=None):
    """Прогнать `mop <роль> …` целиком через диспетчер -> (out, err, code).

    Подмены -- только того, что есть и без лаунчера: шина, терминал (os),
    ssh (subprocess), часы (time), вход, рабочая копия. Так проверка
    краснеет поведением прежнего диспетчера, а не отказом импорта."""
    clock = [0.0]

    def sleep(s):
        clock[0] += s

    def ensure():
        (login_calls if login_calls is not None else []).append("login")

    def call(argv, *a, **kw):
        return pool.run_ssh(list(argv))

    from mop.common import souldirs
    dirs = [*(catalogue_dirs or []), *souldirs.roots()]
    with patched(souldirs, roots=lambda: list(dirs)), \
            patched(os, isatty=lambda fd: tty), \
            patched(subprocess, call=call), \
            patched(time, sleep=sleep, time=lambda: clock[0]), \
            patched(bus, call_cluster=pool.cluster, request_many=pool.states), \
            patched(lib, origin=lambda *a: ORIGIN, project_ready=lambda *a: True), \
            patched(login, ensure=ensure), \
            patched(_common, workspace_text=lambda o: ("", {"present": False})), \
            patched(config, require=lambda *a: None), \
            context.use(context.resolve({"server": "srv.test", "user": "alice"},
                                        {}, {})):
        return run_command(cli.main, argv, via_cli=True)


# HYPOTHESIS: каталог клиента знал только пакетный каталог -- внешний
# корень MOP_SOUL_DIRS (#603) молча не виден, и роль установки не
# запускается, пока сервер её видит.
# SOLUTION: таблица корней -- общая (souldirs.roots), клиент не держит
# своей; приоритет -- настройка раньше пакета.
def check_catalogue_roots_586(c):
    from mop.common import souldirs
    with tempfile.TemporaryDirectory() as root:
        role_dir(root, "installed")
        before = souldirs.roots()
        with patched_env(MOP_SOUL_DIRS=root):
            c.expect("#586 external roots lead the client catalogue",
                     roles.dirs(), [root, *before])
        c.expect("#586 without the setting the package catalogue stands alone",
                 roles.dirs(), before)


# HYPOTHESIS: `mop <роль>` не знает ролей -- диспетчер отвечает «unknown
# command», и ни одна роль каталога не запускается.
# SOLUTION: незнакомое команде имя ищется в каталоге ролей; найденное идёт
# в общий лаунчер с ролью первым аргументом.
def check_role_dispatch_586(c):
    pool = Pool()
    out, err, code = launch(["builder"], pool)
    c.expect("#586 mop builder succeeds", (code, err), (0, ""))
    c.expect("#586 one registration", len(pool.adds), 1)
    add = pool.adds[0] if pool.adds else {}
    c.expect("#586 the role travels to the server", add.get("soul"), "builder")
    c.expect("#586 origin is preserved", add.get("origin"), ORIGIN)
    c.check("#586 branch of the working copy is sent", "branch" in add, add)
    c.check("#586 the output names the puppet", NAME in out, out)

    pool = Pool()
    out, err, code = launch(["no-such-role"], pool)
    c.expect("#586 an unknown name is still an unknown command", code, 1)
    c.check("#586 unknown command names help", "mop help" in err, err)
    c.expect("#586 an unknown name allocates nothing", pool.calls, [])

    pool = Pool()
    out, err, code = launch(["Builder"], pool)
    c.expect("#586 a malformed name allocates nothing", (code, pool.calls), (1, []))


# HYPOTHESIS: подъём считается готовым по running-аллокации, и терминал
# подключается к телу, в котором ещё нет tmux-сессии.
# SOLUTION: после ops.add (bootstrap ok) ждём present в узловых states, и
# только потом ssh; таймаут -- ошибка с именем, без второго создания.
def check_readiness_586(c):
    pool = Pool(ready_after=3)
    out, err, code = launch(["builder"], pool)
    c.expect("#586 waits for the session before attaching", (code, err), (0, ""))
    c.check("#586 states polled until present", pool.polls == 4, pool.polls)
    c.expect("#586 attaches once", len(pool.ssh), 1)
    if pool.ssh:
        argv = pool.ssh[0]
        c.expect("#586 ssh with a tty to the node", argv[:3], ["ssh", "-t", NODE])
        c.check("#586 ssh enters the puppet's tmux", "tmux" in argv[3] and NAME in argv[3],
                argv)
    c.check("#586 states come after the running allocation",
            pool.calls.index("states") > pool.calls.index("alloc")
            if "states" in pool.calls and "alloc" in pool.calls else False, pool.calls)

    pool = Pool(gone=True)
    out, err, code = launch(["builder"], pool)
    c.expect("#586 readiness timeout fails", code, 1)
    c.check("#586 timeout names the puppet and the way back",
            NAME in err and f"mop attach {NAME}" in err and "session" in err, err)
    c.expect("#586 timeout does not attach", pool.ssh, [])
    c.expect("#586 timeout does not create again", len(pool.adds), 1)

    # tmux без сверенного бандла -- не готовность: в теле может стоять
    # прежняя роль или bootstrap без неё.
    for marker in ("none", "other"):
        pool = Pool(marker=marker)
        out, err, code = launch(["builder"], pool)
        c.expect(f"#586 session without the role marker ({marker}) is not ready", code, 1)
        c.check(f"#586 marker timeout ({marker}) names the puppet", NAME in err, err)
        c.expect(f"#586 marker timeout ({marker}) does not attach", pool.ssh, [])


# HYPOTHESIS: готовность читалась по плоским ключам факта -- узел #584
# отдаёт вложенный soul-факт {ready, role, soul_inc, soul_digest}, и
# лаунчер не видел ни готовности, ни причины.
# SOLUTION: session_ready читает вложенный факт и сверяет тройку с эхом
# регистрации; ключа нет -- сессия ещё не роль (обёртка не сверила бандл).
def check_readiness_shape_586(c):
    def facts(soul=None, present=True):
        f = {"present": present}
        if soul is not None:
            f["soul"] = soul
        return {NODE: {"puppets": {NAME: f}}}

    triple = {"soul": "builder", "soul_digest": DIGEST, "soul_inc": INC}

    def soul_fact(role="builder", digest=DIGEST, inc=INC):
        # Факт узла (#584): {ready, role, soul_inc, soul_digest}; имя роли
        # -- ключом role, как в маркере #590, а не soul, как в эхе add.
        return {"ready": True, "role": role, "soul_digest": digest,
                "soul_inc": inc}

    c.check("#586 the nested role fact is readiness",
            roles.session_ready(facts(soul_fact()), NODE, NAME, triple), True)
    c.check("#586 readiness needs the nested fact at all",
            roles.session_ready(facts(None), NODE, NAME, triple) is False)
    c.check("#586 the node's refusal is loud, not a silent wait",
            raises(roles.session_ready, facts({"ready": False, "why": "absent"}),
                   NODE, NAME, triple, want_type=RuntimeError,
                   want_text="absent") is False)
    c.check("#586 another soul's fact is not readiness",
            roles.session_ready(facts(soul_fact(digest="f" * 32)),
                                NODE, NAME, triple) is False)
    c.check("#586 another role's fact is not readiness",
            roles.session_ready(facts(soul_fact(role="other")),
                                NODE, NAME, triple) is False)


# HYPOTHEHSIS: провал после создания (bootstrap, ssh) читается как «не
# вышло» без имени, и следующий запуск плодит второго папета.
# SOLUTION: каждый отказ после регистрации называет созданного папета и
# `mop attach`; повторного add нет.
def check_failures_after_creation_586(c):
    pool = Pool(bootstrap={"played": True, "ok": False, "task": "apt", "message": "boom"})
    out, err, code = launch(["builder"], pool)
    c.expect("#586 bootstrap failure fails", code, 1)
    c.check("#586 bootstrap failure carries its reason and the name",
            "bootstrap failed" in err and "apt" in err and NAME in err, err)
    c.expect("#586 bootstrap failure does not attach", pool.ssh, [])

    pool = Pool(ssh_rc=255)
    out, err, code = launch(["builder"], pool)
    c.expect("#586 ssh failure fails", code, 1)
    c.check("#586 ssh failure names the running puppet and mop attach",
            NAME in err and f"mop attach {NAME}" in err and "ssh" in err, err)
    c.expect("#586 ssh failure does not create again", len(pool.adds), 1)

    pool = Pool(echo=False)
    out, err, code = launch(["builder"], pool)
    c.expect("#586 a server without roles fails", code, 1)
    c.check("#586 no role echo names the created puppet and the reason",
            NAME in err and "role" in err, err)
    c.expect("#586 a role-less puppet is not attached", pool.ssh, [])
    c.expect("#586 a role-less puppet is not created again", len(pool.adds), 1)


# HYPOTHESIS: отказ агента и непрошедшая проверка узла терялись в общем
# таймауте без причины: _wait_ready глотал их как «ещё не готово», и
# человек смотрел на «no session ... after Ns» без почему.
# SOLUTION: причина узла -- в текст таймаута последней; {error} ответа
# агента и why непрошедшего soul-факта поднимаются сразу, «факта ещё
# нет» -- тихое ожидание (False).
def check_readiness_reasons_586(c):
    def answers(what):
        return {NODE: what}

    triple = {"soul": "builder", "soul_digest": DIGEST, "soul_inc": INC}
    c.check("#586 an agent error reaches the caller",
            raises(roles.session_ready, answers({"error": "agent: disk full"}),
                   NODE, NAME, triple,
                   want_type=RuntimeError, want_text="disk full") is False)
    puppet = {NAME: {"present": True, "soul": {"ready": False,
                                                "why": "the binding is gone"}}}
    c.check("#586 an unverified fact says why",
            raises(roles.session_ready, answers({"puppets": puppet}),
                   NODE, NAME, triple,
                   want_type=RuntimeError, want_text="binding is gone") is False)
    c.check("#586 a pending poll without the fact is not an error",
            roles.session_ready(answers(
                {"puppets": {NAME: {"present": False}}}), NODE, NAME,
                triple) is False)
    c.check("#586 a missing fact on a live session is not an error",
            roles.session_ready(answers(
                {"puppets": {NAME: {"present": True}}}), NODE, NAME,
                triple) is False)


# HYPOTHESIS: эхо проверяло только непустоту: битый digest (не 64 hex),
# не-UUID инкарнация или отсутствие имени джоба проходили как
# подтверждение роли.
# SOLUTION: грамматика тройки -- общая JobMeta (#580), второй копии
# парсера у клиента нет; имя джоба в ответе обязательно.
def check_echo_grammar_586(c):
    bad = [{"soul": "builder", "soul_digest": "f" * 32, "soul_inc": INC},
           {"soul": "builder", "soul_digest": DIGEST, "soul_inc": "z" * 36},
           {"soul": "builder", "soul_digest": ("ab" * 32).upper(),
            "soul_inc": INC},
           {"soul": "builder", "soul_digest": 5, "soul_inc": INC},
           {"soul": "", "soul_digest": DIGEST, "soul_inc": INC}]
    for triple in bad:
        c.check(f"#586 a malformed echo {triple.get('soul_digest')!r}/"
                f"{triple.get('soul_inc')!r} is refused",
                raises(roles.echo, {"name": NAME, **triple}, "builder") is False)
    c.check("#586 the created job name is required",
            raises(roles.echo, {"soul": "builder", "soul_digest": DIGEST,
                                "soul_inc": INC}, "builder",
                      want_text="named no created puppet") is False)
    c.check("#586 a well-formed echo passes",
            raises(roles.echo, {"name": NAME, "soul": "builder",
                                "soul_digest": DIGEST, "soul_inc": INC},
                   "builder"))


# HYPOTHESIS: _wait_ready зовёт puppets.node_of ДО своей обработки: узел
# не определён (нет работающей аллокации, сервис отказал) -- исключение
# улетает без имени созданного папета и пути назад, и диспетчер печатает
# голый traceback.
# SOLUTION: node_of внутри ожидания: отказ узла -- та же причина в сроке
# готовности (созданного называем, второго не создаём), а небежащий папет
# -- ожидание до конца срока, как молчащий агент.
def check_node_failure_586(c):
    def node_refused(*a, **kw):
        raise bus.Refused("the project is closed for new placements")

    def node_unknown(*a, **kw):
        raise LookupError(f"{NAME}: no allocation — node unknown")

    for node_fn, word in ((node_refused, "closed for new placements"),
                          (node_unknown, "node unknown")):
        pool = Pool()
        with patched(puppets, node_of=node_fn):
            out, err, code = launch(["builder"], pool)
        c.expect(f"#586 node resolution failure ({word}) fails", code, 1)
        c.check(f"#586 the failure names the created puppet ({word})",
                NAME in err, err)
        c.check(f"#586 the failure carries the reason ({word})", word in err, err)
        c.check(f"#586 no second add ({word})", len(pool.adds), 1)
        c.expect(f"#586 the failure does not attach ({word})", pool.ssh, [])


# HYPOTHESIS: неизвестность ответа регистрации (таймаут шины) повторяется
# новым запросом, и сервер заводит второго папета.
# SOLUTION: request_id выдаётся один раз на попытку создания и едет во всех
# повторах add: сервер (#582) сводит их к одному джобу и гранту.
def check_request_id_586(c):
    pool = Pool(timeouts=2)
    out, err, code = launch(["builder"], pool)
    c.expect("#586 uncertain add is retried to success", (code, err), (0, ""))
    ids = [a.get("request_id") for a in pool.adds]
    c.expect("#586 three add attempts", len(ids), 3)
    c.check("#586 request_id is set", bool(ids and ids[0]), ids)
    c.check("#586 request_id is stable across retries", len(set(ids)) == 1, ids)

    other = Pool()
    launch(["builder"], other)
    c.check("#586 a new launch is a new request_id",
            other.adds and ids and other.adds[0].get("request_id") != ids[0],
            (ids, other.adds))

    # Поля роли -- общим ops.add (#603): клиент не собирает провод сам, а
    # передаёт soul/request_id аргументами операции. Проверка смотрит и
    # аргументы ops.add, и провод: аргументы без провода -- поля потерялись
    # в клиенте, провод без аргументов -- клиент собрал его копией.
    pool = Pool()
    seen = {}

    real_add = ops.add

    def recording_add(transport, *a, **kw):
        seen["kwargs"] = {k: kw.get(k) for k in ("soul", "request_id")}
        out = real_add(transport, *a, **kw)
        seen["wire"] = pool.adds[-1] if pool.adds else None
        return out

    with patched(ops, add=recording_add):
        launch(["builder"], pool)
    kw, wire = seen.get("kwargs") or {}, seen.get("wire") or {}
    c.check("#586 ops.add is called with the role fields",
            kw.get("soul") == "builder" and bool(kw.get("request_id")), kw)
    c.check("#586 the wire carries the role fields once",
            wire.get("soul") == "builder" and bool(wire.get("request_id")), wire)
    c.expect("#586 exactly one add on the wire", len(pool.adds), 1)

    pool = Pool(timeouts=10)
    out, err, code = launch(["builder"], pool)
    c.expect("#586 add uncertainty exhausted fails", code, 1)
    c.expect("#586 retries are bounded", len(pool.adds), 3)
    c.check("#586 the uncertainty is said with the request id",
            "unknown" in err and pool.adds and pool.adds[0]["request_id"] in err, err)
    c.expect("#586 uncertainty does not attach", pool.ssh, [])

    # Повтор -- только когда сервер сказал, что сводит повторы по request_id.
    pool = Pool(timeouts=1, protocol={"version": 1})
    out, err, code = launch(["builder"], pool)
    c.expect("#586 without reconciliation one uncertain add fails", code, 1)
    c.expect("#586 without reconciliation no blind retry", len(pool.adds), 1)
    c.check("#586 the single uncertainty names its request id",
            pool.adds and pool.adds[0]["request_id"] in err, err)


# HYPOTHESIS: старый сервер молча игнорирует поле soul и заводит
# безролевого папета.
# SOLUTION: рукопожатие ping до создания: нет протокола ролей -- отказ без
# аллокации; после add -- проверка полной тройки эха.
def check_handshake_586(c):
    for protocol in (None, {"version": 2, "request_id": True}, {"request_id": True}):
        pool = Pool(protocol=protocol)
        out, err, code = launch(["builder"], pool)
        c.expect(f"#586 server protocol {protocol} refused", code, 1)
        c.check(f"#586 refusal says roles are unsupported ({protocol})",
                "role" in err and "server" in err, err)
        c.expect(f"#586 server protocol {protocol} creates nothing", pool.adds, [])

    pool = Pool(echo="partial")
    out, err, code = launch(["builder"], pool)
    c.expect("#586 a partial role echo fails", code, 1)
    c.check("#586 a partial echo names the created puppet", NAME in err, err)
    c.expect("#586 a partial echo is not attached", pool.ssh, [])
    c.check("#586 nothing is deleted", "delete" not in pool.calls, pool.calls)


# HYPOTHESIS: отказ сервера (нет изолирующего драйвера, роль не пускают)
# теряет причину или приходит после аллокации.
# SOLUTION: Refused регистрации -- ошибка с текстом сервера, без ожидания
# узла и без ssh.
def check_server_refusal_586(c):
    pool = Pool(refuse="role master needs an isolating driver: none in the pool")
    out, err, code = launch(["builder"], pool)
    c.expect("#586 refusal fails", code, 1)
    c.check("#586 refusal carries the server's reason", "isolating driver" in err, err)
    c.expect("#586 refusal asks nothing after add", pool.calls, ["ping", "add"])


# HYPOTHESIS: пасстру claude-опций позволяет подменить роль, промпт или
# креды, и лаунчер шлёт их в тело.
# SOLUTION: лаунчер принимает только [origin]; --model/--effort -- тоже
# отказ до аллокации, пока сервер и обёртка их не довозят (#582/#583).
def check_options_586(c):
    for argv in (["--append-system-prompt", "x"], ["--append-system-prompt-file", "f"],
                 ["--system-prompt", "x"], ["--settings", "{}"], ["--mcp-config", "{}"],
                 ["--dangerously-skip-permissions"], ["--continue"], ["-p", "hi"],
                 ["--model", "opus"], ["--effort=high"], ["--model"],
                 ["a", "b"], ["--", "--model", "x"]):
        pool = Pool()
        out, err, code = launch(["builder", *argv], pool)
        c.check(f"#586 {argv} refused before allocation",
                code not in (0, None) and pool.calls == [], (code, pool.calls, err))

    pool = Pool()
    out, err, code = launch(["builder", ORIGIN], pool)
    c.expect("#586 an explicit origin is accepted", (code, err), (0, ""))
    add = pool.adds[0] if pool.adds else {}
    c.check("#586 nothing but the role and the request travels",
            "model" not in add and "effort" not in add, add)


# HYPOTHESIS: без терминала лаунчер создаёт папета и падает на attach.
# SOLUTION: нет терминала -- отказ до аллокации.
def check_no_tty_586(c):
    pool = Pool()
    out, err, code = launch(["builder"], pool, tty=False)
    c.expect("#586 no tty fails", code, 1)
    c.check("#586 no tty says why", "terminal" in err, err)
    c.expect("#586 no tty allocates nothing", pool.calls, [])


# HYPOTHESIS: роль каталога затеняет встроенную команду или берёт
# зарезервированное имя шины, и `mop <имя>` молча делает не то.
# SOLUTION: коллизия -- громкий отказ до аллокации; встроенная команда
# при этом остаётся встроенной.
def check_reserved_586(c):
    for name in ("list", "add", "attach", "dev", "server", "help", "deploy",
                 "join", "admin", "service", "callout"):
        root = tempfile.mkdtemp(prefix="mop-roles-")
        role_dir(root, name)
        role_dir(root, "fine")
        pool = Pool()
        out, err, code = launch(["fine"], pool, catalogue_dirs=[root])
        c.expect(f"#586 role {name} collides", code, 1)
        c.check(f"#586 collision with {name} is named", name in err, err)
        c.expect(f"#586 collision with {name} allocates nothing", pool.calls, [])

    for prefix in ("grant-", "puppet-", "node-", "master-"):
        root = tempfile.mkdtemp(prefix="mop-roles-")
        # Имя с префиксом грамматике роли подходит -- отказ даёт резерв.
        role_dir(root, prefix + "x")
        pool = Pool()
        out, err, code = launch([prefix + "x"], pool,
                                catalogue_dirs=[root])
        c.expect(f"#586 reserved prefix {prefix} refused", code, 1)
        c.expect(f"#586 reserved prefix {prefix} allocates nothing", pool.calls, [])

    root = tempfile.mkdtemp(prefix="mop-roles-")
    role_dir(root, "broken", extra="unknown: 1\n")
    pool = Pool()
    out, err, code = launch(["builder"], pool, catalogue_dirs=[root])
    c.expect("#586 a malformed catalogue refuses", code, 1)
    c.check("#586 malformed catalogue says what", "unknown" in err, err)
    c.expect("#586 malformed catalogue allocates nothing", pool.calls, [])

    root = tempfile.mkdtemp(prefix="mop-roles-")
    d = role_dir(root, "loose")
    with open(os.path.join(d, "stray"), "w") as f:
        f.write("x")
    pool = Pool()
    out, err, code = launch(["loose"], pool, catalogue_dirs=[root])
    c.expect("#586 a role that is not a bundle refuses", code, 1)
    c.expect("#586 a broken bundle allocates nothing", pool.calls, [])


# HYPOTHESIS: `mop master` по-прежнему execvpe локального claude с
# человеческими кредами, и мастер-роль из пула недостижима.
# SOLUTION: `mop master` -- явный алиас мастер-роли через общий лаунчер,
# пишет, что запускает; прежний локальный запуск -- `mop master --local`.
def check_master_alias_586(c):
    # #662: у алиаса нет ни своего os/exec, ни локального лаунчера --
    # только общий лаунчер роли; exec-контроль прежний: ничего локального.
    execs = []
    pool = Pool()
    out, err, code = launch(["master"], pool)
    c.expect("#586 mop master launches the role", (code, err), (0, ""))
    c.expect("#586 mop master never execs a local claude", execs, [])
    add = pool.adds[0] if pool.adds else {}
    c.expect("#586 mop master asks for role master", add.get("soul"), "master")
    c.check("#586 mop master says what it launches",
            "role master" in out and "pooled puppet" in out, out)

    # #662: локальный запуск снят (breaking change): --local больше не
    # опция, запуск локального claude невозможен из этой команды.
    pool = Pool()
    out, err, code = launch(["master", "--local", "--", "--model", "opus"], pool)
    c.check("#662 --local is refused before anything is allocated",
            code != 0 and not pool.calls, (code, out, err))


# HYPOTHESIS: `mop attach` и лаунчер собирают ssh каждый своей копией, и
# attach может создать папета.
# SOLUTION: один помощник _common.attach_argv; attach только подключается.
def check_attach_586(c):
    pool = Pool()
    launch(["builder"], pool)
    execs = []
    with patched(attach.os, execvp=lambda *a: execs.append(a)), \
            patched(puppets, running=lambda n: ({"NodeName": NODE}, "host")), \
            patched(attach.lib, guard=lambda n: None), \
            patched(config, require=lambda *a: None):
        run_command(attach.main, [NAME])
    c.expect("#586 attach execs ssh once", len(execs), 1)
    c.check("#586 attach and the launcher enter the body the same way",
            execs and pool.ssh and list(execs[0][1]) == pool.ssh[0], (execs, pool.ssh))


def main():
    c = Checks()
    for check in (check_catalogue_roots_586, check_readiness_shape_586,
                  check_readiness_reasons_586, check_echo_grammar_586,
                  check_node_failure_586,
                  check_role_dispatch_586, check_readiness_586,
                  check_failures_after_creation_586, check_request_id_586,
                  check_handshake_586, check_server_refusal_586, check_options_586, check_no_tty_586,
                  check_reserved_586, check_master_alias_586, check_attach_586):
        try:
            check(c)
        except Exception as e:  # noqa: BLE001 -- провал проверки, а не файла
            c.fail(f"{check.__name__} raised", f"{type(e).__name__}: {e}")
    return c.report("launch")


if __name__ == "__main__":
    sys.exit(main())
