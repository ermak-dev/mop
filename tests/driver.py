#!/usr/bin/env python3
"""Проверка реестра драйверов узла без пула: python3 tests/driver.py

Драйвер отвечает на третий вопрос системы — в чём живёт папет (docs/DRIVER.md).
Реестр — чистая функция над каталогом плагинов, и нарушение контракта обязано
находиться здесь, а не на узле: агент зовёт глаголы драйвера из петли, и
отсутствующий `argv` там прочитается как «узел молчит», а не как «драйвер
кривой».

Проверяется ровно то, что проверяемо без пула: контракт реестра, чистые
функции драйвера host (префиксы команд) и проверка имени, из которого драйвер
собирает шелл. Всё остальное — создание тела, ssh в него, лимиты — добывается
на живом пуле.
"""
import os
import sys
import tempfile
import types

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import (Checks, canned, offline, patched, patched_env,  # noqa: E402
                  restored, run_command, udp_socket)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config  # noqa: E402
from mop import driver  # noqa: E402


def plugin(**attrs):
    """Модуль-плагин с заданными атрибутами; остальное берёт контрактом."""
    mod = types.ModuleType("fake")
    for verb in driver.VERBS:
        mod.__dict__[verb] = lambda *a, **k: None
    mod.SESSION_PY = "/opt/mop/mop/session.py"
    mod.IS_CONTAINER = True
    mod.__dict__.update(attrs)
    return mod


# ── контракт реестра ─────────────────────────────────────────────────────
CONTRACT = [
    # (что проверяем, модуль, ок?)
    ("complete module", plugin(), True),
    ("description — first line of docstring", plugin(__doc__="one\ntwo"), True),
    ("no argv", plugin(argv=None), False),
    ("no ensure", plugin(ensure=None), False),
    ("no destroy", plugin(destroy=None), False),
    ("no bodies", plugin(bodies=None), False),
    ("no capacity", plugin(capacity=None), False),
    ("no push", plugin(push=None), False),
    ("no projects_dir", plugin(projects_dir=None), False),
    ("no attach_argv", plugin(attach_argv=None), False),
    ("no repair_argv", plugin(repair_argv=None), False),
    ("no run_argv", plugin(run_argv=None), False),
    # admit (#62): открыть/закрыть телу дорогу для bootstrap'а с сервера.
    ("no admit", plugin(admit=None), False),
    ("a verb that is not callable", plugin(argv="ssh"), False),
    # SESSION_PY уезжает в шелл внутри тела. Пустое значение там молча
    # соберётся в `python3  probe <clone>` — python прочитает probe как файл.
    ("IS_CONTAINER not declared", plugin(IS_CONTAINER=None), False),
    ("IS_CONTAINER not a bool", plugin(IS_CONTAINER="yes"), False),
    # #151: адрес тела и сборочные тела -- в контракте; драйвер без них
    # проходил проверку и падал у потребителя (bootstrap, junk).
    ("no address", plugin(address=None), False),
    ("no templates", plugin(templates=None), False),
    # #276: один толстый контракт требовал от host глагол, смысла для него не
    # имеющий. templates -- вопрос гипервизора: драйвер с отдельными телами
    # обязан его иметь, драйвер «тело = узел» -- нет. Глаголы тела обязаны все.
    ("pve without templates -> refused",
     plugin(IS_CONTAINER=True, templates=None), False),
    ("host without templates -> fine",
     plugin(IS_CONTAINER=False, templates=None), True),
    ("host without ensure -> refused",
     plugin(IS_CONTAINER=False, ensure=None), False),
    ("host without bodies -> refused",
     plugin(IS_CONTAINER=False, bodies=None), False),
    ("host without capacity -> refused",
     plugin(IS_CONTAINER=False, capacity=None), False),
    ("no SESSION_PY", plugin(SESSION_PY=None), False),
    ("empty SESSION_PY", plugin(SESSION_PY=""), False),
    ("SESSION_PY is not a path", plugin(SESSION_PY="session.py"), False),
]

# Имя папета склеивается в шелл — и у host, и у драйвера контейнеров. Проверка
# имени поэтому общая, в самом реестре: два списка разъехались бы молча.
NAMES = [
    ("pu-mop-1", True),
    ("pu-some-project-12", True),
    ("mop-1", False),            # без префикса — не папет
    ("pu-mop-1/../etc", False),
    ("pu-mop 1", False),
    ("pu-mop-1;rm -rf /", False),
    ("pu-$(id)-1", False),
    ("", False),
]

# Драйвер host: тело равно узлу, поэтому префиксы пустые, а человек входит
# прямо в tmux-сервер папета. На этом стоит `mop attach`.
HOST_ARGV = [
    ("argv is empty: the body is the node", "argv", []),
    ("nothing to connect with either: the wrapper runs here", "run_argv", []),
    ("repair path is the same as the main one", "repair_argv", []),
    ("a human attaches to the puppet's own tmux server", "attach_argv",
     ["tmux", "-L", "pu-mop-1", "attach", "-t", "pu-mop-1"]),
]


# Драйвер pve: тело — контейнер LXC, и всё про него выводится из имени.
# Второе имя для того же (VMID в модели mop, адрес в реестре) означало бы
# второе место, отвечающее на вопрос «чей это папет».
PVE_NAMES = ("pu-mop-1", "pu-mop-2", "pu-rugent-7", "pu-cloudpub-11",
             "pu-some.proj-3")


ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def consumed_names():
    """Что потребители берут у модуля драйвера -- выводом из их кода, а не
    списком: `DRIVER.<имя>` в агенте, `d.<имя>` там, где d -- модуль драйвера
    (`driver.current()`/`driver.module(...)` или первый аргумент `d`), и
    `driver.current().<имя>`."""
    import re
    found = set()
    for base, _, files in os.walk(os.path.join(ROOT, "mop")):
        if os.path.join("mop", "driver") in base:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            text = open(os.path.join(base, f)).read()
            # (?<!/): `docs/DRIVER.md` в тексте -- путь, а не обращение.
            found |= set(re.findall(r"(?<!/)\bDRIVER\.([A-Za-z_]+)", text))
            found |= set(re.findall(r"driver\.current\(\)\.([A-Za-z_]+)", text))
            # d -- модуль драйвера: присвоен из реестра или пришёл первым
            # аргументом (bootstrap.run(d, name, project)).
            if re.search(r"\bd = driver\.(current|module)\(|\bdef \w+\(d, ", text):
                found |= set(re.findall(r"\bd\.([A-Za-z_]+)", text))
    return found


