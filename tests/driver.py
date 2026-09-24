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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import config, driver  # noqa: E402


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


def check_contract_151():
    """#151: адрес, сборочные тела и тип узла -- в контракте, ветки по флагу
    -- в драйверах. -> (случаев, провалов)."""
    import asyncio
    import re
    cases = bad = 0

    def fail(text):
        nonlocal bad
        bad += 1
        print(f"FAILED  {text}")

    host, pve = driver.module("host"), driver.module("pve")

    # Всё, что потребители зовут у драйвера, объявлено контрактом, и каждый
    # драйвер это имеет: третий драйвер иначе проходит проверку и падает у
    # потребителя.
    used = consumed_names()
    cases += 1
    if not used or used - set(driver.CONSUMED):
        fail(f"consumers call {sorted(used - set(driver.CONSUMED))} outside "
             f"driver.CONSUMED {driver.CONSUMED}")
    for name, mod in (("host", host), ("pve", pve)):
        for attr in driver.CONSUMED:
            cases += 1
            if not hasattr(mod, attr):
                fail(f"{name} has no {attr}, which consumers call")

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
                    cases += 1
                    fail(f"{os.path.relpath(os.path.join(base, f), ROOT)}:{n} "
                         f"branches on the driver type: {line.strip()}")

    # Характеризация: ответы прежних веток. Адрес сервера у host с #200 -- из
    # url кредов шины узла, а не из настройки; сервер тот же.
    keep = os.environ.get("MOP_SERVER_LAN"), host.NODE_FILE
    os.environ["MOP_SERVER_LAN"] = "127.0.0.1"
    tmp = tempfile.TemporaryDirectory()
    host.NODE_FILE = os.path.join(tmp.name, "bus.json")
    with open(host.NODE_FILE, "w") as f:
        f.write('{"url": "nats://127.0.0.1:4222"}')
    try:
        cases += 1
        if host.address("pu-mop-1") != old_toward_server():
            fail(f"host.address -> {host.address('pu-mop-1')!r}, "
                 f"wanted {old_toward_server()!r} (the node, seen from the server)")
    finally:
        host.NODE_FILE = keep[1]
        tmp.cleanup()
        if keep[0] is None:
            os.environ.pop("MOP_SERVER_LAN", None)
        else:
            os.environ["MOP_SERVER_LAN"] = keep[0]
    for n in ("pu-mop-1", "pu-rugent-7"):
        cases += 1
        if pve.address(n) != pve.address_of(n):
            fail(f"pve.address({n}) -> {pve.address(n)}, wanted {pve.address_of(n)}")
    cases += 1
    if asyncio.run(host.templates()) != []:
        fail("host.templates() must be [] — a node-body driver builds no images")
    cases += 1
    if not asyncio.iscoroutinefunction(pve.templates):
        fail("pve.templates must stay a coroutine")
    for name, want in (("host", False), ("pve", True)):
        cases += 1
        if driver.is_container(name) is not want:
            fail(f"driver.is_container({name!r}) must be {want}")
    # До #175 неизвестное и пустое имя были «контейнером» (builder и image
    # решали `!= DEFAULT`), а of_node оставлял пустое пустым. Теперь пустое
    # -- DEFAULT, неизвестное -- отказ (check_driver_rule_175).
    for name in ("", "nosuch"):
        cases += 1
        try:
            driver.is_container(name)
            fail(f"driver.is_container({name!r}) must refuse")
        except RuntimeError:
            pass
    for meta, want in (({}, driver.DEFAULT), ({"mop_driver": "pve"}, "pve"),
                       ({"mop_driver": ""}, driver.DEFAULT), (None, driver.DEFAULT)):
        cases += 1
        if driver.of_node(meta) != want:
            fail(f"driver.of_node({meta!r}) -> {driver.of_node(meta)!r}, wanted {want!r}")

    # Впуск ключа сервера (#62) -- полиморфный admit(name, open): host открывать
    # нечего, pve без ключа сервера на узле отказывает прежним текстом.
    for open_ in (True, False):
        cases += 1
        if asyncio.run(host.admit("pu-mop-1", open_)) != {}:
            fail(f"host.admit(..., {open_}) must be a no-op")
    cases += 1
    if driver.SERVER_PUB != f"{driver.HOME}/.config/mop/bootstrap.pub":
        fail(f"driver.SERVER_PUB moved: {driver.SERVER_PUB}")
    missing = os.path.join(tempfile.mkdtemp(prefix="mop-test-driver-"), "bootstrap.pub")
    keep_pub, pve.SERVER_PUB = pve.SERVER_PUB, missing
    try:
        cases += 1
        try:
            asyncio.run(pve.admit("pu-mop-1", True))
            fail("pve.admit without the server key must refuse")
        except RuntimeError as e:
            want = f"no server key on this node ({missing}) — run mop deploy"
            if str(e) != want:
                fail(f"pve refusal without the server key: {str(e)!r}, wanted {want!r}")
    finally:
        pve.SERVER_PUB = keep_pub
    return cases, bad


