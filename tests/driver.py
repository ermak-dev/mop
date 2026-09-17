#!/usr/bin/env python3
"""Проверка реестра драйверов узла без пула: python3 tests/driver.py

Драйвер отвечает на третий вопрос системы — В ЧЁМ живёт папет (docs/DRIVER.md).
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

from mop import driver  # noqa: E402


def plugin(**attrs):
    """Модуль-плагин с заданными атрибутами; остальное берёт контрактом."""
    mod = types.ModuleType("fake")
    for verb in driver.VERBS:
        mod.__dict__[verb] = lambda *a, **k: None
    mod.SESSION_PY = "/opt/mop/mop/session.py"
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
    ("a verb that is not callable", plugin(argv="ssh"), False),
    # SESSION_PY уезжает в шелл внутри тела. Пустое значение там молча
    # соберётся в `python3  probe <clone>` — python прочитает probe как файл.
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


# Драйвер pve: тело — контейнер LXC, и всё про него ВЫВОДИТСЯ ИЗ ИМЕНИ.
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

    # Драйвер УЗЛА, а не папета: значение приезжает в окружение агента юнитом,
    # и подставить его запросом с шины нельзя. Дефолт — host: узел, ничего про
    # драйвер не знающий, обязан вести себя как раньше.
    host = driver.module("host")
    for what, verb, want in HOST_ARGV:
        cases += 1
        got = getattr(host, verb)("pu-mop-1")
        if got != want:
            bad += 1
            print(f"FAILED  host.{verb}: {what}\n  wanted: {want!r}\n  got: {got!r}")

    # Драйвер УЗЛА, а не папета, и спрашивают его ДВОЕ с разным окружением:
    # агент из юнита systemd и внешний врапер из процесса задачи Nomad. Пока
    # значение жило строкой Environment= в юните, врапер его не видел и молча
    # поднимал папета драйвером host — на гипервизоре это значит «прямо на
    # гипервизоре, мимо тела». Поэтому источник правды — файл узла.
    saved_env = os.environ.pop("MOP_DRIVER", None)
    saved_file = driver.NODE_FILE
    tmp = os.path.join(tempfile.mkdtemp(), "driver")
    try:
        driver.NODE_FILE = tmp
        cases += 1
        if driver.current_name() != "host":
            bad += 1
            print("FAILED  a node that says nothing about a driver must be host")
        cases += 1
        with open(tmp, "w") as f:
            f.write("pve\n")
        if driver.current_name() != "pve":
            bad += 1
            print("FAILED  current_name() must read the node's file — an empty "
                  "environment is the wrapper's normal case")
        cases += 1
        with open(tmp, "w") as f:
            f.write("\n")
        if driver.current_name() != "host":
            bad += 1
            print("FAILED  an empty file must mean host, not an empty driver name")
        cases += 1
        with open(tmp, "w") as f:
            f.write("pve\n")
        os.environ["MOP_DRIVER"] = "host"
        if driver.current() is not host:
            bad += 1
            print("FAILED  MOP_DRIVER must outrank the node's file")
    finally:
        driver.NODE_FILE = saved_file
        os.environ.pop("MOP_DRIVER", None)
        if saved_env is not None:
            os.environ["MOP_DRIVER"] = saved_env

    # ── драйвер pve: всё выводится из имени ──────────────────────────────
    pve = driver.module("pve")

    cases += 1
    if not isinstance(driver.get("pve"), dict):
        bad += 1
        print("FAILED  registry didn't find the pve driver")

    # VMID в своём диапазоне и НЕ пересекается с диапазоном шаблонов: шаблон
    # живёт рядом с телами и сносится теми же глаголами, так что налезь один
    # на другой — снос папета унёс бы образ шарда.
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

    for shard in ("mop", "rugent", "cloudpub"):
        cases += 1
        t = pve.template_vmid(shard)
        if not pve.TMPL_MIN <= t <= pve.TMPL_MAX:
            bad += 1
            print(f"FAILED  pve.template_vmid({shard!r}) = {t}, outside "
                  f"{pve.TMPL_MIN}..{pve.TMPL_MAX}")
        if pve.BODY_MIN <= t <= pve.BODY_MAX:
            bad += 1
            print(f"FAILED  template {t} lands in the body range — a wipe would "
                  f"take the shard's image with it")
        cases += 1
        # Имя шаблона обязано быть под охраной префикса (root-обёртка на
        # гипервизоре пускает только pu-*), но НЕ быть именем папета: иначе
        # ростер тел показал бы образ живым папетом.
        tn = pve.template_name(shard)
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

    # Соединение ВРАПЕРА живёт столько же, сколько папет, и мультиплексировать
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
    cases += 1
    if "ServerAliveInterval" not in joined:
        bad += 1
        print("FAILED  pve.run_argv holds a connection for the puppet's whole "
              "life; without a keepalive a silent NAT drop reads as a dead puppet")

    # Аварийный путь — НЕ ssh: он нужен ровно тогда, когда у тела сломана сеть,
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

    # Транскрипты лежат ВНУТРИ тела: считать их путём на гипервизоре значит
    # молча получить нулевой расход токенов у контейнерных папетов.
    cases += 1
    if pve.projects_dir("pu-mop-1") == host.projects_dir("pu-mop-1") \
            and pve.HOME != host.HOME:
        bad += 1
        print("FAILED  pve.projects_dir must point inside the body")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