def old_toward_server():
    """Адрес узла со стороны сервера, как его считал bootstrap до #151."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((config.get("MOP_SERVER_LAN"), 1))
        return s.getsockname()[0]
    finally:
        s.close()


def check_contract_151(c):
    """#151: адрес, сборочные тела и тип узла -- в контракте, ветки по флагу
    -- в драйверах."""
    import asyncio
    import re

    host, pve = driver.module("host"), driver.module("pve")

    # Всё, что потребители зовут у драйвера, объявлено контрактом, и каждый
    # драйвер это имеет: третий драйвер иначе проходит проверку и падает у
    # потребителя.
    used = consumed_names()
    c.check("consumers call only what driver.CONSUMED declares",
            not (not used or used - set(driver.CONSUMED)),
            f"consumers call {sorted(used - set(driver.CONSUMED))} outside "
            f"driver.CONSUMED {driver.CONSUMED}")
    for name, mod in (("host", host), ("pve", pve)):
        for attr in driver.CONSUMED:
            c.check(f"{name} has {attr}, which consumers call", hasattr(mod, attr))

    # Флага и сравнения с драйвером по умолчанию вне mop/driver/ нет.
    for base, _, files in os.walk(os.path.join(ROOT, "mop")):
        if os.path.join("mop", "driver") in base:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            for n, line in enumerate(open(os.path.join(base, f)), 1):
                code = line.split("#")[0]
                if re.search(r"BODY_IS_NODE|IS_CONTAINER|address_of\(|getattr\(.*templates"
                             r"|[!=]= *driver\.DEFAULT", code):
                    c.fail(f"{os.path.relpath(os.path.join(base, f), ROOT)}:{n} "
                           f"branches on the driver type", line.strip())

    # Характеризация: ответы прежних веток. Адрес сервера у host с #200 -- из
    # url кредов шины узла, а не из настройки; сервер тот же.
    with tempfile.TemporaryDirectory() as tmp, \
            patched_env(MOP_SERVER_LAN="127.0.0.1"), \
            patched(host, NODE_FILE=os.path.join(tmp, "bus.json")):
        with open(host.NODE_FILE, "w") as f:
            f.write('{"url": "nats://127.0.0.1:4222"}')
        c.expect("host.address: the node, seen from the server",
                 host.address("pu-mop-1"), old_toward_server())
    for n in ("pu-mop-1", "pu-rugent-7"):
        c.expect(f"pve.address({n})", pve.address(n), pve.address_of(n))
    # #276: у host глагола templates нет вовсе -- контракт его не требует, а
    # потребитель берёт его через driver.hypervisor_verb и без него отвечает
    # прежним [] (agent.v_junk).
    # STATUS: FIXED — see #276
    c.check("host must not carry templates: a node-body driver builds no images",
            not hasattr(host, "templates"))
    c.check("driver.hypervisor_verb(host, 'templates') must be None",
            getattr(driver, "hypervisor_verb", lambda m, v: "missing")(host, "templates") is None)
    c.check("driver.hypervisor_verb(pve, 'templates') must be pve.templates",
            getattr(driver, "hypervisor_verb", lambda m, v: None)(pve, "templates") is pve.templates)
    c.check("templates must not be demanded of every driver",
            "templates" not in getattr(driver, "BODY_VERBS", ("templates",))
            + getattr(driver, "NODE_VERBS", ()))
    c.check("pve.templates must stay a coroutine",
            asyncio.iscoroutinefunction(pve.templates))
    for name, want in (("host", False), ("pve", True)):
        c.check(f"driver.is_container({name!r}) must be {want}",
                not (driver.is_container(name) is not want))
    # До #175 неизвестное и пустое имя были «контейнером» (builder и image
    # решали `!= DEFAULT`), а of_node оставлял пустое пустым. Теперь пустое
    # -- DEFAULT, неизвестное -- отказ (check_driver_rule_175).
    for name in ("", "nosuch"):
        try:
            driver.is_container(name)
            refused = False
        except RuntimeError:
            refused = True
        c.check(f"driver.is_container({name!r}) must refuse", refused)
    for meta, want in (({}, driver.DEFAULT), ({"mop_driver": "pve"}, "pve"),
                       ({"mop_driver": ""}, driver.DEFAULT), (None, driver.DEFAULT)):
        c.expect(f"driver.of_node({meta!r})", driver.of_node(meta), want)

    # Впуск ключа сервера (#62) -- полиморфный admit(name, open): host открывать
    # нечего, pve без ключа сервера на узле отказывает прежним текстом.
    for open_ in (True, False):
        c.expect(f"host.admit(..., {open_}) must be a no-op",
                 asyncio.run(host.admit("pu-mop-1", open_)), {})
    c.expect("driver.SERVER_PUB must not move", driver.SERVER_PUB,
             f"{driver.HOME}/.config/mop/bootstrap.pub")
    missing = os.path.join(tempfile.mkdtemp(prefix="mop-test-driver-"), "bootstrap.pub")
    with patched(pve, SERVER_PUB=missing):
        try:
            asyncio.run(pve.admit("pu-mop-1", True))
            c.fail("pve.admit without the server key must refuse")
        except RuntimeError as e:
            c.expect("pve refusal without the server key", str(e),
                     f"no server key on this node ({missing}) — run mop server deploy")


# ── #345: врапер и сдавшийся bootstrap ──────────────────────────────────
# HYPOTHESIS: ответ gave_up (третий провал того же workspace) врапер
# печатал бы как обычный провал -- без «сдался» и без того, что делать; а
# стоп джоба, пришедший посреди bootstrap, убил бы процесс SIGTERM'ом до
# finally, и ключ сервера остался бы в теле.
# SOLUTION: refusal называет сдачу и лечение; bootstrap_sandbox на время
# разговора ставит обработчик SIGTERM, который бросает SystemExit, так что
# дверь закрывается в любом исходе, и возвращает прежний обработчик.
# STATUS: FIXED — see #345
def check_gave_up_wrapper_345(c):
    import os
    import signal
    import time
    from mop.common import bus
    from mop.cli.driver import run
    reply = {"ok": False, "played": True, "rc": 2, "tail": "TASK [bootstrap : sync]\nfatal: ...",
             "task": "bootstrap : sync", "message": "Group `cloud` is not defined",
             "gave_up": True, "failures": 3}
    c.expect("#345 the wrapper names the give-up and the cure",
             run.refusal("pu-rudesktop-8", run.BootstrapFailed(reply)),
             "bootstrap of pu-rudesktop-8 gave up after 3 attempts at task «bootstrap : sync»: "
             "Group `cloud` is not defined — fix .mop/bootstrap.yaml, then mop update\n"
             "TASK [bootstrap : sync]\nfatal: ...")
    c.expect("#345 a replay-less gave-up reply reads the same way",
             run.refusal("pu-x-1", run.BootstrapFailed({**reply, "played": False, "tail": "t"})),
             "bootstrap of pu-x-1 gave up after 3 attempts at task «bootstrap : sync»: "
             "Group `cloud` is not defined — fix .mop/bootstrap.yaml, then mop update\nt")

    doors = []

    class D:
        def address(self, name):
            return "10.0.0.5"

        async def admit(self, name, on):
            doors.append(on)
            return {}

    def stopped(*a, **kw):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
        return {"ok": True}
    # Свой безобидный обработчик на время проверки: без защиты во врапере
    # SIGTERM не убьёт прогон проверок, а покажет, что bootstrap не прервался.
    seen = []

    def before(_sig, _frm):
        seen.append(1)
    saved = signal.signal(signal.SIGTERM, before)
    try:
        with patched(bus, connect=lambda *a, **kw: None, ask_server=stopped):
            try:
                run.bootstrap_sandbox(D(), "pu-x-1", "x")
                c.fail("#345 a SIGTERM during bootstrap must end the wrapper")
            except SystemExit:
                pass
        c.expect("#345 a stop during bootstrap still closes the door", doors, [True, False])
        c.check("#345 the previous SIGTERM handler is back",
                signal.getsignal(signal.SIGTERM) is before)
    finally:
        signal.signal(signal.SIGTERM, saved)



# ── #346: сторож контейнерных узлов не сносит работу ─────────────────────────
# HYPOTHESIS: `mop driver sweep` сносил каждое тело без живой tmux-сессии
# вместе с клоном -- и папета на подъёме, и папета с падающим bootstrap'ом,
# у которых сессии нет часами, а работа в клоне есть. Тело, до которого не
# достучаться (ssh 255, таймаут), тоже шло под снос: живым считался только
# код 0, хотя докстринг обещал «молчащее тело оставляем».
# SOLUTION: план из фактов тела. Снос -- только «нет сессии» (tmux ответил 1)
# и клон без работы по domain.holds_work, либо клона нет вовсе; клон с
# работой и непрочитанный клон -- оставить с причиной; недостижимое тело --
# оставить. Предохранитель «ноль живых -- отказ» прежний.
# STATUS: FIXED — see #346
def _clone(dirty=0, ahead=0, branch="master", default="master"):
    from mop.common.domain import CloneFacts
    return CloneFacts(branch=branch, default_branch=default, dirty=dirty, ahead=ahead).to_dict()


SWEEP_PLAN = [
    ("no session, clean clone -> destroyed", ("pu-x-1", 1, _clone(), None),
     ("destroy", "no session, clean clone")),
    ("no session, uncommitted -> kept", ("pu-x-2", 1, _clone(dirty=3), None),
     ("keep", "no session, clone holds work (uncommitted: 3)")),
    ("no session, unpushed -> kept", ("pu-x-3", 1, _clone(ahead=2), None),
     ("keep", "no session, clone holds work (unpushed: 2)")),
    ("no session, both -> kept", ("pu-x-4", 1, _clone(dirty=1, ahead=1), None),
     ("keep", "no session, clone holds work (uncommitted: 1, unpushed: 1)")),
    ("no session, clean but off its home -> kept", ("pu-x-5", 1, _clone(branch="fix/7-x"), None),
     ("keep", "no session, clone holds work (off home master)")),
    ("no session, clone unreadable -> kept", ("pu-x-6", 1, None, 0),
     ("keep", "no session, clone unreadable")),
    ("no session, clone probe unanswered -> kept", ("pu-x-7", 1, None, None),
     ("keep", "no session, clone unreadable")),
    ("no session, no clone at all -> destroyed", ("pu-x-8", 1, None, 1),
     ("destroy", "no session, no clone")),
    ("body unreachable (ssh) -> kept", ("pu-x-9", 255, None, None),
     ("keep", "body unreachable (tmux check: exit 255)")),
    ("body silent (timeout) -> kept", ("pu-x-10", None, None, None),
     ("keep", "body unreachable (tmux check: timeout)")),
]


def check_sweep_keeps_work_346(c):
    from mop.cli.driver import sweep
    plan = getattr(sweep, "plan", None)
    if c.check("#346 sweep has a plan over body facts", callable(plan)):
        for what, facts, want in SWEEP_PLAN:
            c.expect(f"#346 sweep plan: {what}", plan(*facts), want)
    # Командлет целиком: драйвер, tmux и клоны подменены.
    from mop.node import agent
    codes = {"pu-a-1": 0, "pu-a-2": 1, "pu-a-3": 1, "pu-a-4": 255, "pu-a-5": 1}
    clones = {"pu-a-2": _clone(), "pu-a-3": _clone(dirty=2, ahead=1), "pu-a-5": None}
    destroyed = []

    def fake_driver(names):
        async def bodies():
            return list(names)

        async def destroy(name, branch=None):
            destroyed.append(name)
            return {"destroyed": 101, "target": f"body 101 ({name})"}
        return types.SimpleNamespace(IS_CONTAINER=True, bodies=bodies, destroy=destroy,
                                     argv=lambda n: ["body", n])

    async def sh(script, timeout=20, prefix=()):
        name = prefix[-1]
        if "has-session" in script:
            return "", codes[name]
        return "", 1                                  # test -d: клона нет

    async def clone_facts(name):
        return clones.get(name)

    def run(names, argv):
        destroyed.clear()
        d = fake_driver(names)
        with patched(driver, current=lambda: d, current_name=lambda: "pve",
                     is_container=lambda n: True, sh=sh), \
                patched(agent, clone_facts=clone_facts):
            return run_command(sweep.main, argv)
    out, err, code = run(list(codes), [])
    c.expect("#346 sweep destroys only the clean and the clone-less", sorted(destroyed),
             ["pu-a-2", "pu-a-5"])
    c.check("#346 sweep names what it keeps and why",
            "kept pu-a-3: no session, clone holds work (uncommitted: 2, unpushed: 1)" in out
            and "kept pu-a-4: body unreachable (tmux check: exit 255)" in out, out)
    c.expect("#346 sweep exit", code, 0)
    out, err, code = run(list(codes), ["--dry"])
    c.check("#346 --dry destroys nothing and still names the kept",
            destroyed == [] and "would destroy pu-a-2" in out and "kept pu-a-3" in out, out)
    # Предохранитель: ни одной живой сессии -- отказ, ничего не снесено.
    codes.update({"pu-a-1": 1})
    out, err, code = run(list(codes), [])
    c.check("#346 zero live sessions: refused, nothing destroyed",
            destroyed == [] and code == 1 and "refusing to sweep" in err, (out, err, code))


def main():
    c = Checks()
    check_gave_up_wrapper_345(c)
    check_sweep_keeps_work_346(c)

    for what, mod, ok in CONTRACT:
        try:
            got = driver.contract("fake", mod)
            refused = False
        except RuntimeError:
            got, refused = None, True
        if c.check(f"contract: {what}", refused != ok,
                   f"{'refused' if refused else 'accepted'}, wanted the opposite"):
            c.check(f"contract: {what} — doc is a string",
                    not (ok and got.get("doc") and not isinstance(got["doc"], str)))

    c.expect("contract: doc must be the first line of the docstring",
             driver.contract("fake", plugin(__doc__="one\ntwo"))["doc"], "one")

    for name, ok in NAMES:
        c.expect(f"valid_name({name!r})", driver.valid_name(name), ok)

    c.check("registry finds the host driver", isinstance(driver.get("host"), dict))
    c.check("get() of an unknown name must return None", driver.get("no-such") is None)
    try:
        driver.require("no-such")
        refused = False
    except RuntimeError:
        refused = True
    c.check("require() of an unknown name must refuse", refused)

    # Драйвер узла, а не папета: значение приезжает в окружение агента юнитом,
    # и подставить его запросом с шины нельзя. Дефолт — host: узел, ничего про
    # драйвер не знающий, обязан вести себя как раньше.
    host = driver.module("host")
    for what, verb, want in HOST_ARGV:
        c.expect(f"host.{verb}: {what}", getattr(host, verb)("pu-mop-1"), want)

    # Драйвер узла, а не папета, и спрашивают его двое с разным окружением:
    # агент из юнита systemd и внешний врапер из процесса задачи Nomad. Пока
    # значение жило строкой Environment= в юните, врапер его не видел и молча
    # поднимал папета драйвером host — на гипервизоре это значит «прямо на
    # гипервизоре, мимо тела». Поэтому источник правды — файл узла.
    tmp = os.path.join(tempfile.mkdtemp(), "node.env")
    try:
        with patched_env(MOP_DRIVER=None), patched(config, NODE_ENV_FILE=tmp):
            config.forget()
            c.check("a node that says nothing about a driver must be host",
                    driver.current_name() == "host")
            with open(tmp, "w") as f:
                f.write("MOP_DRIVER=pve\n")
            config.forget()
            c.check("current_name() must read the node's file — an empty "
                    "environment is the wrapper's normal case",
                    driver.current_name() == "pve")
            with open(tmp, "w") as f:
                f.write("MOP_DRIVER=\n")
            config.forget()
            c.check("an empty file must mean host, not an empty driver name",
                    driver.current_name() == "host")
            with open(tmp, "w") as f:
                f.write("MOP_DRIVER=pve\n")
            config.forget()
            os.environ["MOP_DRIVER"] = "host"
            c.check("MOP_DRIVER must outrank the node's file", driver.current() is host)
    finally:
        config.forget()

    # ── драйвер pve: всё выводится из имени ──────────────────────────────
    pve = driver.module("pve")

    # Раздача файла (`mop login`) идёт в узел И в каждое тело. У host второе
    # было бы той же записью в тот же файл по разу на папета — и, что хуже,
    # отчёт обещал бы запись в тела, которых нет.
    c.check("host.IS_CONTAINER must be False — the body IS the node",
            host.IS_CONTAINER is False)

    c.check("registry finds the pve driver", isinstance(driver.get("pve"), dict))

    # VMID в своём диапазоне и не пересекается с диапазоном шаблонов: шаблон
    # живёт рядом с телами и сносится теми же глаголами, так что налезь один
    # на другой — снос папета унёс бы образ проекта.
    seen = {}
    for name in PVE_NAMES:
        vmid = pve.vmid_of(name)
        c.check(f"pve.vmid_of({name!r}) within {pve.BODY_MIN}..{pve.BODY_MAX}",
                pve.BODY_MIN <= vmid <= pve.BODY_MAX, f"got {vmid}")
        c.check(f"pve.vmid_of: {name!r} collides with no other name", vmid not in seen,
                f"{name!r} and {seen.get(vmid)!r} collide on {vmid}")
        seen[vmid] = name

    c.check("pve.vmid_of must be a function of the name, nothing else",
            pve.vmid_of("pu-mop-1") == pve.vmid_of("pu-mop-1"))

    for project in ("mop", "rugent", "cloudpub"):
        t = pve.template_vmid(project)
        c.check(f"pve.template_vmid({project!r}) within {pve.TMPL_MIN}..{pve.TMPL_MAX}",
                pve.TMPL_MIN <= t <= pve.TMPL_MAX, f"got {t}")
        c.check(f"template {t} stays out of the body range — a wipe would "
                f"take the project's image with it", not (pve.BODY_MIN <= t <= pve.BODY_MAX))
        # Имя шаблона обязано быть под охраной префикса (root-обёртка на
        # гипервизоре пускает только pu-*), но не быть именем папета: иначе
        # ростер тел показал бы образ живым папетом.
        tn = pve.template_name(project)
        c.check(f"template name {tn!r} must start with pu- and not look like a puppet name",
                not (not tn.startswith("pu-") or driver.valid_name(tn)))

    # Адрес выводится из VMID, а не хранится: хранимый однажды разойдётся с
    # тем, что реально стоит на контейнере.
    addrs = {}
    for name in PVE_NAMES:
        ip = pve.address_of(name)
        c.check(f"pve.address_of({name!r}) is not the gateway", ip != pve.GATEWAY, ip)
        c.check(f"pve.address_of({name!r}) within {pve.SUBNET}",
                ip.startswith(pve.SUBNET.rsplit(".", 2)[0] + "."), ip)
        c.check(f"pve.address_of: {name!r} shares its address with no other name",
                ip not in addrs, f"{name!r} and {addrs.get(ip)!r} share {ip}")
        addrs[ip] = name

    c.check("pve.IS_CONTAINER must be True — a body is a container",
            pve.IS_CONTAINER is True)

    # ssh, а не proxmox_pct_remote: ControlPersist держит соединение, и проба
    # состояния перестаёт платить рукопожатием.
    a = pve.argv("pu-mop-1")
    ip = pve.address_of("pu-mop-1")
    c.check(f"pve.argv must be ssh into {ip}",
            not (a[:1] != ["ssh"] or not any(x.endswith("@" + ip) for x in a)), repr(a))
    c.check("pve.argv without ControlPersist pays a handshake per probe",
            "ControlPersist=" in " ".join(a))
    c.check("pve.argv without BatchMode can stop on a password prompt",
            "BatchMode=yes" in " ".join(a))

    # Соединение врапера живёт столько же, сколько папет, и мультиплексировать
    # его нельзя. Поймано на живом папете: мастер-соединение, уходящее по
    # ControlPersist, уносит с собой сессию врапера — ssh отдаёт 255, Nomad
    # читает это как падение задачи и перезапускает папета на ровном месте.
    r = pve.run_argv("pu-mop-1")
    joined = " ".join(r)
    c.check(f"pve.run_argv must be ssh into {ip}",
            not (r[:1] != ["ssh"] or not any(x.endswith("@" + ip) for x in r)), repr(r))
    c.check("pve.run_argv must not share a multiplexed connection — "
            "the master's ControlPersist would take the puppet down with it",
            not ("ControlMaster=no" not in joined or "ControlPath=none" not in joined))
    # ПОРЯДОК, а не присутствие: у ssh побеждает первая встреченная опция, и
    # `ControlMaster=no` после `auto` из общего списка не действует. Так
    # врапер молча становился клиентом мастер-сокета агента и умирал с ним
    # при каждом рестарте юнита — задача выходила кодом 255 (22.09, дважды).
    # HYPOTHESIS: переопределение стоит после _SSH_OPTS. SOLUTION: перед.
    # STATUS: FIXED — see #72
    masters = [x for x in r if x.startswith("ControlMaster=")]
    paths = [x for x in r if x.startswith("ControlPath=")]
    c.check("pve.run_argv: the FIRST ControlMaster/ControlPath must be "
            "no/none — ssh takes the first value it sees",
            not (masters[:1] != ["ControlMaster=no"] or paths[:1] != ["ControlPath=none"]),
            f"{masters} {paths}")
    c.check("pve.run_argv holds a connection for the puppet's whole "
            "life; without a keepalive a silent NAT drop reads as a dead puppet",
            "ServerAliveInterval" in joined)

    # Аварийный путь — не ssh: он нужен ровно тогда, когда у тела сломана сеть,
    # sshd или права на authorized_keys.
    r = pve.repair_argv("pu-mop-1")
    c.check("pve.repair_argv must not go over ssh", not (not r or "ssh" in r[0]), repr(r))
    c.check("pve.repair_argv must name the body's vmid",
            str(pve.vmid_of("pu-mop-1")) in r, repr(r))

    # Человек входит в тело, а не на гипервизор: на гипервизоре tmux-сервера
    # папета нет вовсе.
    at = pve.attach_argv("pu-mop-1")
    c.check("pve.attach_argv must ssh into the body and run tmux",
            not (at[:1] != ["ssh"] or "tmux" not in at), repr(at))

    # Имя, не прошедшее valid_name, в шелл гипервизора не попадает вовсе.
    for bogus in ("pu-mop-1;id", "../etc", ""):
        try:
            pve.vmid_of(bogus)
            refused = False
        except ValueError:
            refused = True
        c.check(f"pve.vmid_of({bogus!r}) must refuse", refused)

    # Транскрипты лежат внутри тела: считать их путём на гипервизоре значит
    # молча получить нулевой расход токенов у контейнерных папетов.
    c.check("pve.projects_dir must point inside the body",
            not (pve.projects_dir("pu-mop-1") == host.projects_dir("pu-mop-1")
                 and pve.HOME != host.HOME))

    # Адрес сборочного тела обязан быть СВОИМ у каждого проекта и не задевать
    # живые тела. Раньше он считался как «шлюз плюс один» одинаково для всех,
    # и это ловилось не отказом, а зависанием: две сборки на одном
    # гипервизоре садились на один адрес, ssh уходил в чужой контейнер, обе
    # стороны оставались живыми и молчали (22.09, rugent против rudesktop).
    projects = ("rugent", "cloudpub", "mop", "rudesktop", "a", "zzz")
    c.check("no two projects share one build address",
            len({pve.template_address(s) for s in projects}) == len(projects))

    # Диапазоны не пересекаются по построению: VMID шаблонов идут выше VMID
    # тел, и адрес считается из VMID одной формулой. Проверяем края — именно
    # там прежний «шлюз плюс один» и совпадал с первым живым телом.
    body_addrs = {pve.address_of_vmid(v) for v in (pve.BODY_MIN, pve.BODY_MAX)}
    tmpl_addrs = {pve.address_of_vmid(v) for v in (pve.TMPL_MIN, pve.TMPL_MAX)}
    c.check("build addresses do not overlap live bodies", not (body_addrs & tmpl_addrs))

    # И адрес обязан быть настоящим адресом этой сети, не шлюзом: выехавший
    # за подсеть адрес не отказывает, он просто не отвечает.
    import ipaddress as _ip
    net = _ip.ip_network(pve.SUBNET)
    for sh in projects:
        addr = _ip.ip_address(pve.template_address(sh))
        c.check(f"build address of {sh} is inside {net} and not the gateway",
                not (addr not in net or str(addr) == pve.GATEWAY), str(addr))

    # Адрес считается от АБСОЛЮТНОГО VMID, а не от базы этого узла (#58).
    # База (MOP_PVE_VMID_BASE) узловая и у второго гипервизора своя, а
    # смещение от неё сажало первое тело ЛЮБОГО гипервизора на сеть+2: два
    # узла выдали бы один адрес двум телам, и маршрут к сети тел стал бы
    # неоднозначным — не при настройке, а позже и молча. От абсолютного VMID
    # разные базы дают непересекающиеся куски одной плоской сети, и маршрут
    # к каждому гипервизору выходит однозначным сам собой.
    # HYPOTHESIS: address_of_vmid = сеть + 2 + (vmid − BODY_MIN).
    # SOLUTION: сеть + vmid, без базы. STATUS: FIXED — see #58
    for v in (pve.BODY_MIN, pve.BODY_MIN + 828, pve.TMPL_MAX):
        c.expect(f"pve.address_of_vmid({v}): the address must not depend on this "
                 f"node's base", pve.address_of_vmid(v), str(net.network_address + v))

    # VMID, не влезающий в сеть тел, — громкий отказ, а не адрес соседней
    # сети: уехавший за подсеть адрес не отказывает, он просто не отвечает.
    try:
        pve.address_of_vmid(net.num_addresses + 5)
        refused = False
    except ValueError:
        refused = True
    c.check("pve.address_of_vmid past the subnet must refuse", refused)

    # Маршрут к телам этого узла (#59): сервер достаёт до тел только через
    # гипервизор, и кто-то обязан раздать ему маршрут. Кусок сети одного
    # гипервизора — это адреса его диапазона VMID (тела и шаблоны), и
    # покрывать его надо ТОЧНО: шире — и маршруты двух гипервизоров
    # налезут друг на друга, уже — и часть тел останется недостижимой.
    # HYPOTHESIS: маршрута нет вовсе, роль pve кончается мостом и NAT.
    # SOLUTION: pve.routes_of(subnet, base) — CIDR'ы, покрывающие ровно
    # [сеть+base, сеть+base+999]; проверяется без пула. STATUS: FIXED — see #59
    try:
        got = pve.routes_of(pve.SUBNET, 9000)
        present = True
    except AttributeError:
        got, present = None, False
    c.check("pve.routes_of exists: somebody hands the server a route", present)
    if got is not None:
        nets = [_ip.ip_network(n) for n in got]
        covered = set()
        for n in nets:
            covered |= set(n.hosts()) | {n.network_address, n.broadcast_address}
        want = {net.network_address + v for v in range(9000, 10000)}
        c.check("routes_of covers exactly the 1000 addresses of vmids 9000..9999",
                covered == want, f"covers {len(covered)} addresses: {got}")
        # Второй гипервизор со своей базой не пересекается с первым ни одним
        # адресом — иначе маршрут неоднозначен, и ломается это молча.
        other = [_ip.ip_network(n) for n in pve.routes_of(pve.SUBNET, 20000)]
        c.check("routes of two bases do not overlap",
                not any(a.overlaps(b) for a in nets for b in other))
        # И это маршруты именно ЭТОЙ сети, а не соседней.
        c.check(f"no route leaves the bodies' network {net}",
                not any(not n.subnet_of(net) for n in nets + other))

    # Сборочное тело (#60). Пересборка больше не начинается со сноса образа:
    # шаблон полностью клонируется в сборочное тело, плейбук играется там
    # (идемпотентно — качается только новое), и лишь потом образ заменяется.
    # Сборочное тело зовётся по проекту, чтобы оборванную сборку можно было
    # ПРОДОЛЖИТЬ одной командой: следующий прогон находит его по имени.
    # HYPOTHESIS: stage_name/stage_vmid/parse_list нет вовсе — образ сносится
    # первой задачей. SOLUTION: чистые функции ниже. STATUS: FIXED — see #60
    try:
        sn = pve.stage_name("mop")
        c.check(f"stage name {sn!r} must be a template-like name of its own",
                not (not sn.startswith("pu-tmpl-") or driver.valid_name(sn)
                     or sn == pve.template_name("mop")))
    except AttributeError:
        c.fail("pve.stage_name is missing")

    listing = "9828 pu-rugent-1 running\n9988 pu-tmpl-rugent stopped\n"
    try:
        parsed = pve.parse_list(listing)
        c.expect("parse_list", parsed, [(9828, "pu-rugent-1", "running"),
                                        (9988, "pu-tmpl-rugent", "stopped")])
    except AttributeError:
        parsed = None
        c.fail("pve.parse_list is missing")

    if parsed is not None:
        # Оборванная сборка: её тело стоит под именем проекта — продолжаем в нём.
        left = parsed + [(9950, pve.stage_name("mop"), "stopped")]
        c.expect("stage_vmid must resume the build body left by a previous run",
                 pve.stage_vmid("mop", left), 9950)
        # Иначе — свободный номер из диапазона шаблонов, не занятый и не
        # совпадающий с номером образа этого проекта.
        v = pve.stage_vmid("mop", parsed)
        taken = {vm for vm, _, _ in parsed}
        c.check(f"stage_vmid({v}) must be a free template slot of its own",
                not (not pve.TMPL_MIN <= v <= pve.TMPL_MAX or v in taken
                     or v == pve.template_vmid("mop")))
        # Диапазон занят целиком — громко, а не номер чужого контейнера.
        full = [(vm, f"pu-tmpl-x{vm}", "stopped")
                for vm in range(pve.TMPL_MIN, pve.TMPL_MAX + 1)]
        try:
            pve.stage_vmid("mop", full)
            refused = False
        except RuntimeError:
            refused = True
        c.check("stage_vmid with no free slot must refuse", refused)

    # Контракт драйвера — СЛОВАРЬ, и флаг в нём ключом, а не атрибутом.
    # Модуль с атрибутом IS_CONTAINER отдаёт только `current()`, и он про свой
    # узел; спросить про чужой можно лишь по имени, через реестр. Перепутать
    # эти две вещи легко, а отказ приходит не там: `mop delete` успевает снять
    # джоб и падает уже ПОСЛЕ этого на AttributeError, оставляя тело сиротой
    # (поймано 22.09 живым прогоном, на двух телах сразу).
    for name in ("host", "pve"):
        reg = driver.require(name)
        c.check(f"driver.require({name!r}) must be a mapping with is_container",
                not (not isinstance(reg, dict) or "is_container" not in reg),
                f"got {type(reg).__name__}")

    c.check("is_container must tell a node-body driver from a "
            "container one — everything that decides what to destroy "
            "hangs on it",
            not (driver.require("host")["is_container"] is not False
                 or driver.require("pve")["is_container"] is not True))

    # HYPOTHESIS (#114): pve-тело получает кред проекта копией файла с
    # гипервизора, а файл туда клал прогон; прогон его больше не кладёт.
    # SOLUTION: кред едет в тело только из ответа bootstrap (driver run), в
    # переливке с узла его нет. STATUS: FIXED — see #114
    from mop.driver import pve
    c.check("pve seed must not copy the node's bus-<project>.json: "
            "the credentials come from the bootstrap answer only",
            not any("bus-" in path for path, _ in pve._seed_files()))

    # HYPOTHESIS (#137): файл в pve-тело стоил ~4 с (четыре pct на файл), и
    # агент писал в тела по очереди. SOLUTION: все файлы тела -- одним tar
    # через `mop-pve unpack` (#136). STATUS: FIXED — see #137
    import io
    import tarfile
    from mop.driver import pve
    try:
        blob = pve.tar_of([(f"{pve.HOME}/.claude/.credentials.json", b"{}"),
                           (f"{pve.HOME}/.config/mop/secrets.env", b"K=v\n")], pve.HOME)
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            got = {m.name: (m.mode, t.extractfile(m).read()) for m in t.getmembers()}
        c.expect("tar_of must hold home-relative 0600 files", got,
                 {".claude/.credentials.json": (0o600, b"{}"),
                  ".config/mop/secrets.env": (0o600, b"K=v\n")})
        # Время файла -- сейчас: tar без mtime распаковывается 1970-м годом.
        import time
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            c.check("tar_of must stamp the files with the current time",
                    not any(abs(m.mtime - time.time()) > 60 for m in t.getmembers()))
    except AttributeError:
        c.fail("pve.tar_of is missing")
    for outside in ("/etc/passwd", f"{pve.HOME}/../x", "/tmp/x"):
        try:
            pve.tar_of([(outside, b"x")], pve.HOME)
            refused = False
        except (ValueError, AttributeError):
            refused = True
        c.check(f"tar_of must refuse a path outside the home: {outside}", refused)
    c.check("push_many must be a driver verb: the agent writes a body in one call",
            "push_many" in driver.VERBS)

    # #73: первый ssh врапера в свежее тело ушёл в Connection timed out, а
    # повтор Nomad через 17 с вошёл сразу. HYPOTHESIS: сеть свежего клона
    # (мост/ARP) догоняет не сразу, а между стартом тела и run_argv пробы
    # нет. SOLUTION: until_ok -- повторять пробу тем же соединением, отказ
    # после повторов громкий, с последней причиной.
    # RESULT: 4 проверки. STATUS: FIXED — see #73
    try:
        slept = []

        def flaky(results):
            it = iter(results)
            return lambda: next(it)
        got = driver.until_ok(flaky([(False, "timed out"), (False, "timed out"),
                                     (True, None)]), 5, 2, slept.append)
        c.check("until_ok must retry until the probe passes",
                not (got != (True, None, 3) or slept != [2, 2]), f"{got}, slept {slept}")
        slept.clear()
        got = driver.until_ok(flaky([(True, None)]), 5, 2, slept.append)
        c.check("a healthy body must cost one probe and no pause",
                not (got != (True, None, 1) or slept), f"{got}, slept {slept}")
        slept.clear()
        got = driver.until_ok(flaky([(False, f"try {i}") for i in range(3)]), 3, 2,
                              slept.append)
        c.expect("after the last try the refusal carries the last reason",
                 got, (False, "try 2", 3))
        c.expect("no pause after the last try", slept, [2, 2])
    except AttributeError:
        c.fail("driver.until_ok is missing")

    check_pve_facts(c)

    try:
        check_contract_151(c)
    except Exception as e:
        c.fail("check_contract_151", f"{type(e).__name__}: {e}")

    try:
        check_driver_rule_175(c)
    except Exception as e:
        c.fail("check_driver_rule_175", f"{type(e).__name__}: {e}")

    try:
        check_timeouts_171(c)
    except Exception as e:
        c.fail("check_timeouts_171", f"{type(e).__name__}: {e}")

    try:
        check_body_gone_267(c)
    except Exception as e:
        c.fail("check_body_gone_267", f"{type(e).__name__}: {e}")
    try:
        check_bootstrap_refusal_333(c)
    except Exception as e:
        c.fail("check_bootstrap_refusal_333", f"{type(e).__name__}: {e}")
    try:
        check_seed_clears_mark_312(c)
    except Exception as e:
        c.fail("check_seed_clears_mark_312", f"{type(e).__name__}: {e}")

    try:
        check_clone_lock_195(c)
    except Exception as e:
        c.fail("check_clone_lock_195", f"{type(e).__name__}: {e}")

    try:
        check_body_memory_197(c)
    except Exception as e:
        c.fail("check_body_memory_197", f"{type(e).__name__}: {e}")

    try:
        check_host_address_200(c)
    except Exception as e:
        c.fail("check_host_address_200", f"{type(e).__name__}: {e}")

    try:
        check_ssh_port_201(c)
    except Exception as e:
        c.fail("check_ssh_port_201", f"{type(e).__name__}: {e}")

    try:
        check_clone_before_bootstrap_247(c)
    except Exception as e:
        c.fail("check_clone_before_bootstrap_247", f"{type(e).__name__}: {e}")

    return c.report("driver")


# ── клон до bootstrap (#247) ────────────────────────────────────────────
# HYPOTHESIS: `mop driver run` зовёт сервер играть bootstrap сразу после
# ensure, а клон в теле делает внутренний врапер уже после ответа сервера.
# На первом старте контейнерного тела клона ещё нет, и задача проекта с
# chdir на mop_clone падает: «Unable to change directory» (pu-rudesktop-1).
# На host-узле клон переживает папетов, поэтому там не всплывало.
# SOLUTION: подготовка клона (снять старую сессию, освободить каталог,
# перенацелить, клонировать с зеркалом) -- одна константа driver.CLONE_SH.
# Её текст едет во врапере как раньше (слепок tests/spec.py не меняется), и
# её же `mop driver run` исполняет в теле ДО bootstrap: run.clone_script --
# прелюдия из окружения задачи, охрана пустого PU_CLONE, сниппет. Старая
# спека продолжает работать: её врапер видит .git и клонировать не идёт.
# RESULT: врапер несёт CLONE_SH дословно один раз, стадия run собирается из
# прелюдии, охраны и того же сниппета, и в run.main стоит до bootstrap.run.
# STATUS: FIXED — see #247
def check_clone_before_bootstrap_247(c):
    import inspect
    import subprocess
    from mop.server import spec
    from mop.cli.driver import run

    def valid_bash(s):
        return subprocess.run(["bash", "-n"], input=s, text=True,
                              capture_output=True).returncode == 0

    snippet = driver.CLONE_SH
    # Одно определение: врапер несёт сниппет дословно и один раз.
    n = spec.WRAPPER.count(snippet)
    c.check("#247 wrapper carries CLONE_SH verbatim, once", n == 1, f"got {n!r}")
    c.check("#247 CLONE_SH clones with the mirror and retargets",
            "git clone -q --reference" in snippet and 'rm -rf "$d"' in snippet
            and "kill-session" in snippet, f"got {snippet!r}")
    env = {"PU_CARRY": "PU_NAME,PU_ORIGIN,PU_PROJECT,PU_CLONE,HOME",
           "PU_NAME": "pu-mop-1", "PU_ORIGIN": "git@h:o/mop.git",
           "PU_PROJECT": "mop", "PU_CLONE": "/home/pool/puppets/pu-mop-1",
           "HOME": "/home/pool", "PU_WRAPPER": "not-carried"}
    stage = run.clone_script(env)
    c.check("#247 stage exports the task env, not the wrapper",
            "export PU_ORIGIN=git@h:o/mop.git\n" in stage
            and "export PU_CLONE=/home/pool/puppets/pu-mop-1\n" in stage
            and "not-carried" not in stage, f"got {stage!r}")
    c.check("#247 stage stops at the first failure",
            "set -e" in stage.split(snippet)[0], f"got {stage!r}")
    c.check("#247 stage refuses an empty PU_CLONE before any rm -rf",
            "${PU_CLONE:?" in stage
            and stage.index("${PU_CLONE:?") < stage.index('rm -rf "$d"'), f"got {stage!r}")
    # После сниппета -- только запись дома клона (#272).
    c.check("#247 stage carries CLONE_SH once, then only the home record",
            stage.count(snippet) == 1 and "mop.home" in stage.split(snippet)[1]
            and "clone -q" not in stage.split(snippet)[1], f"got {stage!r}")
    # #256: ветка мастера из меты джоба (NOMAD_META_branch) -- checkout после
    # свежего клона; существующий клон не переключается, там может быть
    # работа; нет такой ветки в origin -- завести локально, мастер создаст
    # её первым landing. STATUS: FIXED — see #256
    c.check("#247 no branch, no checkout", "checkout" not in stage, f"got {stage!r}")
    with_b = run.clone_script(dict(env, NOMAD_META_branch="swarm"))
    c.check("#247 branch stage still carries CLONE_SH", snippet in with_b, f"got {with_b!r}")
    c.check("#247 branch stage exports PU_BRANCH", "export PU_BRANCH=swarm\n" in with_b,
            f"got {with_b!r}")
    c.check("#247 checkout comes after the clone, for a fresh clone only",
            with_b.index("checkout") > with_b.index(snippet)
            and "fresh" in with_b and 'git -C "$d" checkout -q "$PU_BRANCH"' in with_b
            and 'checkout -q -b "$PU_BRANCH"' in with_b, f"got {with_b!r}")
    c.check("#247 branch stage is valid bash", valid_bash(with_b), f"got {with_b!r}")
    # #272: дом клона пишется на каждом старте, не только у свежего клона:
    # существующий клон узнаёт его тоже. С веткой в мете -- она, без неё --
    # ветка по умолчанию из origin/HEAD. STATUS: FIXED — see #272
    tail = with_b.split(snippet)[1]
    c.check("#247 #272: branch stage records mop.home = PU_BRANCH on every start",
            'config mop.home "$PU_BRANCH"' in tail
            and tail.index("mop.home") > tail.index("fi\n"), f"got {tail!r}")
    plain = stage.split(snippet)[1]
    c.check("#247 #272: no branch -- mop.home is the default branch from origin/HEAD",
            "origin/HEAD" in plain and "config mop.home" in plain, f"got {plain!r}")
    c.check("#247 #272: plain stage is valid bash", valid_bash(stage), f"got {stage!r}")
    # Порядок в run.main: клон исполняется в теле до вызова сервера.
    src = inspect.getsource(run.main)
    c.check("#247 run.main clones before bootstrap",
            "clone_script(" in src and "bootstrap_sandbox(" in src
            and src.index("clone_script(") < src.index("bootstrap_sandbox("), f"got {src!r}")

# ── адрес узла со стороны сервера -- из кредов шины узла (#200) ─────────
# HYPOTHESIS: host.address выбирает маршрут к config.get("MOP_SERVER_LAN"), а
# этой настройки на узле нет: её нет в NODE_SCOPED, и в node.env она не
# приезжает. Пустой адрес -- маршрут к localhost, узел отдаёт bootstrap'у
# 127.0.0.1, и сервер ходит ssh'ем сам в себя. На сервере-узле это случайно
# верно, у pve адрес контейнерный -- поэтому до первого удалённого host-узла
# (gpu, wate-wsl) не проявлялось.
# SOLUTION: адрес сервера узел берёт из того, чем уже ходит к нему, -- из url
# своих кредов шины (bus.NODE_FILE): плейбук рендерит туда тот же
# MOP_SERVER_LAN (deploy/setup.yml, nats_lan). Вторая копия факта в
# NODE_SCOPED разъехалась бы с первой. Нет файла, нет url, имя не резолвится
# -- отказ с названием источника, а не 127.0.0.1.
# RESULT: маршрут выбирается к хосту из url файла шины; четыре отказа
# называют файл, пятый -- имя, которое не резолвится.
# STATUS: FIXED — see #200
def check_host_address_200(c):
    from mop.driver import host

    stub, targets = udp_socket("192.0.2.77", unresolvable=("nowhere.invalid",))
    with offline(), restored(host, "socket", "NODE_FILE"):
        # Настройка на узле -- не тот сервер: ответ обязан прийти из файла шины.
        os.environ["MOP_SERVER_LAN"] = "198.51.100.9"
        host.socket = stub
        with tempfile.TemporaryDirectory() as tmp:
            def bus_file(body):
                path = os.path.join(tmp, "bus.json")
                with open(path, "w") as f:
                    f.write(body)
                host.NODE_FILE = path
                return path

            def refusal(what, *words):
                try:
                    got = host.address("pu-mop-1")
                except RuntimeError as e:
                    missing = [w for w in words if w not in str(e)]
                    c.check(f"{what}: the refusal names {list(words)}", not missing,
                            f"refusal {str(e)!r} doesn't name {missing}")
                    return
                c.fail(f"{what}: wanted a refusal", f"answered {got!r}")

            bus_file('{"url": "nats://192.0.2.1:4222", "user": "node", "password": "x"}')
            del targets[:]
            c.expect("address from the node's bus url", host.address("pu-mop-1"), "192.0.2.77")
            c.expect("route chosen toward the bus server", targets, [("192.0.2.1", 1)])

            bus_file('{"url": "nats://server.example:4222", "user": "node", "password": "x"}')
            del targets[:]
            host.address("pu-mop-1")
            c.expect("a server name routes by name", targets, [("server.example", 1)])

            path = os.path.join(tmp, "absent.json")
            host.NODE_FILE = path
            refusal("no bus file", path)
            path = bus_file('{"user": "node", "password": "x"}')
            refusal("bus file without url", path, "url")
            path = bus_file('{"url": "nats://:4222"}')
            refusal("bus url without a host", path)
            path = bus_file('not json')
            refusal("unreadable bus file", path)
            path = bus_file('{"url": "nats://nowhere.invalid:4222"}')
            refusal("unresolvable server", path, "nowhere.invalid")


# ── ssh-порт host-узла -- узловая настройка (#201) ──────────────────────
# HYPOTHESIS: host.address отдаёт голый адрес, и сервер ходит на узел портом
# 22; у wate-wsl (WSL) sshd на 2222, а на 22 отвечает другой -- вход
# отвергнут. Порт знал только ssh config root'а на контроллере.
# SOLUTION: порт -- узловая настройка MOP_SSH_PORT (node.env из ansible_port
# инвентаря, по умолчанию 22). Не 22 -- адрес `адрес:порт`; 22 -- голый
# адрес, как сегодня. Не порт -- отказ с именем настройки.
# STATUS: FIXED — see #201
def check_ssh_port_201(c):
    from mop.driver import host

    stub, _ = udp_socket("192.168.1.37")
    with offline(), restored(host, "socket", "NODE_FILE"), patched_env(MOP_SSH_PORT=None):
        host.socket = stub
        with tempfile.TemporaryDirectory() as tmp:
            host.NODE_FILE = os.path.join(tmp, "bus.json")
            with open(host.NODE_FILE, "w") as f:
                f.write('{"url": "nats://192.0.2.1:4222"}')
            os.environ.pop("MOP_SSH_PORT", None)
            c.expect("config: MOP_SSH_PORT defaults to 22",
                     config.SETTINGS.get("MOP_SSH_PORT"), "22")
            c.expect("config: MOP_SSH_PORT reaches the node",
                     "MOP_SSH_PORT" in config.NODE_SCOPED, True)
            c.expect("no port setting: the bare address, as today",
                     host.address("pu-mop-1"), "192.168.1.37")
            os.environ["MOP_SSH_PORT"] = "22"
            c.expect("port 22: the bare address", host.address("pu-mop-1"), "192.168.1.37")
            os.environ["MOP_SSH_PORT"] = "2222"
            c.expect("port 2222: address:port", host.address("pu-mop-1"), "192.168.1.37:2222")
            for garbage in ("22x", "0", "70000", " "):
                os.environ["MOP_SSH_PORT"] = garbage
                try:
                    got = host.address("pu-mop-1")
                except RuntimeError as e:
                    c.check(f"port {garbage!r}: the refusal names MOP_SSH_PORT",
                            "MOP_SSH_PORT" in str(e), f"refusal {str(e)!r}")
                    continue
                c.fail(f"port {garbage!r}: wanted a refusal", f"answered {got!r}")


# ── память тела -- из спеки, на каждом подъёме (#197) ───────────────────
# HYPOTHESIS: память pve-тела -- `pct --memory` образа, испечённая сборкой из
# `.mop` на тот момент; спека папета (MemoryMaxMB) до тела не доходит, и
# стоящее тело не меняется никогда.
# SOLUTION: спека несёт потолок в PU_MEM_MB, `mop driver run` отдаёт его
# ensure, и ensure до start ставит телу память глаголом `mop-pve memory` --
# и новому клону, и стоящему телу. Спека до #197 PU_MEM_MB не несёт: тогда
# память тела не трогается, как было.
# STATUS: FIXED — see #197
def check_body_memory_197(c):
    import asyncio
    from mop.driver import pve
    from mop.cli.driver import run

    name = "pu-mop-1"
    vmid = pve.vmid_of(name)

    def fake(fail=None, code=1):
        """sh: список вызовов глаголов обёртки; fail -- глагол, который
        отвечает кодом code (None -- таймаут)."""
        calls = []

        async def sh(script, timeout=20, prefix=()):
            words = [w.strip("'") for w in script.split()]
            verb = next((w for w in words if w in (
                "list", "clone", "net", "memory", "start", "push", "exec")), None)
            calls.append((verb, words))
            if verb == fail:
                # Таймаут приходит без вывода, как у настоящего sh.
                return ("no such thing\n" if code is not None else ""), code
            return "", 0
        return sh, calls

    def verbs(calls):
        return [v for v, _ in calls if v]

    async def standing(_v):
        return name

    async def empty(_v):
        return ""

    with restored(pve, "sh", "SSH_KEY", "_seed_files", "_hostname"), \
            tempfile.TemporaryDirectory() as d:
        key = os.path.join(d, "mop-body")
        with open(key + ".pub", "w") as f:
            f.write("ssh-ed25519 AAAA node\n")
        pve.SSH_KEY = key
        pve._seed_files = lambda: []

        for what, stand in (("a standing body", standing), ("a fresh clone", empty)):
            pve._hostname = stand
            pve.sh, calls = fake()
            r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": "16384"}))
            c.check(f"#197 {what}: ensure succeeds", not r.get("error"), f"got {r!r}")
            mem = [w for v, w in calls if v == "memory"]
            c.check(f"#197 {what}: memory is set once, to the spec's ceiling",
                    len(mem) == 1 and mem[0][-2:] == [str(vmid), "16384"], f"got {mem!r}")
            v = verbs(calls)
            c.check(f"#197 {what}: memory is set before start",
                    "memory" in v and "start" in v and v.index("memory") < v.index("start"),
                    f"got {v!r}")

        pve._hostname = standing
        pve.sh, calls = fake()
        r = asyncio.run(pve.ensure(name, {"project": "mop"}))
        v = verbs(calls)
        c.check("#197 a spec before #197: the body's memory is left alone",
                "memory" not in v and "start" in v, f"got {v!r}")

        pve.sh, calls = fake("memory")
        r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": "16384"}))
        c.check("#197 memory refused: ensure refuses, naming the body and the size",
                str(vmid) in (r.get("error") or "")
                and "16384" in r["error"] and "no such thing" in r["error"], f"got {r!r}")
        v = verbs(calls)
        c.check("#197 memory refused: the body is not started", "start" not in v,
                f"got {v!r}")
        pve.sh, calls = fake("memory", None)
        r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": "16384"}))
        c.check("#197 memory timed out: a refusal, not a success",
                "timed out" in (r.get("error") or ""), f"got {r!r}")

        for bad_mem in ("lots", "0", "-1", "8G"):
            pve.sh, calls = fake()
            r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": bad_mem}))
            v = verbs(calls)
            c.check(f"#197 mem {bad_mem!r}: refused before the hypervisor",
                    "PU_MEM_MB" in (r.get("error") or "")
                    and "memory" not in v and "start" not in v, f"got {(r, v)!r}")

    # `mop driver run` отдаёт ensure потолок из окружения задачи.
    c.expect("#197 run: params carry the spec's ceiling",
             run.ensure_params(name, {"PU_MEM_MB": "16384"}),
             {"project": "mop", "mem": "16384"})
    c.expect("#197 run: a spec before #197 carries no mem",
             run.ensure_params(name, {}), {"project": "mop"})
    c.expect("#197 run: an empty PU_MEM_MB is no mem",
             run.ensure_params(name, {"PU_MEM_MB": ""}), {"project": "mop"})


# ── имя драйвера узла -- одно правило (#175) ────────────────────────────
# HYPOTHESIS: of_node оставлял пустой mop_driver пустым (is_container("") ->
# True, узел читался контейнерным), а четыре других места делали из пустого
# host (`or DEFAULT`); неизвестное имя -- опечатка в инвентаре -- было
# «контейнером» для сборщика и объявления образов и падало в mop delete.
# SOLUTION: of_node -- единственное правило: нет ключа или пусто -> DEFAULT
# (узел, настроенный до поля), неизвестное -> громкий отказ с узлом и
# значением. Остальные места -- через него.
# STATUS: FIXED — see #175
def check_driver_rule_175(c):
    import re

    for meta, want in ((None, driver.DEFAULT), ({}, driver.DEFAULT),
                       ({"mop_driver": ""}, driver.DEFAULT),
                       ({"mop_driver": "host"}, "host"), ({"mop_driver": "pve"}, "pve")):
        try:
            got = driver.of_node(meta, "n1")
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        c.check(f"#175 of_node({meta!r}) -> {got!r}, wanted {want!r}", got == want)
    c.check("#175 is_container through of_node: empty is host",
            driver.is_container(driver.of_node({"mop_driver": ""}, "n1")) is False)
    c.check("#175 is_container through of_node: pve is a container",
            driver.is_container(driver.of_node({"mop_driver": "pve"}, "n1")) is True)
    try:
        driver.of_node({"mop_driver": "bogus"}, "hyper")
        c.fail("#175 of_node of an unknown driver must refuse")
    except RuntimeError as e:
        c.check(f"#175 the refusal names the node and the value: {e}",
                "hyper" in str(e) and "bogus" in str(e) and "\n" not in str(e))
    # Эта машина -- то же правило.
    with patched_env(MOP_DRIVER="bogus"):
        try:
            driver.current_name()
            c.fail("#175 current_name of an unknown MOP_DRIVER must refuse")
        except RuntimeError as e:
            c.check("#175 current_name names the value", "bogus" in str(e))
    # Правило одно: `or DEFAULT` для имени драйвера -- только в of_node.
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    found = []
    for top, _, files in os.walk(os.path.join(root, "mop")):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(top, f)
                for i, line in enumerate(open(path), 1):
                    if re.search(r"or (driver\.)?DEFAULT\b", line):
                        found.append(f"{os.path.relpath(path, root)}:{i}")
    # Списки не слепнут от чужой опечатки: узел с неизвестным драйвером --
    # строка с отказом, остальные видны. Операции над ним (delete, attach,
    # build на нём) отказывают громко -- это of_node выше.
    from mop.server import builder, image, nodes, nomad
    metas = {"bad": {"mop_driver": "bogus"}, "hyper": {"mop_driver": "pve"}}
    summary = lambda n: {"Name": n, "Status": "ready"}
    try:
        rows = [nodes.row(summary(n), metas[n], {}) for n in sorted(metas)]
        got = {r["name"]: r for r in rows}
        c.check("#175 nodes.row: the good node is visible",
                got["hyper"]["driver"] == "pve" and not got["hyper"].get("error"))
        c.check(f"#175 nodes.row: the bad node is a row with the refusal: {got['bad']}",
                "bad: unknown driver 'bogus'" in (got["bad"].get("error") or ""))
    except Exception as e:
        c.fail(f"#175 nodes.row with a bad node: {type(e).__name__}: {e}")
    with patched(nomad, nodes_meta=lambda: metas,
                 node_dynamic_meta=lambda n: {"mop_projects": "mop"},
                 set_node_meta=lambda n, m: None):
        try:
            refused = []
            serving = builder.serving_now(refused)
            c.check(f"#175 serving_now: the good node counts: {serving}",
                    serving == {"hyper": ["mop"]})
            c.check(f"#175 serving_now: the bad node is reported: {refused}",
                    any("bad: unknown driver 'bogus'" in r for r in refused))
        except Exception as e:
            c.fail(f"#175 serving_now with a bad node: {type(e).__name__}: {e}")
        try:
            got = dict(image.announce("mop"))
            c.check(f"#175 announce: the good node answers: {got}",
                    got.get("hyper") == "already announced")
            c.check(f"#175 announce: the bad node carries the refusal: {got}",
                    "bad: unknown driver 'bogus'" in (got.get("bad") or ""))
        except Exception as e:
            c.fail(f"#175 announce with a bad node: {type(e).__name__}: {e}")
    c.check(f"#175 `or DEFAULT` outside of_node: {found}",
            all(x.startswith("mop/driver/__init__.py") for x in found) and len(found) <= 1)


def check_body_gone_267(c):
    """HYPOTHESIS (#267): ответы драйверов разные по драйверу -- ensure: body
    None у host и vmid у pve; destroy: {reset, target} у host и {destroyed,
    target} у pve, -- и каждый читатель (driver run, sweep, wipe агента)
    разбирает словарь своими ключами.
    SOLUTION: domain.Body(name, vmid, address, created) и domain.Gone(target,
    vmid, reset) -- одно значение на оба драйвера; драйвер строит его, а по
    шине и в ответе уходит прежний словарь (to_dict). STATUS: FIXED — see #267"""
    import asyncio
    from mop.common import domain
    from mop.driver import host, pve

    Body, Gone = getattr(domain, "Body", None), getattr(domain, "Gone", None)
    if not c.check("#267 domain.Body / domain.Gone exist", not (Body is None or Gone is None)):
        return

    ok_sh = canned("", 0)

    name = "pu-mop-1"

    async def standing(_v):
        return name
    with restored(host, "sh"), restored(pve, "sh", "SSH_KEY", "_seed_files", "_hostname"), \
            tempfile.TemporaryDirectory() as d:
        key = os.path.join(d, "mop-body")
        with open(key + ".pub", "w") as f:
            f.write("ssh-ed25519 AAAA node\n")
        pve.SSH_KEY, pve._seed_files, pve._hostname = key, (lambda: []), standing
        host.sh = pve.sh = ok_sh
        vmid = pve.vmid_of(name)

        r = asyncio.run(host.ensure(name))
        c.expect("#267 host.ensure wire", r, {"name": name, "body": None, "created": False,
                                              "address": None})
        c.expect("#267 host.ensure value", Body.from_dict(r), Body(name))
        c.expect("#267 host.ensure round trip", Body.from_dict(r).to_dict(), r)
        r = asyncio.run(pve.ensure(name))
        b = Body.from_dict(r)
        c.expect("#267 pve.ensure value", (b.name, b.vmid, b.created), (name, vmid, False))
        c.expect("#267 pve.ensure round trip", b.to_dict(), r)

        r = asyncio.run(host.destroy(name))
        c.expect("#267 host.destroy wire keys", sorted(r), ["reset", "target"])
        g = Gone.from_dict(r)
        c.expect("#267 host.destroy value", (g.target, g.vmid, g.reset),
                 (r["target"], None, True))
        c.expect("#267 host.destroy round trip", g.to_dict(), r)
        r = asyncio.run(pve.destroy(name))
        c.expect("#267 pve.destroy wire", r, {"destroyed": vmid, "target": f"body {vmid}"})
        g = Gone.from_dict(r)
        c.expect("#267 pve.destroy value", (g.target, g.vmid, g.reset),
                 (f"body {vmid}", vmid, False))
        c.expect("#267 pve.destroy round trip", g.to_dict(), r)
        c.expect("#267 a refusal is no value", (Body.from_dict({"error": "x"}),
                                                Gone.from_dict({"error": "x"})), (None, None))
    # Драйверы строят значение, читатели читают его -- не ключи словаря.
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for rel, needle in (("mop/driver/host.py", "Body("), ("mop/driver/host.py", "Gone("),
                        ("mop/driver/pve.py", "Body("), ("mop/driver/pve.py", "Gone("),
                        ("mop/cli/driver/run.py", "Body.from_dict("),
                        ("mop/cli/driver/sweep.py", "Gone.from_dict(")):
        c.expect(f"#267 {rel} uses {needle}",
                 needle in open(os.path.join(root, rel)).read(), True)


# ── #333: отказ врапера называет упавшую задачу bootstrap ─────────────
# HYPOTHESIS: `mop driver run` печатает «bootstrap of <папет> failed:
# bootstrap failed (ansible exit N):» и хвост -- без задачи, хотя ответ
# сервера её теперь несёт.
# SOLUTION: BootstrapFailed(ответ) и чистая refusal(имя, ошибка): задача
# есть -- «bootstrap of <имя> failed at task «<задача>»: <сообщение>», затем
# хвост, как сегодня; нет -- прежний текст. STATUS: FIXED — see #333
def check_bootstrap_refusal_333(c):
    from mop.cli.driver import run
    if not hasattr(run, "refusal") or not hasattr(run, "BootstrapFailed"):
        c.fail("#333 no run.refusal / run.BootstrapFailed")
        return
    reply = {"ok": False, "played": True, "rc": 2, "tail": "TASK [bootstrap : env file]\nfatal: ...",
             "task": "bootstrap : env file", "message": "Could not find '.env-prod'"}
    c.expect("#333 a named task", run.refusal("pu-rugent-3", run.BootstrapFailed(reply)),
             "bootstrap of pu-rugent-3 failed at task «bootstrap : env file»: "
             "Could not find '.env-prod'\nTASK [bootstrap : env file]\nfatal: ...")
    old = dict(reply, task=None, message=None)
    c.expect("#333 no task: the text as before",
             run.refusal("pu-rugent-3", run.BootstrapFailed(old)),
             "bootstrap of pu-rugent-3 failed: bootstrap failed (ansible exit 2):\n"
             "TASK [bootstrap : env file]\nfatal: ...")
    c.expect("#333 a server from before #333 (no keys): as before",
             run.refusal("pu-x-1", run.BootstrapFailed({"ok": False, "rc": 4, "tail": "t"})),
             "bootstrap of pu-x-1 failed: bootstrap failed (ansible exit 4):\nt")
    c.expect("#333 any other refusal: as before",
             run.refusal("pu-x-1", RuntimeError("cannot let the server into the body: no key")),
             "bootstrap of pu-x-1 failed: cannot let the server into the body: no key")
    c.check("#333 BootstrapFailed is still a RuntimeError",
            issubclass(run.BootstrapFailed, RuntimeError))


# ── #312: метка аренды -- только от удачной раздачи этого подъёма ──────
# HYPOTHESIS: тело pve переживает рестарт вместе с меткой аренды
# (.local/state/mop/cred). Аренду сняли (`mop update` без --cred) -- метка
# осталась, и операторский `mop login` без адреса (#312, слой 1) обходит это
# тело: протухший логин лечится только рестартом, то есть ценой разговора.
# SOLUTION: _seed на каждом подъёме снимает метку в теле ДО bootstrap'а;
# cred_push после прогона кладёт файлы и метку заново, если аренда есть.
# Инвариант: метка есть тогда и только тогда, когда раздача этого подъёма
# удалась. У host метку узла на подъёме не трогаем: $HOME общий (слой 4).
# STATUS: FIXED — see #312
def check_seed_clears_mark_312(c):
    import asyncio
    from mop.common import paths
    from mop.driver import pve
    clear = getattr(pve, "_seed_clear", None)
    if clear is None:
        c.fail("#312 no pve._seed_clear: the lease mark outlives a dropped lease")
        return
    mark = os.path.join(pve.HOME, paths.CRED_MARK)
    c.expect("#312 seed plan: the lease mark is cleared in the body", clear(), [mark])
    ran = []

    async def sh(script, timeout=20, prefix=()):
        ran.append(script)
        return "", 0
    with restored(pve, "sh", "_seed_files"):
        pve.sh = sh
        pve._seed_files = lambda: []
        got = asyncio.run(pve._seed("pu-mop-1", 9001))
    removed = [s for s in ran if "exec" in s and "rm -f" in s and mark in s]
    c.check("#312 seed: rm -f of the mark inside body 9001, and the seed succeeds",
            got == {} and len(removed) == 1 and "9001" in removed[0], (got, ran))

    async def refusing(script, timeout=20, prefix=()):
        return "no such body", 1
    with restored(pve, "sh", "_seed_files"):
        pve.sh = refusing
        pve._seed_files = lambda: []
        got = asyncio.run(pve._seed("pu-mop-1", 9001))
    c.check("#312 seed: a mark that cannot be cleared refuses the start, naming it",
            "cred" in (got.get("error") or ""), got)


# ── таймаут шелла -- не успех (#171) ─────────────────────────────────────
# HYPOTHESIS: sh() на таймауте отдаёт ("", None) -- намеренно отдельно от
# ненулевого кода, -- а мутирующие глаголы проверяли `code not in (0, None)`
# и читали «не успел» как «сделал»: host.destroy по недоделанному
# `rm -rf target` отвечал {"reset": True}, ensure поднимал тело, которое не
# стартовало.
# SOLUTION: мутирующие глаголы считают None отказом, причина -- why(),
# которая для None говорит «timed out after Ns». Пробы только для чтения
# (перечисление тел, шаблоны, ёмкость) остаются как были. Имя контейнера --
# проба, но по ней ensure решает, клонировать ли: таймаут там -- отказ ensure,
# а не клон поверх занятого vmid.
# STATUS: FIXED — see #171
def check_timeouts_171(c):
    import asyncio
    from mop.driver import host, pve

    def fake_sh(fail=None, everything=False):
        """sh, у которого «не успевает» команда с этим словом (или любая)."""
        async def sh(script, timeout=20, prefix=()):
            words = script.split()
            if everything or (fail and fail in words):
                return "", None
            return "", 0
        return sh

    timed_out = lambda r: isinstance(r, dict) and "timed out" in (r.get("error") or "")
    real_hostname = pve._hostname
    name = "pu-mop-1"
    with restored(host, "sh"), restored(pve, "sh", "SSH_KEY", "_seed_files", "_hostname"), \
            tempfile.TemporaryDirectory() as d:
        key = os.path.join(d, "mop-body")
        with open(key + ".pub", "w") as f:
            f.write("ssh-ed25519 AAAA node\n")
        seed = os.path.join(d, "seed")
        with open(seed, "w") as f:
            f.write("x")
        pve.SSH_KEY = key
        pve._seed_files = lambda: [(seed, "600")]
        vmid = pve.vmid_of(name)

        # host.destroy: и git, и rm -rf target.
        host.sh = fake_sh("reset")
        r = asyncio.run(host.destroy(name))
        c.check("#171 host.destroy, git timed out", timed_out(r), f"got {r!r}")
        host.sh = fake_sh("-rf")
        r = asyncio.run(host.destroy(name))
        c.check("#171 host.destroy, rm timed out", timed_out(r), f"got {r!r}")
        c.check("#171 host.destroy, rm: the timeout is named",
                "600s" in (r.get("error") or ""), f"got {r!r}")
        host.sh = fake_sh()
        r = asyncio.run(host.destroy(name))
        c.check("#171 host.destroy, success as before", r.get("reset") is True, f"got {r!r}")
        # #257: после сноса клон встаёт на ветку мастера, если её назвали:
        # -B на origin/<ветка> после fetch, иначе локально от HEAD; без
        # ветки -- как было, никакого checkout. STATUS: FIXED — see #257
        ran = []

        async def recording_sh(script, timeout=20, prefix=()):
            ran.append(script)
            return "", 0
        host.sh = recording_sh
        asyncio.run(host.destroy(name))
        c.check("#171 host.destroy without a branch runs no checkout",
                not any("checkout" in s for s in ran), f"got {ran!r}")
        ran.clear()
        r = asyncio.run(host.destroy(name, branch="swarm"))
        c.check("#171 host.destroy with a branch checks it out after the reset",
                any("checkout -q -B swarm origin/swarm" in s for s in ran)
                and any("|| git" in s and "checkout -q -B swarm" in s.split("||")[-1]
                        and "origin/swarm" not in s.split("||")[-1] for s in ran)
                and ran.index(next(s for s in ran if "checkout" in s))
                > ran.index(next(s for s in ran if "reset --hard" in s)), f"got {ran!r}")
        # #272: wipe на ветку мастера пишет её же домом клона; без ветки
        # дом не трогается. STATUS: FIXED — see #272
        c.check("#171 #272: host.destroy with a branch records mop.home",
                any("config mop.home swarm" in s for s in ran), f"got {ran!r}")
        ran.clear()
        asyncio.run(host.destroy(name))
        c.check("#171 #272: host.destroy without a branch leaves mop.home alone",
                not any("mop.home" in s for s in ran), f"got {ran!r}")
        host.sh = fake_sh()

        # pve: каждый мутирующий шаг по отдельности.
        async def standing(_v):
            return name

        async def empty(_v):
            return ""
        for what, fail, stand, fn in (
                ("ensure: clone", "clone", empty, lambda: pve.ensure(name)),
                ("ensure: net", "net", standing, lambda: pve.ensure(name)),
                ("ensure: start", "start", standing, lambda: pve.ensure(name)),
                ("ensure: package push", "push", standing,
                 lambda: pve._sync_package(name, vmid)),
                ("ensure: package unpack", "exec", standing,
                 lambda: pve._sync_package(name, vmid)),
                ("ensure: seed", "push", standing, lambda: pve._seed(name, vmid)),
                ("admit", "keys", standing, lambda: pve.admit(name, False)),
                ("push_many", "unpack", standing,
                 lambda: pve.push_many(name, [(f"{pve.HOME}/.claude.json", b"{}")])),
                ("destroy", "destroy", standing, lambda: pve.destroy(name))):
            pve._hostname = stand
            pve.sh = fake_sh(fail)
            r = asyncio.run(fn())
            c.check(f"#171 pve.{what} timed out", timed_out(r), f"got {r!r}")

        pve._hostname = standing
        pve.sh = fake_sh()
        r = asyncio.run(pve.ensure(name))
        c.check("#171 pve.ensure, success as before",
                r.get("body") == vmid and not r.get("error"), f"got {r!r}")
        r = asyncio.run(pve.destroy(name))
        c.check("#171 pve.destroy, success as before", r.get("destroyed") == vmid,
                f"got {r!r}")
        c.expect("#171 pve.admit, success as before", asyncio.run(pve.admit(name, False)),
                 {"admitted": False})

        # Пробы только для чтения -- ответ прежний (характеризация до правки).
        pve._hostname = real_hostname
        pve.sh = fake_sh(everything=True)
        c.expect("#171 pve.bodies tolerates", asyncio.run(pve.bodies()), [])
        c.expect("#171 pve.templates tolerates", asyncio.run(pve.templates()), [])
        # _hostname кормит мутирующее решение ensure (клонировать или
        # поднять стоящее): таймаут -- «не знаю» (None), а не «пусто».
        r = asyncio.run(pve._hostname(vmid))
        c.check("#171 pve._hostname: timeout is unknown, not absent", r is None,
                f"got {r!r}")
        calls = []

        async def list_times_out(script, timeout=20, prefix=()):
            calls.append(script.split())
            return ("", None) if "list" in script.split() else ("", 0)
        pve.sh = list_times_out
        r = asyncio.run(pve.ensure(name))
        c.check("#171 pve.ensure: list timed out -> refusal", timed_out(r), f"got {r!r}")
        c.check("#171 pve.ensure: list timed out -> no clone",
                not any("clone" in w for w in calls), f"got {calls!r}")

        async def list_fails(script, timeout=20, prefix=()):
            return ("", 1) if "list" in script.split() else ("", 0)
        pve.sh = list_fails
        c.expect("#171 pve._hostname: non-zero exit is absent, as before",
                 asyncio.run(pve._hostname(vmid)), "")
        pve.sh = fake_sh(everything=True)
        r = asyncio.run(pve.capacity())
        c.check("#171 pve.capacity is an error, as before", bool(r.get("error")),
                f"got {r!r}")
        host.sh = fake_sh(everything=True)
        r = asyncio.run(host.capacity())
        c.check("#171 host.capacity is an error, as before", bool(r.get("error")),
                f"got {r!r}")

        # why(): ненулевой код -- как было, None -- «timed out».
        c.expect("#171 why: output wins", driver.why(" boom \n", 1), "boom")
        c.expect("#171 why: exit code", driver.why("", 2), "exit 2")
        c.expect("#171 why: timeout with N", driver.why("", None, 30),
                 "timed out after 30s")


# ── блокировка шаблона на время чужого клона (#195) ─────────────────────
# Наблюдение: после `mop driver build mop` на hyper четыре тела поднимались
# разом, три из четырёх упали с «CT is locked (disk); build the project's
# image: mop driver build mop» -- Proxmox держит блокировку шаблона на время
# клона, и соседний клон получает отказ. Nomad перезапустил задачи, и за
# ~3 минуты поднялись все: образ был на месте, совет пересобрать его --
# ложный и вёл бы в ту же блокировку.
# HYPOTHESIS: ensure сводит любой отказ `mop-pve clone` к «собери образ».
# SOLUTION: отказ по блокировке («is locked», «can't lock file») ensure
# повторяет с паузами, в сумме 119 с; не ушла -- отказ называет блокировку и
# `pct unlock`. Совет собрать образ -- только когда шаблона нет в списке
# гипервизора; иной отказ -- своей причиной.
# RESULT: до правки 7 из 13 проверок красные, после -- все зелёные.
# STATUS: FIXED — see #195
def check_clone_lock_195(c):
    import asyncio
    from mop.driver import pve

    name, project = "pu-mop-1", "mop"
    src = pve.template_vmid(project)
    locked = f"CT is locked (disk)\n"

    def fake(clone_answers, template=True):
        """sh: list -- шаблон стоит (или нет), тела нет; clone -- ответы по
        очереди, последний повторяется; остальное -- успех."""
        calls = []

        async def sh(script, timeout=20, prefix=()):
            words = script.split()
            if "list" in words:
                return ((f"{src} {pve.template_name(project)} stopped\n"
                         if template else ""), 0)
            if "clone" in words:
                calls.append(words)
                n = min(len(calls), len(clone_answers)) - 1
                return clone_answers[n]
            return "", 0
        return sh, calls

    slept = []

    async def sleep(s):
        slept.append(s)

    with restored(pve, "sh", "SSH_KEY", "_seed_files", "_sleep"), \
            tempfile.TemporaryDirectory() as d:
        key = os.path.join(d, "mop-body")
        with open(key + ".pub", "w") as f:
            f.write("ssh-ed25519 AAAA node\n")
        seed = os.path.join(d, "seed")
        with open(seed, "w") as f:
            f.write("x")
        pve.SSH_KEY = key
        pve._seed_files = lambda: [(seed, "600")]
        pve._sleep = sleep

        # Блокировка дважды, потом клон проходит: тело поднято без рестарта.
        pve.sh, calls = fake([(locked, 1), (locked, 1), (f"{src}\n", 0)])
        r = asyncio.run(pve.ensure(name))
        c.check("#195 locked twice then ok -> body",
                not r.get("error") and r.get("created") is True, f"got {r!r}")
        c.expect("#195 locked twice then ok -> three clones", len(calls), 3)
        c.expect("#195 locked twice then ok -> two pauses", len(slept), 2)

        # Второй текст отказа по блокировке -- файл конфига под замком.
        slept.clear()
        pve.sh, calls = fake([
            ("can't lock file '/run/lock/lxc/pve-config-9001.lock' "
             "- got timeout\n", 1), (f"{src}\n", 0)])
        r = asyncio.run(pve.ensure(name))
        c.check("#195 can't lock file -> retried",
                not r.get("error") and len(calls) == 2, f"got {(r, len(calls))!r}")

        # Блокировка не уходит: отказ называет блокировку, а не сборку, и
        # ожидание ограничено.
        slept.clear()
        pve.sh, calls = fake([(locked, 1)])
        r = asyncio.run(pve.ensure(name))
        err = r.get("error") or ""
        c.check("#195 lock persists -> error", bool(r.get("error")), f"got {r!r}")
        c.check("#195 lock persists -> names the lock", "locked" in err, f"got {err!r}")
        c.check("#195 lock persists -> no build advice", "mop driver build" not in err,
                f"got {err!r}")
        c.check("#195 lock persists -> bounded wait", 60 <= sum(slept) <= 180,
                f"got {sum(slept)!r}")

        # Шаблона нет: совет собрать образ, как сегодня.
        slept.clear()
        pve.sh, calls = fake([(f"mop-pve: no container {src}\n", 64)],
                             template=False)
        r = asyncio.run(pve.ensure(name))
        err = r.get("error") or ""
        c.check("#195 no template -> build advice", f"mop driver build {project}" in err,
                f"got {err!r}")
        c.expect("#195 no template -> not retried", (len(calls), slept), (1, []))

        # Иной отказ при стоящем шаблоне: его причина, без совета.
        pve.sh, calls = fake([("storage 'local-lvm' is full\n", 255)])
        r = asyncio.run(pve.ensure(name))
        err = r.get("error") or ""
        c.check("#195 other error -> its reason", "storage 'local-lvm' is full" in err,
                f"got {err!r}")
        c.check("#195 other error -> no build advice", "mop driver build" not in err,
                f"got {err!r}")
        c.expect("#195 other error -> not retried", (len(calls), slept), (1, []))


# ── факты pve для плейбуков (#158) ───────────────────────────────────────
# Три копии `python3 -c` в плейбуках, дословно как стояли до #158 (аудит
# 5965bcd): характеристика. Ожидаемое считается ими самими, подпроцессом, с
# базой в окружении -- ровно так, как их звал ansible.
SNIPPET_IMAGE = """
import sys
sys.path.insert(0, sys.argv[1])
from mop.driver import pve
print(pve.template_vmid(sys.argv[2]), pve.template_name(sys.argv[2]),
      pve.GATEWAY, pve.PREFIXLEN, pve.template_address(sys.argv[2]))
"""
SNIPPET_STAGE = """
import sys
sys.path.insert(0, sys.argv[1])
from mop.driver import pve
listing = pve.parse_list(sys.argv[3])
v = pve.stage_vmid(sys.argv[2], listing)
print(v, pve.stage_name(sys.argv[2]), pve.address_of_vmid(v))
"""
SNIPPET_ROUTES = """
import sys
sys.path.insert(0, sys.argv[1])
from mop.driver import pve
print(" ".join(pve.routes_of(sys.argv[2], int(sys.argv[3]))))
"""


def _old(code, base, *args):
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    env = dict(os.environ, MOP_PVE_VMID_BASE=str(base))
    return subprocess.run([sys.executable, "-c", code, root, *args], env=env,
                          capture_output=True, text=True, check=True).stdout


def _jinja_build(stdout_lines, tmpl_name, stage_name, project):
    """Выражения jinja из deploy/pve-build.yml до #158, переписанные один в
    один (ansible в клоне нет -- исполнить их нечем):
      have_image: stdout_lines | select('search', ' ' ~ tmpl_name ~ ' ') | list | length > 0
      have_stage: stdout_lines | select('search', ' ' ~ stage_name ~ ' ') | list | length > 0
      project_bodies: stdout_lines | map('split') | map(attribute=1)
                      | select('match', '^pu-' ~ (mop_project | regex_escape) ~ '-[0-9]+$') | list
    """
    import re
    return {
        "have_image": len([l for l in stdout_lines if re.search(" " + tmpl_name + " ", l)]) > 0,
        "have_stage": len([l for l in stdout_lines if re.search(" " + stage_name + " ", l)]) > 0,
        "project_bodies": [n for n in (l.split()[1] for l in stdout_lines)
                           if re.match("^pu-" + re.escape(project) + "-[0-9]+$", n)],
    }


def check_pve_facts(c):
    """HYPOTHESIS (#158): факты драйвера pve плейбуки добывали тремя копиями
    `python3 -c` с sys.path и окружением, а список тел разбирали дважды --
    pve.parse_list и jinja `search ' имя '`; длину префикса сети считал ещё и
    jinja роли, а путь обёртки, ключи и VMID_MAX жили параллельно в pve.py и
    YAML.
    SOLUTION: pve.facts() -- чистая функция, `mop server pve-facts` печатает
    её JSON; плейбуки читают факты из него.
    STATUS: FIXED — see #158"""
    import json
    import subprocess

    pve = driver.module("pve")
    if not c.check("pve.facts exists", hasattr(pve, "facts")):
        return
    subnet, gateway = config.get("MOP_PVE_SUBNET"), config.get("MOP_PVE_GATEWAY")
    home = config.get("MOP_HOME")
    for base in (9000, 12000):
        # Роль pve: разбор сети jinja, VMID_MAX из vmid.yml, маршруты -- копией.
        f = pve.facts(base)
        c.expect(f"network {base}", (f["network"], str(f["prefix"]), f["gateway"]),
                (subnet.split("/")[0], subnet.split("/")[1], gateway))
        c.expect(f"vmid_max {base}", f["vmid_max"], base + 999)
        c.expect(f"routes {base}", f["routes"],
                _old(SNIPPET_ROUTES, base, subnet, str(base)).split())
        # Пути, которые YAML писал литералом.
        c.expect("wrapper", f["wrapper"], "/usr/local/sbin/mop-pve")
        c.expect("ssh_key", f["ssh_key"], f"{home}/.ssh/mop-body")
        c.expect("known_hosts", f["known_hosts"], f"{home}/.ssh/known_hosts-mop-body")
        c.expect("no project, no image", "image" in f, False)

        for project in ("mop", "rugent", "ru.gent"):
            tmpl = _old(SNIPPET_IMAGE, base, project).split()
            tn = f"pu-tmpl-{project}"
            for listing in (
                    "",
                    f"{base + 912} {tn} stopped\n{base + 3} pu-{project}-1 running\n"
                    f"{base + 4} pu-{project}-12 stopped\n{base + 5} pu-{project}x-1 running\n"
                    f"{base + 6} pu-other-2 running",
                    f"{base + 999} pu-tmpl-other-build running\n"
                    f"{base + 998} {tn}-build stopped\n{base + 912} {tn} stopped"):
                stage = _old(SNIPPET_STAGE, base, project, listing).split()
                lines = listing.splitlines()
                jinja = _jinja_build(lines, tmpl[1], stage[1], project)
                f = pve.facts(base, project=project, listing=listing)
                what = f"facts({base}, {project}, {len(lines)} lines)"
                c.expect(f"{what} image", (str(f["image"]["vmid"]), f["image"]["name"],
                                          f["gateway"], str(f["prefix"]), f["image"]["address"]),
                        tuple(tmpl))
                c.expect(f"{what} stage", (str(f["stage"]["vmid"]), f["stage"]["name"],
                                          f["stage"]["address"]), tuple(stage))
                c.expect(f"{what} present", (f["image"]["present"], f["stage"]["present"],
                                            f["bodies"]),
                        (jinja["have_image"], jinja["have_stage"], jinja["project_bodies"]))

    # Командлет печатает ровно pve.facts() JSON'ом -- через диспетчер.
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    listing = "9912 pu-tmpl-mop stopped\n9003 pu-mop-1 running"
    r = subprocess.run([os.path.join(root, "bin", "mop"), "driver", "pve-facts",
                        "--base", "9000", "--project", "mop", "--listing", listing],
                       capture_output=True, text=True)
    try:
        got = json.loads(r.stdout)
    except ValueError:
        got = (r.returncode, r.stdout, r.stderr)
    c.expect("mop server pve-facts", got,
            json.loads(json.dumps(pve.facts(9000, project="mop", listing=listing))))


if __name__ == "__main__":
    sys.exit(main())