def main():
    bad = 0
    cases = 0

    for what, mod, ok in CONTRACT:
        cases += 1
        try:
            got = driver.contract("fake", mod)
            refused = False
        except RuntimeError:
            got, refused = None, True
        if refused == ok:
            bad += 1
            print(f"FAILED  contract: {what} — "
                  f"{'refused' if refused else 'accepted'}, wanted the opposite")
        elif ok and got.get("doc") and not isinstance(got["doc"], str):
            bad += 1
            print(f"FAILED  contract: {what} — doc is not a string")

    cases += 1
    if driver.contract("fake", plugin(__doc__="one\ntwo"))["doc"] != "one":
        bad += 1
        print("FAILED  contract: doc must be the first line of the docstring")

    for name, ok in NAMES:
        cases += 1
        if driver.valid_name(name) != ok:
            bad += 1
            print(f"FAILED  valid_name({name!r}) must be {ok}")

    cases += 1
    if not isinstance(driver.get("host"), dict):
        bad += 1
        print("FAILED  registry didn't find the host driver")
    cases += 1
    if driver.get("no-such") is not None:
        bad += 1
        print("FAILED  get() of an unknown name must return None")
    cases += 1
    try:
        driver.require("no-such")
        bad += 1
        print("FAILED  require() of an unknown name must refuse")
    except RuntimeError:
        pass

    # Драйвер узла, а не папета: значение приезжает в окружение агента юнитом,
    # и подставить его запросом с шины нельзя. Дефолт — host: узел, ничего про
    # драйвер не знающий, обязан вести себя как раньше.
    host = driver.module("host")
    for what, verb, want in HOST_ARGV:
        cases += 1
        got = getattr(host, verb)("pu-mop-1")
        if got != want:
            bad += 1
            print(f"FAILED  host.{verb}: {what}\n  wanted: {want!r}\n  got: {got!r}")

    # Драйвер узла, а не папета, и спрашивают его двое с разным окружением:
    # агент из юнита systemd и внешний врапер из процесса задачи Nomad. Пока
    # значение жило строкой Environment= в юните, врапер его не видел и молча
    # поднимал папета драйвером host — на гипервизоре это значит «прямо на
    # гипервизоре, мимо тела». Поэтому источник правды — файл узла.
    saved_env = os.environ.pop("MOP_DRIVER", None)
    saved_file = config.NODE_ENV_FILE
    tmp = os.path.join(tempfile.mkdtemp(), "node.env")
    try:
        config.NODE_ENV_FILE = tmp
        config.forget()
        cases += 1
        if driver.current_name() != "host":
            bad += 1
            print("FAILED  a node that says nothing about a driver must be host")
        cases += 1
        with open(tmp, "w") as f:
            f.write("MOP_DRIVER=pve\n")
        config.forget()
        if driver.current_name() != "pve":
            bad += 1
            print("FAILED  current_name() must read the node's file — an empty "
                  "environment is the wrapper's normal case")
        cases += 1
        with open(tmp, "w") as f:
            f.write("MOP_DRIVER=\n")
        config.forget()
        if driver.current_name() != "host":
            bad += 1
            print("FAILED  an empty file must mean host, not an empty driver name")
        cases += 1
        with open(tmp, "w") as f:
            f.write("MOP_DRIVER=pve\n")
        config.forget()
        os.environ["MOP_DRIVER"] = "host"
        if driver.current() is not host:
            bad += 1
            print("FAILED  MOP_DRIVER must outrank the node's file")
    finally:
        config.NODE_ENV_FILE = saved_file
        config.forget()
        os.environ.pop("MOP_DRIVER", None)
        if saved_env is not None:
            os.environ["MOP_DRIVER"] = saved_env

    # ── драйвер pve: всё выводится из имени ──────────────────────────────
    pve = driver.module("pve")

    # Раздача файла (`mop login`) идёт в узел И в каждое тело. У host второе
    # было бы той же записью в тот же файл по разу на папета — и, что хуже,
    # отчёт обещал бы запись в тела, которых нет.
    cases += 1
    if host.IS_CONTAINER is not False:
        bad += 1
        print("FAILED  host.IS_CONTAINER must be False — the body IS the node")

    cases += 1
    if not isinstance(driver.get("pve"), dict):
        bad += 1
        print("FAILED  registry didn't find the pve driver")

    # VMID в своём диапазоне и не пересекается с диапазоном шаблонов: шаблон
    # живёт рядом с телами и сносится теми же глаголами, так что налезь один
    # на другой — снос папета унёс бы образ проекта.
    seen = {}
    for name in PVE_NAMES:
        cases += 1
        vmid = pve.vmid_of(name)
        if not pve.BODY_MIN <= vmid <= pve.BODY_MAX:
            bad += 1
            print(f"FAILED  pve.vmid_of({name!r}) = {vmid}, outside "
                  f"{pve.BODY_MIN}..{pve.BODY_MAX}")
        if vmid in seen:
            bad += 1
            print(f"FAILED  pve.vmid_of: {name!r} and {seen[vmid]!r} collide on {vmid}")
        seen[vmid] = name

    cases += 1
    if pve.vmid_of("pu-mop-1") != pve.vmid_of("pu-mop-1"):
        bad += 1
        print("FAILED  pve.vmid_of must be a function of the name, nothing else")

    for project in ("mop", "rugent", "cloudpub"):
        cases += 1
        t = pve.template_vmid(project)
        if not pve.TMPL_MIN <= t <= pve.TMPL_MAX:
            bad += 1
            print(f"FAILED  pve.template_vmid({project!r}) = {t}, outside "
                  f"{pve.TMPL_MIN}..{pve.TMPL_MAX}")
        if pve.BODY_MIN <= t <= pve.BODY_MAX:
            bad += 1
            print(f"FAILED  template {t} lands in the body range — a wipe would "
                  f"take the project's image with it")
        cases += 1
        # Имя шаблона обязано быть под охраной префикса (root-обёртка на
        # гипервизоре пускает только pu-*), но не быть именем папета: иначе
        # ростер тел показал бы образ живым папетом.
        tn = pve.template_name(project)
        if not tn.startswith("pu-") or driver.valid_name(tn):
            bad += 1
            print(f"FAILED  template name {tn!r} must start with pu- and not "
                  f"look like a puppet name")

    # Адрес выводится из VMID, а не хранится: хранимый однажды разойдётся с
    # тем, что реально стоит на контейнере.
    addrs = {}
    for name in PVE_NAMES:
        cases += 1
        ip = pve.address_of(name)
        if ip == pve.GATEWAY:
            bad += 1
            print(f"FAILED  pve.address_of({name!r}) is the gateway {ip}")
        if not ip.startswith(pve.SUBNET.rsplit(".", 2)[0] + "."):
            bad += 1
            print(f"FAILED  pve.address_of({name!r}) = {ip}, outside {pve.SUBNET}")
        if ip in addrs:
            bad += 1
            print(f"FAILED  pve.address_of: {name!r} and {addrs[ip]!r} share {ip}")
        addrs[ip] = name

    cases += 1
    if pve.IS_CONTAINER is not True:
        bad += 1
        print("FAILED  pve.IS_CONTAINER must be True — a body is a container")

    # ssh, а не proxmox_pct_remote: ControlPersist держит соединение, и проба
    # состояния перестаёт платить рукопожатием.
    cases += 1
    a = pve.argv("pu-mop-1")
    ip = pve.address_of("pu-mop-1")
    if a[:1] != ["ssh"] or not any(x.endswith("@" + ip) for x in a):
        bad += 1
        print(f"FAILED  pve.argv must be ssh into {ip}: {a!r}")
    cases += 1
    if "ControlPersist=" not in " ".join(a):
        bad += 1
        print("FAILED  pve.argv without ControlPersist pays a handshake per probe")
    cases += 1
    if "BatchMode=yes" not in " ".join(a):
        bad += 1
        print("FAILED  pve.argv without BatchMode can stop on a password prompt")

    # Соединение врапера живёт столько же, сколько папет, и мультиплексировать
    # его нельзя. Поймано на живом папете: мастер-соединение, уходящее по
    # ControlPersist, уносит с собой сессию врапера — ssh отдаёт 255, Nomad
    # читает это как падение задачи и перезапускает папета на ровном месте.
    cases += 1
    r = pve.run_argv("pu-mop-1")
    joined = " ".join(r)
    if r[:1] != ["ssh"] or not any(x.endswith("@" + ip) for x in r):
        bad += 1
        print(f"FAILED  pve.run_argv must be ssh into {ip}: {r!r}")
    cases += 1
    if "ControlMaster=no" not in joined or "ControlPath=none" not in joined:
        bad += 1
        print("FAILED  pve.run_argv must not share a multiplexed connection — "
              "the master's ControlPersist would take the puppet down with it")
    # ПОРЯДОК, а не присутствие: у ssh побеждает первая встреченная опция, и
    # `ControlMaster=no` после `auto` из общего списка не действует. Так
    # врапер молча становился клиентом мастер-сокета агента и умирал с ним
    # при каждом рестарте юнита — задача выходила кодом 255 (22.09, дважды).
    # HYPOTHESIS: переопределение стоит после _SSH_OPTS. SOLUTION: перед.
    # STATUS: FIXED — see #72
    cases += 1
    masters = [x for x in r if x.startswith("ControlMaster=")]
    paths = [x for x in r if x.startswith("ControlPath=")]
    if masters[:1] != ["ControlMaster=no"] or paths[:1] != ["ControlPath=none"]:
        bad += 1
        print(f"FAILED  pve.run_argv: the FIRST ControlMaster/ControlPath must be "
              f"no/none — ssh takes the first value it sees: {masters} {paths}")
    cases += 1
    if "ServerAliveInterval" not in joined:
        bad += 1
        print("FAILED  pve.run_argv holds a connection for the puppet's whole "
              "life; without a keepalive a silent NAT drop reads as a dead puppet")

    # Аварийный путь — не ssh: он нужен ровно тогда, когда у тела сломана сеть,
    # sshd или права на authorized_keys.
    cases += 1
    r = pve.repair_argv("pu-mop-1")
    if not r or "ssh" in r[0]:
        bad += 1
        print(f"FAILED  pve.repair_argv must not go over ssh: {r!r}")
    cases += 1
    if str(pve.vmid_of("pu-mop-1")) not in r:
        bad += 1
        print(f"FAILED  pve.repair_argv must name the body's vmid: {r!r}")

    # Человек входит в тело, а не на гипервизор: на гипервизоре tmux-сервера
    # папета нет вовсе.
    cases += 1
    at = pve.attach_argv("pu-mop-1")
    if at[:1] != ["ssh"] or "tmux" not in at:
        bad += 1
        print(f"FAILED  pve.attach_argv must ssh into the body and run tmux: {at!r}")

    # Имя, не прошедшее valid_name, в шелл гипервизора не попадает вовсе.
    for bogus in ("pu-mop-1;id", "../etc", ""):
        cases += 1
        try:
            pve.vmid_of(bogus)
            bad += 1
            print(f"FAILED  pve.vmid_of({bogus!r}) must refuse")
        except ValueError:
            pass

    # Транскрипты лежат внутри тела: считать их путём на гипервизоре значит
    # молча получить нулевой расход токенов у контейнерных папетов.
    cases += 1
    if pve.projects_dir("pu-mop-1") == host.projects_dir("pu-mop-1") \
            and pve.HOME != host.HOME:
        bad += 1
        print("FAILED  pve.projects_dir must point inside the body")

    # Адрес сборочного тела обязан быть СВОИМ у каждого проекта и не задевать
    # живые тела. Раньше он считался как «шлюз плюс один» одинаково для всех,
    # и это ловилось не отказом, а зависанием: две сборки на одном
    # гипервизоре садились на один адрес, ssh уходил в чужой контейнер, обе
    # стороны оставались живыми и молчали (22.09, rugent против rudesktop).
    projects = ("rugent", "cloudpub", "mop", "rudesktop", "a", "zzz")
    cases += 1
    if len({pve.template_address(s) for s in projects}) != len(projects):
        bad += 1
        print("FAILED  two projects share one build address")

    # Диапазоны не пересекаются по построению: VMID шаблонов идут выше VMID
    # тел, и адрес считается из VMID одной формулой. Проверяем края — именно
    # там прежний «шлюз плюс один» и совпадал с первым живым телом.
    cases += 1
    body_addrs = {pve.address_of_vmid(v) for v in (pve.BODY_MIN, pve.BODY_MAX)}
    tmpl_addrs = {pve.address_of_vmid(v) for v in (pve.TMPL_MIN, pve.TMPL_MAX)}
    if body_addrs & tmpl_addrs:
        bad += 1
        print("FAILED  build addresses overlap live bodies")

    # И адрес обязан быть настоящим адресом этой сети, не шлюзом: выехавший
    # за подсеть адрес не отказывает, он просто не отвечает.
    import ipaddress as _ip
    net = _ip.ip_network(pve.SUBNET)
    for sh in projects:
        cases += 1
        addr = _ip.ip_address(pve.template_address(sh))
        if addr not in net or str(addr) == pve.GATEWAY:
            bad += 1
            print(f"FAILED  build address of {sh} is {addr}, outside {net} "
                  f"or equal to the gateway")

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
        cases += 1
        want = str(net.network_address + v)
        if pve.address_of_vmid(v) != want:
            bad += 1
            print(f"FAILED  pve.address_of_vmid({v}) = {pve.address_of_vmid(v)}, "
                  f"wanted {want}: the address must not depend on this node's base")

    # VMID, не влезающий в сеть тел, — громкий отказ, а не адрес соседней
    # сети: уехавший за подсеть адрес не отказывает, он просто не отвечает.
    cases += 1
    try:
        pve.address_of_vmid(net.num_addresses + 5)
        bad += 1
        print("FAILED  pve.address_of_vmid past the subnet must refuse")
    except ValueError:
        pass

    # Маршрут к телам этого узла (#59): сервер достаёт до тел только через
    # гипервизор, и кто-то обязан раздать ему маршрут. Кусок сети одного
    # гипервизора — это адреса его диапазона VMID (тела и шаблоны), и
    # покрывать его надо ТОЧНО: шире — и маршруты двух гипервизоров
    # налезут друг на друга, уже — и часть тел останется недостижимой.
    # HYPOTHESIS: маршрута нет вовсе, роль pve кончается мостом и NAT.
    # SOLUTION: pve.routes_of(subnet, base) — CIDR'ы, покрывающие ровно
    # [сеть+base, сеть+base+999]; проверяется без пула. STATUS: FIXED — see #59
    cases += 1
    try:
        got = pve.routes_of(pve.SUBNET, 9000)
    except AttributeError:
        got = None
        bad += 1
        print("FAILED  pve.routes_of is missing: nobody hands the server a route")
    if got is not None:
        nets = [_ip.ip_network(c) for c in got]
        covered = set()
        for n in nets:
            covered |= set(n.hosts()) | {n.network_address, n.broadcast_address}
        want = {net.network_address + v for v in range(9000, 10000)}
        cases += 1
        if covered != want:
            bad += 1
            print(f"FAILED  routes_of covers {len(covered)} addresses, wanted "
                  f"exactly the 1000 of vmids 9000..9999: {got}")
        # Второй гипервизор со своей базой не пересекается с первым ни одним
        # адресом — иначе маршрут неоднозначен, и ломается это молча.
        cases += 1
        other = [_ip.ip_network(c) for c in pve.routes_of(pve.SUBNET, 20000)]
        if any(a.overlaps(b) for a in nets for b in other):
            bad += 1
            print("FAILED  routes of two bases overlap")
        # И это маршруты именно ЭТОЙ сети, а не соседней.
        cases += 1
        if any(not n.subnet_of(net) for n in nets + other):
            bad += 1
            print(f"FAILED  a route leaves the bodies' network {net}")

    # Сборочное тело (#60). Пересборка больше не начинается со сноса образа:
    # шаблон полностью клонируется в сборочное тело, плейбук играется там
    # (идемпотентно — качается только новое), и лишь потом образ заменяется.
    # Сборочное тело зовётся по проекту, чтобы оборванную сборку можно было
    # ПРОДОЛЖИТЬ одной командой: следующий прогон находит его по имени.
    # HYPOTHESIS: stage_name/stage_vmid/parse_list нет вовсе — образ сносится
    # первой задачей. SOLUTION: чистые функции ниже. STATUS: FIXED — see #60
    cases += 1
    try:
        sn = pve.stage_name("mop")
        if not sn.startswith("pu-tmpl-") or driver.valid_name(sn) \
                or sn == pve.template_name("mop"):
            bad += 1
            print(f"FAILED  stage name {sn!r} must be a template-like name of its own")
    except AttributeError:
        bad += 1
        print("FAILED  pve.stage_name is missing")

    listing = "9828 pu-rugent-1 running\n9988 pu-tmpl-rugent stopped\n"
    cases += 1
    try:
        parsed = pve.parse_list(listing)
        if parsed != [(9828, "pu-rugent-1", "running"),
                      (9988, "pu-tmpl-rugent", "stopped")]:
            bad += 1
            print(f"FAILED  parse_list: {parsed!r}")
    except AttributeError:
        parsed = None
        bad += 1
        print("FAILED  pve.parse_list is missing")

    if parsed is not None:
        # Оборванная сборка: её тело стоит под именем проекта — продолжаем в нём.
        cases += 1
        left = parsed + [(9950, pve.stage_name("mop"), "stopped")]
        if pve.stage_vmid("mop", left) != 9950:
            bad += 1
            print("FAILED  stage_vmid must resume the build body left by a previous run")
        # Иначе — свободный номер из диапазона шаблонов, не занятый и не
        # совпадающий с номером образа этого проекта.
        cases += 1
        v = pve.stage_vmid("mop", parsed)
        taken = {vm for vm, _, _ in parsed}
        if not pve.TMPL_MIN <= v <= pve.TMPL_MAX or v in taken \
                or v == pve.template_vmid("mop"):
            bad += 1
            print(f"FAILED  stage_vmid({v}) must be a free template slot of its own")
        # Диапазон занят целиком — громко, а не номер чужого контейнера.
        cases += 1
        full = [(vm, f"pu-tmpl-x{vm}", "stopped")
                for vm in range(pve.TMPL_MIN, pve.TMPL_MAX + 1)]
        try:
            pve.stage_vmid("mop", full)
            bad += 1
            print("FAILED  stage_vmid with no free slot must refuse")
        except RuntimeError:
            pass

    # Контракт драйвера — СЛОВАРЬ, и флаг в нём ключом, а не атрибутом.
    # Модуль с атрибутом IS_CONTAINER отдаёт только `current()`, и он про свой
    # узел; спросить про чужой можно лишь по имени, через реестр. Перепутать
    # эти две вещи легко, а отказ приходит не там: `mop delete` успевает снять
    # джоб и падает уже ПОСЛЕ этого на AttributeError, оставляя тело сиротой
    # (поймано 22.09 живым прогоном, на двух телах сразу).
    for name in ("host", "pve"):
        cases += 1
        c = driver.require(name)
        if not isinstance(c, dict) or "is_container" not in c:
            bad += 1
            print(f"FAILED  driver.require({name!r}) must be a mapping with "
                  f"is_container, got {type(c).__name__}")

    cases += 1
    if driver.require("host")["is_container"] is not False \
            or driver.require("pve")["is_container"] is not True:
        bad += 1
        print("FAILED  is_container must tell a node-body driver from a "
              "container one — everything that decides what to destroy "
              "hangs on it")

    # HYPOTHESIS (#114): pve-тело получает кред проекта копией файла с
    # гипервизора, а файл туда клал прогон; прогон его больше не кладёт.
    # SOLUTION: кред едет в тело только из ответа bootstrap (driver run), в
    # переливке с узла его нет. STATUS: FIXED — see #114
    from mop.driver import pve
    cases += 1
    if any("bus-" in path for path, _ in pve._seed_files()):
        bad += 1
        print("FAILED  pve seed must not copy the node's bus-<project>.json: "
              "the credentials come from the bootstrap answer only")

    # HYPOTHESIS (#137): файл в pve-тело стоил ~4 с (четыре pct на файл), и
    # агент писал в тела по очереди. SOLUTION: все файлы тела -- одним tar
    # через `mop-pve unpack` (#136). STATUS: FIXED — see #137
    import io
    import tarfile
    from mop.driver import pve
    cases += 1
    try:
        blob = pve.tar_of([(f"{pve.HOME}/.claude/.credentials.json", b"{}"),
                           (f"{pve.HOME}/.config/mop/secrets.env", b"K=v\n")], pve.HOME)
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            got = {m.name: (m.mode, t.extractfile(m).read()) for m in t.getmembers()}
        want = {".claude/.credentials.json": (0o600, b"{}"),
                ".config/mop/secrets.env": (0o600, b"K=v\n")}
        if got != want:
            bad += 1
            print(f"FAILED  tar_of must hold home-relative 0600 files: {got}")
        # Время файла -- сейчас: tar без mtime распаковывается 1970-м годом.
        import time
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            if any(abs(m.mtime - time.time()) > 60 for m in t.getmembers()):
                bad += 1
                print("FAILED  tar_of must stamp the files with the current time")
    except AttributeError:
        bad += 1
        print("FAILED  pve.tar_of is missing")
    for outside in ("/etc/passwd", f"{pve.HOME}/../x", "/tmp/x"):
        cases += 1
        try:
            pve.tar_of([(outside, b"x")], pve.HOME)
            bad += 1
            print(f"FAILED  tar_of must refuse a path outside the home: {outside}")
        except (ValueError, AttributeError):
            pass
    cases += 1
    if "push_many" not in driver.VERBS:
        bad += 1
        print("FAILED  push_many must be a driver verb: the agent writes a body in one call")

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
        cases += 1
        got = driver.until_ok(flaky([(False, "timed out"), (False, "timed out"),
                                     (True, None)]), 5, 2, slept.append)
        if got != (True, None, 3) or slept != [2, 2]:
            bad += 1
            print(f"FAILED  until_ok must retry until the probe passes: {got}, slept {slept}")
        cases += 1
        slept.clear()
        got = driver.until_ok(flaky([(True, None)]), 5, 2, slept.append)
        if got != (True, None, 1) or slept:
            bad += 1
            print(f"FAILED  a healthy body must cost one probe and no pause: {got}, slept {slept}")
        cases += 1
        slept.clear()
        got = driver.until_ok(flaky([(False, f"try {i}") for i in range(3)]), 3, 2,
                              slept.append)
        if got != (False, "try 2", 3):
            bad += 1
            print(f"FAILED  after the last try the refusal carries the last reason: {got}")
        cases += 1
        if slept != [2, 2]:
            bad += 1
            print(f"FAILED  no pause after the last try: slept {slept}")
    except AttributeError:
        bad += 1
        print("FAILED  driver.until_ok is missing")

    c, b = check_pve_facts()
    cases += c
    bad += b

    try:
        c, b = check_contract_151()
    except Exception as e:
        c, b = 1, 1
        print(f"FAILED  check_contract_151: {type(e).__name__}: {e}")
    cases, bad = cases + c, bad + b

    try:
        c, b = check_driver_rule_175()
    except Exception as e:
        c, b = 1, 1
        print(f"FAILED  check_driver_rule_175: {type(e).__name__}: {e}")
    cases, bad = cases + c, bad + b

    try:
        c, b = check_timeouts_171()
    except Exception as e:
        c, b = 1, 1
        print(f"FAILED  check_timeouts_171: {type(e).__name__}: {e}")
    cases, bad = cases + c, bad + b

    try:
        c, b = check_clone_lock_195()
    except Exception as e:
        c, b = 1, 1
        print(f"FAILED  check_clone_lock_195: {type(e).__name__}: {e}")
    cases, bad = cases + c, bad + b

    try:
        c, b = check_body_memory_197()
    except Exception as e:
        c, b = 1, 1
        print(f"FAILED  check_body_memory_197: {type(e).__name__}: {e}")
    cases, bad = cases + c, bad + b

    try:
        c, b = check_host_address_200()
    except Exception as e:
        c, b = 1, 1
        print(f"FAILED  check_host_address_200: {type(e).__name__}: {e}")
    cases, bad = cases + c, bad + b

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


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
def check_host_address_200():
    """-> (случаев, провалов)."""
    import socket as real
    from cli import no_network
    from mop.driver import host
    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if got != want:
            bad += 1
            print(f"FAILED  {what}\n  wanted: {want!r}\n  got: {got!r}")

    targets = []

    class Sock:
        """UDP-сокет без сети: помнит, куда выбирали маршрут."""
        def __init__(self, *a):
            pass

        def connect(self, addr):
            if addr[0] == "nowhere.invalid":
                raise real.gaierror(-2, "Name or service not known")
            targets.append(addr)

        def getsockname(self):
            return ("192.0.2.77", 40000)

        def close(self):
            pass

    stub = types.SimpleNamespace(socket=Sock, AF_INET=real.AF_INET,
                                 SOCK_DGRAM=real.SOCK_DGRAM, gaierror=real.gaierror,
                                 error=real.error)
    undo = no_network()
    saved = (host.socket, getattr(host, "NODE_FILE", None))
    # Настройка на узле -- не тот сервер: ответ обязан прийти из файла шины.
    os.environ["MOP_SERVER_LAN"] = "198.51.100.9"
    try:
        host.socket = stub
        with tempfile.TemporaryDirectory() as tmp:
            def bus_file(body):
                path = os.path.join(tmp, "bus.json")
                with open(path, "w") as f:
                    f.write(body)
                host.NODE_FILE = path
                return path

            def refusal(what, *words):
                nonlocal cases, bad
                cases += 1
                try:
                    got = host.address("pu-mop-1")
                except RuntimeError as e:
                    missing = [w for w in words if w not in str(e)]
                    if missing:
                        bad += 1
                        print(f"FAILED  {what}: refusal {str(e)!r} doesn't name {missing}")
                    return
                bad += 1
                print(f"FAILED  {what}: answered {got!r}, wanted a refusal")

            bus_file('{"url": "nats://192.0.2.1:4222", "user": "node", "password": "x"}')
            del targets[:]
            check("address from the node's bus url", host.address("pu-mop-1"), "192.0.2.77")
            check("route chosen toward the bus server", targets, [("192.0.2.1", 1)])

            bus_file('{"url": "nats://server.example:4222", "user": "node", "password": "x"}')
            del targets[:]
            host.address("pu-mop-1")
            check("a server name routes by name", targets, [("server.example", 1)])

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
    finally:
        host.socket = saved[0]
        if saved[1] is None:
            host.__dict__.pop("NODE_FILE", None)
        else:
            host.NODE_FILE = saved[1]
        undo()
    return cases, bad


