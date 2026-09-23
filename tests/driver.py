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
    mod.BODY_IS_NODE = False
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
    ("BODY_IS_NODE not declared", plugin(BODY_IS_NODE=None), False),
    ("BODY_IS_NODE not a bool", plugin(BODY_IS_NODE="yes"), False),
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
    if host.BODY_IS_NODE is not True:
        bad += 1
        print("FAILED  host.BODY_IS_NODE must be True — the body IS the node")

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
    if pve.BODY_IS_NODE is not False:
        bad += 1
        print("FAILED  pve.BODY_IS_NODE must be False — a body is a container")

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
    # Модуль с атрибутом BODY_IS_NODE отдаёт только `current()`, и он про свой
    # узел; спросить про чужой можно лишь по имени, через реестр. Перепутать
    # эти две вещи легко, а отказ приходит не там: `mop delete` успевает снять
    # джоб и падает уже ПОСЛЕ этого на AttributeError, оставляя тело сиротой
    # (поймано 22.09 живым прогоном, на двух телах сразу).
    for name in ("host", "pve"):
        cases += 1
        c = driver.require(name)
        if not isinstance(c, dict) or "body_is_node" not in c:
            bad += 1
            print(f"FAILED  driver.require({name!r}) must be a mapping with "
                  f"body_is_node, got {type(c).__name__}")

    cases += 1
    if driver.require("host")["body_is_node"] is not True \
            or driver.require("pve")["body_is_node"] is not False:
        bad += 1
        print("FAILED  body_is_node must tell a node-body driver from a "
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

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