# ── память тела -- из спеки, на каждом подъёме (#197) ───────────────────
# HYPOTHESIS: память pve-тела -- `pct --memory` образа, испечённая сборкой из
# `.mop` на тот момент; спека папета (MemoryMaxMB) до тела не доходит, и
# стоящее тело не меняется никогда.
# SOLUTION: спека несёт потолок в PU_MEM_MB, `mop driver run` отдаёт его
# ensure, и ensure до start ставит телу память глаголом `mop-pve memory` --
# и новому клону, и стоящему телу. Спека до #197 PU_MEM_MB не несёт: тогда
# память тела не трогается, как было.
# STATUS: FIXED — see #197
def check_body_memory_197():
    import asyncio
    from mop.driver import pve
    from mop.cli.driver import run

    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if not want(got):
            bad += 1
            print(f"FAILED  #197 {what}: got {got!r}")

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

    saved = (pve.sh, pve.SSH_KEY, pve._seed_files, pve._hostname)
    try:
        with tempfile.TemporaryDirectory() as d:
            key = os.path.join(d, "mop-body")
            with open(key + ".pub", "w") as f:
                f.write("ssh-ed25519 AAAA node\n")
            pve.SSH_KEY = key
            pve._seed_files = lambda: []

            for what, stand in (("a standing body", standing), ("a fresh clone", empty)):
                pve._hostname = stand
                pve.sh, calls = fake()
                r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": "16384"}))
                check(f"{what}: ensure succeeds", r, lambda r: not r.get("error"))
                mem = [w for v, w in calls if v == "memory"]
                check(f"{what}: memory is set once, to the spec's ceiling", mem,
                      lambda m: len(m) == 1 and m[0][-2:] == [str(vmid), "16384"])
                check(f"{what}: memory is set before start", verbs(calls),
                      lambda v: "memory" in v and "start" in v
                      and v.index("memory") < v.index("start"))

            pve._hostname = standing
            pve.sh, calls = fake()
            r = asyncio.run(pve.ensure(name, {"project": "mop"}))
            check("a spec before #197: the body's memory is left alone", verbs(calls),
                  lambda v: "memory" not in v and "start" in v)

            pve.sh, calls = fake("memory")
            r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": "16384"}))
            check("memory refused: ensure refuses, naming the body and the size", r,
                  lambda r: str(vmid) in (r.get("error") or "")
                  and "16384" in r["error"] and "no such thing" in r["error"])
            check("memory refused: the body is not started", verbs(calls),
                  lambda v: "start" not in v)
            pve.sh, calls = fake("memory", None)
            r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": "16384"}))
            check("memory timed out: a refusal, not a success", r,
                  lambda r: "timed out" in (r.get("error") or ""))

            for bad_mem in ("lots", "0", "-1", "8G"):
                pve.sh, calls = fake()
                r = asyncio.run(pve.ensure(name, {"project": "mop", "mem": bad_mem}))
                check(f"mem {bad_mem!r}: refused before the hypervisor", (r, verbs(calls)),
                      lambda rv: "PU_MEM_MB" in (rv[0].get("error") or "")
                      and "memory" not in rv[1] and "start" not in rv[1])
    finally:
        pve.sh, pve.SSH_KEY, pve._seed_files, pve._hostname = saved

    # `mop driver run` отдаёт ensure потолок из окружения задачи.
    check("run: params carry the spec's ceiling",
          run.ensure_params(name, {"PU_MEM_MB": "16384"}),
          lambda p: p == {"project": "mop", "mem": "16384"})
    check("run: a spec before #197 carries no mem",
          run.ensure_params(name, {}), lambda p: p == {"project": "mop"})
    check("run: an empty PU_MEM_MB is no mem",
          run.ensure_params(name, {"PU_MEM_MB": ""}), lambda p: p == {"project": "mop"})
    return cases, bad


# ── имя драйвера узла -- одно правило (#175) ────────────────────────────
# HYPOTHESIS: of_node оставлял пустой mop_driver пустым (is_container("") ->
# True, узел читался контейнерным), а четыре других места делали из пустого
# host (`or DEFAULT`); неизвестное имя -- опечатка в инвентаре -- было
# «контейнером» для сборщика и объявления образов и падало в mop delete.
# SOLUTION: of_node -- единственное правило: нет ключа или пусто -> DEFAULT
# (узел, настроенный до поля), неизвестное -> громкий отказ с узлом и
# значением. Остальные места -- через него.
# STATUS: FIXED — see #175
def check_driver_rule_175():
    import re
    cases = bad = 0

    def check(what, ok):
        nonlocal cases, bad
        cases += 1
        if not ok:
            bad += 1
            print(f"FAILED  #175 {what}")

    for meta, want in ((None, driver.DEFAULT), ({}, driver.DEFAULT),
                       ({"mop_driver": ""}, driver.DEFAULT),
                       ({"mop_driver": "host"}, "host"), ({"mop_driver": "pve"}, "pve")):
        try:
            got = driver.of_node(meta, "n1")
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        check(f"of_node({meta!r}) -> {got!r}, wanted {want!r}", got == want)
    check("is_container through of_node: empty is host",
          driver.is_container(driver.of_node({"mop_driver": ""}, "n1")) is False)
    check("is_container through of_node: pve is a container",
          driver.is_container(driver.of_node({"mop_driver": "pve"}, "n1")) is True)
    try:
        driver.of_node({"mop_driver": "bogus"}, "hyper")
        check("of_node of an unknown driver must refuse", False)
    except RuntimeError as e:
        check(f"the refusal names the node and the value: {e}",
              "hyper" in str(e) and "bogus" in str(e) and "\n" not in str(e))
    # Эта машина -- то же правило.
    saved = os.environ.get("MOP_DRIVER")
    try:
        os.environ["MOP_DRIVER"] = "bogus"
        try:
            driver.current_name()
            check("current_name of an unknown MOP_DRIVER must refuse", False)
        except RuntimeError as e:
            check("current_name names the value", "bogus" in str(e))
    finally:
        if saved is None:
            os.environ.pop("MOP_DRIVER", None)
        else:
            os.environ["MOP_DRIVER"] = saved
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
    from mop import builder, image, nodes, nomad
    metas = {"bad": {"mop_driver": "bogus"}, "hyper": {"mop_driver": "pve"}}
    summary = lambda n: {"Name": n, "Status": "ready"}
    try:
        rows = [nodes.row(summary(n), metas[n], {}) for n in sorted(metas)]
        got = {r["name"]: r for r in rows}
        check("nodes.row: the good node is visible",
              got["hyper"]["driver"] == "pve" and not got["hyper"].get("error"))
        check(f"nodes.row: the bad node is a row with the refusal: {got['bad']}",
              "bad: unknown driver 'bogus'" in (got["bad"].get("error") or ""))
    except Exception as e:
        check(f"nodes.row with a bad node: {type(e).__name__}: {e}", False)
    saved = (nomad.nodes_meta, nomad.node_dynamic_meta, nomad.set_node_meta)
    try:
        nomad.nodes_meta = lambda: metas
        nomad.node_dynamic_meta = lambda n: {"mop_projects": "mop"}
        nomad.set_node_meta = lambda n, m: None
        try:
            refused = []
            serving = builder.serving_now(refused)
            check(f"serving_now: the good node counts: {serving}", serving == {"hyper": ["mop"]})
            check(f"serving_now: the bad node is reported: {refused}",
                  any("bad: unknown driver 'bogus'" in r for r in refused))
        except Exception as e:
            check(f"serving_now with a bad node: {type(e).__name__}: {e}", False)
        try:
            got = dict(image.announce("mop"))
            check(f"announce: the good node answers: {got}", got.get("hyper") == "already announced")
            check(f"announce: the bad node carries the refusal: {got}",
                  "bad: unknown driver 'bogus'" in (got.get("bad") or ""))
        except Exception as e:
            check(f"announce with a bad node: {type(e).__name__}: {e}", False)
    finally:
        nomad.nodes_meta, nomad.node_dynamic_meta, nomad.set_node_meta = saved
    check(f"`or DEFAULT` outside of_node: {found}",
          all(x.startswith("mop/driver/__init__.py") for x in found) and len(found) <= 1)
    return cases, bad


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
def check_timeouts_171():
    import asyncio
    from mop.driver import host, pve

    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if not want(got):
            bad += 1
            print(f"FAILED  #171 {what}: got {got!r}")

    def fake_sh(fail=None, everything=False):
        """sh, у которого «не успевает» команда с этим словом (или любая)."""
        async def sh(script, timeout=20, prefix=()):
            words = script.split()
            if everything or (fail and fail in words):
                return "", None
            return "", 0
        return sh

    timed_out = lambda r: isinstance(r, dict) and "timed out" in (r.get("error") or "")
    saved = (host.sh, pve.sh, pve.SSH_KEY, pve._seed_files, pve._hostname)
    name = "pu-mop-1"
    try:
        with tempfile.TemporaryDirectory() as d:
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
            check("host.destroy, git timed out", r, timed_out)
            host.sh = fake_sh("-rf")
            r = asyncio.run(host.destroy(name))
            check("host.destroy, rm timed out", r, timed_out)
            check("host.destroy, rm: the timeout is named", r,
                  lambda r: "600s" in (r.get("error") or ""))
            host.sh = fake_sh()
            r = asyncio.run(host.destroy(name))
            check("host.destroy, success as before", r,
                  lambda r: r.get("reset") is True)

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
                check(f"pve.{what} timed out", asyncio.run(fn()), timed_out)

            pve._hostname = standing
            pve.sh = fake_sh()
            check("pve.ensure, success as before", asyncio.run(pve.ensure(name)),
                  lambda r: r.get("body") == vmid and not r.get("error"))
            check("pve.destroy, success as before", asyncio.run(pve.destroy(name)),
                  lambda r: r.get("destroyed") == vmid)
            check("pve.admit, success as before", asyncio.run(pve.admit(name, False)),
                  lambda r: r == {"admitted": False})

            # Пробы только для чтения -- ответ прежний (характеризация до правки).
            pve._hostname = saved[4]
            pve.sh = fake_sh(everything=True)
            check("pve.bodies tolerates", asyncio.run(pve.bodies()), lambda r: r == [])
            check("pve.templates tolerates", asyncio.run(pve.templates()),
                  lambda r: r == [])
            # _hostname кормит мутирующее решение ensure (клонировать или
            # поднять стоящее): таймаут -- «не знаю» (None), а не «пусто».
            check("pve._hostname: timeout is unknown, not absent",
                  asyncio.run(pve._hostname(vmid)), lambda r: r is None)
            calls = []

            async def list_times_out(script, timeout=20, prefix=()):
                calls.append(script.split())
                return ("", None) if "list" in script.split() else ("", 0)
            pve.sh = list_times_out
            r = asyncio.run(pve.ensure(name))
            check("pve.ensure: list timed out -> refusal", r, timed_out)
            check("pve.ensure: list timed out -> no clone", calls,
                  lambda c: not any("clone" in w for w in c))

            async def list_fails(script, timeout=20, prefix=()):
                return ("", 1) if "list" in script.split() else ("", 0)
            pve.sh = list_fails
            check("pve._hostname: non-zero exit is absent, as before",
                  asyncio.run(pve._hostname(vmid)), lambda r: r == "")
            pve.sh = fake_sh(everything=True)
            check("pve.capacity is an error, as before", asyncio.run(pve.capacity()),
                  lambda r: bool(r.get("error")))
            host.sh = fake_sh(everything=True)
            check("host.capacity is an error, as before", asyncio.run(host.capacity()),
                  lambda r: bool(r.get("error")))

            # why(): ненулевой код -- как было, None -- «timed out».
            check("why: output wins", driver.why(" boom \n", 1), lambda r: r == "boom")
            check("why: exit code", driver.why("", 2), lambda r: r == "exit 2")
            check("why: timeout with N", driver.why("", None, 30),
                  lambda r: r == "timed out after 30s")
    finally:
        host.sh, pve.sh, pve.SSH_KEY, pve._seed_files, pve._hostname = saved
    return cases, bad


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
def check_clone_lock_195():
    import asyncio
    from mop.driver import pve

    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if not want(got):
            bad += 1
            print(f"FAILED  #195 {what}: got {got!r}")

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

    saved = (pve.sh, pve.SSH_KEY, pve._seed_files, getattr(pve, "_sleep", None))
    try:
        with tempfile.TemporaryDirectory() as d:
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
            check("locked twice then ok -> body", r,
                  lambda r: not r.get("error") and r.get("created") is True)
            check("locked twice then ok -> three clones", len(calls),
                  lambda n: n == 3)
            check("locked twice then ok -> two pauses", len(slept),
                  lambda n: n == 2)

            # Второй текст отказа по блокировке -- файл конфига под замком.
            slept.clear()
            pve.sh, calls = fake([
                ("can't lock file '/run/lock/lxc/pve-config-9001.lock' "
                 "- got timeout\n", 1), (f"{src}\n", 0)])
            r = asyncio.run(pve.ensure(name))
            check("can't lock file -> retried", (r, len(calls)),
                  lambda x: not x[0].get("error") and x[1] == 2)

            # Блокировка не уходит: отказ называет блокировку, а не сборку, и
            # ожидание ограничено.
            slept.clear()
            pve.sh, calls = fake([(locked, 1)])
            r = asyncio.run(pve.ensure(name))
            err = r.get("error") or ""
            check("lock persists -> error", r, lambda r: bool(r.get("error")))
            check("lock persists -> names the lock", err,
                  lambda e: "locked" in e)
            check("lock persists -> no build advice", err,
                  lambda e: "mop driver build" not in e)
            check("lock persists -> bounded wait", sum(slept),
                  lambda t: 60 <= t <= 180)

            # Шаблона нет: совет собрать образ, как сегодня.
            slept.clear()
            pve.sh, calls = fake([(f"mop-pve: no container {src}\n", 64)],
                                 template=False)
            r = asyncio.run(pve.ensure(name))
            err = r.get("error") or ""
            check("no template -> build advice", err,
                  lambda e: f"mop driver build {project}" in e)
            check("no template -> not retried", (len(calls), slept),
                  lambda x: x == (1, []))

            # Иной отказ при стоящем шаблоне: его причина, без совета.
            pve.sh, calls = fake([("storage 'local-lvm' is full\n", 255)])
            r = asyncio.run(pve.ensure(name))
            err = r.get("error") or ""
            check("other error -> its reason", err,
                  lambda e: "storage 'local-lvm' is full" in e)
            check("other error -> no build advice", err,
                  lambda e: "mop driver build" not in e)
            check("other error -> not retried", (len(calls), slept),
                  lambda x: x == (1, []))
    finally:
        pve.sh, pve.SSH_KEY, pve._seed_files = saved[:3]
        if saved[3] is None:
            if hasattr(pve, "_sleep"):
                del pve._sleep
        else:
            pve._sleep = saved[3]
    return cases, bad


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


def check_pve_facts():
    """HYPOTHESIS (#158): факты драйвера pve плейбуки добывали тремя копиями
    `python3 -c` с sys.path и окружением, а список тел разбирали дважды --
    pve.parse_list и jinja `search ' имя '`; длину префикса сети считал ещё и
    jinja роли, а путь обёртки, ключи и VMID_MAX жили параллельно в pve.py и
    YAML.
    SOLUTION: pve.facts() -- чистая функция, `mop driver pve-facts` печатает
    её JSON; плейбуки читают факты из него.
    STATUS: FIXED — see #158"""
    import json
    import subprocess
    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if got != want:
            bad += 1
            print(f"FAILED  {what}: got {got!r}, want {want!r}")

    pve = driver.module("pve")
    if not hasattr(pve, "facts"):
        print("FAILED  pve.facts is missing")
        return 1, 1
    subnet, gateway = config.get("MOP_PVE_SUBNET"), config.get("MOP_PVE_GATEWAY")
    home = config.get("MOP_HOME")
    for base in (9000, 12000):
        # Роль pve: разбор сети jinja, VMID_MAX из vmid.yml, маршруты -- копией.
        f = pve.facts(base)
        check(f"network {base}", (f["network"], str(f["prefix"]), f["gateway"]),
              (subnet.split("/")[0], subnet.split("/")[1], gateway))
        check(f"vmid_max {base}", f["vmid_max"], base + 999)
        check(f"routes {base}", f["routes"],
              _old(SNIPPET_ROUTES, base, subnet, str(base)).split())
        # Пути, которые YAML писал литералом.
        check("wrapper", f["wrapper"], "/usr/local/sbin/mop-pve")
        check("ssh_key", f["ssh_key"], f"{home}/.ssh/mop-body")
        check("known_hosts", f["known_hosts"], f"{home}/.ssh/known_hosts-mop-body")
        check("no project, no image", "image" in f, False)

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
                check(f"{what} image", (str(f["image"]["vmid"]), f["image"]["name"],
                                        f["gateway"], str(f["prefix"]), f["image"]["address"]),
                      tuple(tmpl))
                check(f"{what} stage", (str(f["stage"]["vmid"]), f["stage"]["name"],
                                        f["stage"]["address"]), tuple(stage))
                check(f"{what} present", (f["image"]["present"], f["stage"]["present"],
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
    check("mop driver pve-facts", got,
          json.loads(json.dumps(pve.facts(9000, project="mop", listing=listing))))
    return cases, bad


if __name__ == "__main__":
    sys.exit(main())
