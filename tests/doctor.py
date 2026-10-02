#!/usr/bin/env python3
"""Шов проверок doctor без пула: python3 tests/doctor.py

`mop server doctor` -- группы проверок модулями mop/server/doctor/ (#356), по
образцу драйверов узла и профилей LLM: один файл -- одна группа,
обнаружение глобом каталога, контракт громко при загрузке, потребитель
(mop/cli/server/doctor.py) не ветвится по группе. Здесь -- контракт, имена
групп, потребляемое против объявленного и вывод командлета байт в байт тот
же, что до шва.
"""
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, restored, run_command  # noqa: E402
sys.path.insert(0, ROOT)

os.environ.setdefault("MOP_SERVER_LAN", "10.0.0.1")

from mop.common import bus, puppets  # noqa: E402

CONSUMER = os.path.join(ROOT, "mop", "cli", "server", "doctor.py")


def run_server(main, args):
    from mop.cli.server import _maintenance
    from _lib import patched
    with patched(_maintenance, require=lambda: None):
        return run_command(main, args)


def registry():
    try:
        from mop.server import doctor
        return doctor
    except ImportError:
        return None


def plugin(**attrs):
    """Модуль-подделка группы: полный контракт, поверх -- attrs (None
    снимает атрибут)."""
    mod = types.ModuleType("fake")
    base = {"__doc__": "fake checks: what they catch",
            "diagnose": lambda: [], "treat": lambda issue: "", "prepare": lambda issues: (None, [])}
    for k, v in {**base, **attrs}.items():
        if v is not None:
            setattr(mod, k, v)
    return mod


# (что, модуль, принять ли)
CONTRACT = [
    ("the full contract", plugin(), True),
    ("no diagnose()", plugin(diagnose=None), False),
    ("no treat()", plugin(treat=None), False),
    ("no prepare()", plugin(prepare=None), False),
    ("diagnose is not callable", plugin(diagnose="x"), False),
    ("no docstring: the group's line in usage", plugin(__doc__=None), False),
    ("an empty docstring", plugin(__doc__="  \n"), False),
]


def consumed_names():
    """Что потребитель берёт у модуля группы: `check.<имя>`, где check --
    модуль из doctor.module()."""
    text = open(CONSUMER).read()
    return set(re.findall(r"\bcheck\.([A-Za-z_]+)", text))


# ── #356: шов проверок doctor ───────────────────────────────────────────
# HYPOTHESIS: `mop server doctor` -- один вызов puppets.diagnose(); проверкам
# эпика #355 (логины, диск, расписание) встать некуда, и каждая добавилась
# бы веткой в командлет.
# SOLUTION: реестр mop/server/doctor/ (plugins.discover, contract,
# CONSUMED); первая группа puppets -- сегодняшние diagnose/treat целиком;
# `mop server doctor [group] [--fix]`, вывод тот же.
# STATUS: FIXED — see #356
def check_contract_356(c, doctor):
    for what, mod, ok in CONTRACT:
        try:
            got = doctor.contract("fake", mod)
            refused = False
        except RuntimeError as e:
            got, refused = None, True
            c.check(f"#356 contract: the refusal names the file: {e}", "fake" in str(e))
        c.check(f"#356 contract: {what}", refused != ok,
                f"{'refused' if refused else 'accepted'}, wanted the opposite")
    c.expect("#356 contract: doc is the first docstring line",
             doctor.contract("fake", plugin(__doc__="one\ntwo"))["doc"], "one")


def check_registry_356(c, doctor):
    groups = doctor.groups()
    c.check(f"#356 the puppets group is there: {sorted(groups)}", "puppets" in groups)
    for name in groups:
        c.check(f"#356 {name!r} is usable as a subcommand word",
                re.fullmatch(r"[a-z][a-z0-9-]*", name) is not None)
    c.expect("#356 the group names are unique", len(set(groups)), len(groups))
    # Потребляемое -- объявлено, и у каждой группы есть.
    used = consumed_names()
    c.check(f"#356 the consumer calls only doctor.CONSUMED: {sorted(used)}",
            used and not (used - set(doctor.CONSUMED)))
    c.check(f"#356 CONSUMED has nothing the consumer does not call: {doctor.CONSUMED}",
            not (set(doctor.CONSUMED) - used))
    for name in groups:
        mod = doctor.module(name)
        for attr in doctor.CONSUMED:
            c.check(f"#356 {name} has {attr}", callable(getattr(mod, attr, None)))
    # Потребитель не ветвится по имени группы.
    text = open(CONSUMER).read()
    c.check("#356 the consumer names no group", not re.search(r"['\"]puppets['\"]", text))


def check_select_356(c, doctor):
    names = ("disk", "puppets")
    # Третье -- безопасный режим (#359): без --safe всегда False.
    for argv, want in [([], (["disk", "puppets"], False, False)),
                       (["--fix"], (["disk", "puppets"], True, False)),
                       (["puppets"], (["puppets"], False, False)),
                       (["puppets", "--fix"], (["puppets"], True, False)),
                       (["--fix", "disk"], (["disk"], True, False))]:
        c.expect(f"#356 select {argv}", doctor.select(names, argv), want)
    for argv in (["nope"], ["puppets", "disk"], ["--force"]):
        try:
            doctor.select(names, argv)
            c.fail(f"#356 select {argv} must refuse")
        except ValueError as e:
            c.check(f"#356 the refusal of {argv} names the groups: {e}",
                    "disk" in str(e) and "puppets" in str(e))


# Вывод до шва, снятый с командлета до правки: байт в байт тот же.
# С #384 контракт --fix снова проще: логины провайдеров умерли вместе с
# арендами, лечение login+nudge -- только побудка (#290); раздач нет.
ISSUES = [{"name": "pu-mop-1", "alloc": {"NodeName": "n1"},
           "diagnosis": "HUNG (not responding)", "action": "restart"},
          {"name": "pu-mop-2", "alloc": {"NodeName": "n2"},
           "diagnosis": "not logged in", "action": "login+nudge"},
          {"name": "pu-mop-3", "alloc": None,
           "diagnosis": "queued — no free slots in the pool", "action": None}]
OUT_TABLE = ("pu-mop-1  n1  HUNG (not responding)               [restart]\n"
             "pu-mop-2  n2  not logged in                       [login+nudge]\n"
             "pu-mop-3  -   queued — no free slots in the pool\n")
OUT_FIX = ("pu-mop-1  n1  HUNG (not responding)\n"
           "pu-mop-2  n2  not logged in\n"
           "pu-mop-3  -   queued — no free slots in the pool\n"
           "\n"
           "  pu-mop-1: treated restart\n"
           "  pu-mop-2: treated login+nudge\n")
OUT_NONE = ("pu-mop-3  -  queued — no free slots in the pool\n"
            "\n"
            "no auto-treatment — operator's call\n")


def check_output_356(c, argvs):
    from mop.cli.server import doctor as cmd
    from mop.cli.server import _maintenance
    # Группы после шва (disk, #358) пусты: вывод до шва -- вывод группы
    # puppets, и соседняя группа не должна ни ходить в шину, ни добавлять строк.
    others = [cmd.doctor.module(n) for n in cmd.doctor.groups() if n != "puppets"] \
        if hasattr(cmd.doctor, "groups") else []
    saved = [(m, m.diagnose) for m in others]
    for m in others:
        m.diagnose = lambda: []
    try:
        _output_356(c, argvs, cmd)
    finally:
        for m, fn in saved:
            m.diagnose = fn


def _output_356(c, argvs, cmd):
    with restored(puppets, "diagnose", "treat"), restored(bus, "call_cluster"):
        puppets.treat = lambda issue: f"treated {issue['action']}"
        for pre in argvs:
            for issues, argv, want in [([], [], "pool is healthy: nothing stuck\n"),
                                       (ISSUES, [], OUT_TABLE),
                                       (ISSUES, ["--fix"], OUT_FIX),
                                       (ISSUES[2:], [], OUT_NONE)]:
                puppets.diagnose = lambda issues=issues: issues
                out, err, code = run_server(cmd.main, pre + argv)
                c.expect(f"#356 mop server doctor {' '.join(pre + argv)}: the same output",
                         (out, err, code), (want, "", 0))

# ── #357: логин лечится арендой из реестра, а не локальным файлом ──────
# HYPOTHESIS: лечение login+nudge гейтится ~/.claude/.credentials.json машины
# запуска (keys.credentials_fresh/push_login); у сервера такого файла нет, и
# расписанный с сервера doctor (#359) протухших логинов не лечит, хотя
# реестр кредитов держит их на сервере.
# SOLUTION: treat группы puppets перед побудкой зовёт cred_push (#360) по
# имени папета; не дошло -- причина вместо побудки. Папет без аренды не
# лечится: диагноз называет `mop update --cred`, action нет. Локального
# пути в doctor нет вовсе.
# STATUS: FIXED — see #357
# ── #358: группа disk -- подметание узлов через шину ──────────────────────
# HYPOTHESIS: подметание диска запускает только nomad periodic pu-cleanup, и
# его итог лежит в логах аллокаций: doctor не видит ни кто подмёл, ни кто
# под давлением, ни кто отказал.
# SOLUTION: группа mop/server/doctor/disk.py: diagnose спрашивает глагол
# агента sweep у каждого готового узла всухую (MOP_SWEEP_DRY), treat метёт
# по-настоящему -- `mop server doctor` без --fix ничего не сносит, как и остальные
# группы. Колонка «где» у проблемы -- поле node (узловая проблема) либо узел
# аллокации (проблема папета): doctor.where.
# STATUS: FIXED — see #358
GB = 1024 * 1024


def ok(freed_kb, free_gb, warnings=(), min_gb=60):
    return {"freed_kb": freed_kb, "free_gb": free_gb, "min_gb": min_gb,
            "warnings": list(warnings)}


class Silent(Exception):
    pass


# (что, ответ узла, (диагноз, лечение) | None -- проблемы нет)
DISK = [
    ("nothing to sweep, room to spare", ok(0, 80), None),
    ("orphans to sweep", ok(3 * GB // 2, 80), ("1.5 GB to sweep, 80 GB free", "sweep")),
    ("pressure with nothing to sweep: tier 3 caps live targets",
     ok(0, 41), ("disk pressure: 41 GB free < 60 GB", "sweep")),
    ("pressure and orphans", ok(512 * 1024, 41),
     ("disk pressure: 41 GB free < 60 GB, 512.0 MB to sweep", "sweep")),
    ("a refusal inside the sweep: the operator's call",
     ok(None, None, ["$HOME has no puppets/ (unmounted ecryptfs?) -- refusing to sweep"]),
     ("$HOME has no puppets/ (unmounted ecryptfs?) -- refusing to sweep", None)),
    ("a warning beside a sweep", ok(GB, 80, ["refusing tier 1; tiers 2-3 still run"]),
     ("1.0 GB to sweep, 80 GB free; refusing tier 1; tiers 2-3 still run", "sweep")),
    ("a body-driver node: no totals, no warnings", ok(None, None), None),
    ("the agent refused", {"error": "pu-sweep exit 2: rm: Permission denied"},
     ("sweep FAILED: pu-sweep exit 2: rm: Permission denied", None)),
    ("the agent is silent (#163): a reason, not an empty list",
     Silent("node agent n1 did not answer in 600s"),
     ("agent silent, not swept: node agent n1 did not answer in 600s", None)),
    ("no answer at all", None, ("agent silent, not swept: no answer", None)),
]


def check_disk_358(c, doctor):
    groups = doctor.groups()
    if not c.check(f"#358 the disk group is there: {sorted(groups)}", "disk" in groups):
        return
    disk = doctor.module("disk")
    for what, answer, want in DISK:
        got = disk.issues(["n1"], {"n1": answer})
        if want is None:
            c.expect(f"#358 disk: {what}: no issue", got, [])
            continue
        c.expect(f"#358 disk: {what}", [(i["diagnosis"], i["action"]) for i in got], [want])
        c.expect(f"#358 disk: {what}: the row sits on its node",
                 [doctor.where(i) for i in got], ["n1"])
    # Узлы -- по порядку, каждый своей строкой, молчание одного не уносит
    # остальных.
    got = disk.issues(["n2", "n1", "n3"], {"n1": ok(GB, 80), "n3": ok(0, 80)})
    c.expect("#358 disk: every node answers for itself, in order",
             [(doctor.where(i), i["action"]) for i in got], [("n1", "sweep"), ("n2", None)])


# ── #363: брошенные тела контейнерного узла -- проблема, и doctor их метёт ──
# HYPOTHESIS: ярус 0 не даёт итоговой строки, freed_kb и free_gb -- None, и
# issue() возвращает None: сухой ответ узла с брошенными телами не становится
# проблемой, --fix (и таймер mop-doctor) не зовёт sweep. pu-mop-5 и pu-mop-6
# стояли на hyper с 2026-09-18.
# SOLUTION: issue() читает поле bodies ответа агента (узловая половина #363):
# bodies > 0 -- «N orphaned bodies to sweep», action sweep; treat называет
# снесённые тела.
# STATUS: FIXED — see #363
def bodies(n, warnings=()):
    return dict(ok(None, None, warnings), bodies=n)


DISK_BODIES = [
    ("orphaned bodies", bodies(2), ("2 orphaned bodies to sweep", "sweep")),
    ("one orphaned body", bodies(1), ("1 orphaned body to sweep", "sweep")),
    ("a container node with nothing to sweep", bodies(0), None),
    ("a tier-0 refusal: the operator's call",
     bodies(None, ["0 live sessions -- tmux unreachable, not 8 orphans; refusing to sweep"]),
     ("0 live sessions -- tmux unreachable, not 8 orphans; refusing to sweep", None)),
]


def check_disk_bodies_363(c, doctor):
    disk = doctor.module("disk")
    for what, answer, want in DISK_BODIES:
        got = disk.issues(["n1"], {"n1": answer})
        if want is None:
            c.expect(f"#363 disk: {what}: no issue", got, [])
            continue
        c.expect(f"#363 disk: {what}", [(i["diagnosis"], i["action"]) for i in got], [want])
    # Лечение -- настоящий sweep; строка называет, сколько тел снесено.
    asked = []
    with restored(bus, "request"):
        bus.request = lambda node, verb, **kw: asked.append((node, verb, kw.get("dry"))) \
            or bodies(2)
        got = disk.treat({"node": "n1", "action": "sweep"})
    c.expect("#363 disk treat: a real sweep of the node", asked, [("n1", "sweep", None)])
    c.expect("#363 disk treat: names the bodies it destroyed", got,
             "n1: destroyed 2 orphaned bodies")


def check_where_358(c, doctor):
    c.expect("#358 where: a puppet's issue sits on its allocation's node",
             doctor.where({"alloc": {"NodeName": "n1"}}), "n1")
    c.expect("#358 where: a node's issue names the node itself",
             doctor.where({"alloc": None, "node": "n2"}), "n2")
    c.expect("#358 where: nowhere yet", doctor.where({"alloc": None}), "-")


# ── #359: безопасный режим для расписания ───────────────────────────────
# HYPOTHESIS: расписанный doctor --fix лечил бы всё actionable -- рестарты,
# alloc stop, /model, перерегистрацию: без оператора рядом это рестарт
# работающего папета каждый час, стоит ему показаться залипшим.
# SOLUTION: `--fix --safe` лечит только SAFE -- login+nudge (аренда из
# реестра, #357, разговор переживает) и sweep (#358, у pu-sweep свои
# предохранители); остальное печатается строкой без исполнения. Фильтр --
# чистая doctor.treats.
# STATUS: FIXED — see #359
OUT_SAFE = ("pu-mop-1  n1  HUNG (not responding)\n"
            "pu-mop-2  n2  not logged in\n"
            "pu-mop-3  -   queued — no free slots in the pool\n"
            "\n"
            "  pu-mop-1: [restart] not treated — --safe treats only login+nudge, sweep\n"
            "  pu-mop-2: treated login+nudge\n")


def check_safe_359(c, doctor):
    names = ("disk", "puppets")
    for argv, want in [(["--fix", "--safe"], (["disk", "puppets"], True, True)),
                       (["--safe", "disk", "--fix"], (["disk"], True, True))]:
        c.expect(f"#359 select {argv}", doctor.select(names, argv), want)
    try:
        doctor.select(names, ["--safe"])
        c.fail("#359 --safe without --fix must refuse: it narrows the treatment")
    except ValueError as e:
        c.check(f"#359 the refusal names --fix: {e}", "--fix" in str(e))
    c.expect("#359 the safe actions", tuple(doctor.SAFE), ("login+nudge", "sweep"))
    for action, safe_too in [("login+nudge", True), ("sweep", True), ("restart", False),
                             ("stop", False), ("model", False), ("update", False)]:
        issue = {"name": "x", "alloc": None, "diagnosis": "d", "action": action}
        c.expect(f"#359 --fix treats {action}", doctor.treats(issue, False), True)
        c.expect(f"#359 --fix --safe treats {action}: {safe_too}",
                 doctor.treats(issue, True), safe_too)
    none = {"name": "x", "alloc": None, "diagnosis": "d", "action": None}
    c.expect("#359 no action: nothing to treat, safe or not",
             (doctor.treats(none, False), doctor.treats(none, True)), (False, False))
    # Вывод: небезопасное названо строкой лечения и не исполнено.
    from mop.cli.server import doctor as cmd
    from mop.cli.server import _maintenance
    treated = []
    others = [doctor.module(n) for n in doctor.groups() if n != "puppets"]
    saved = [(m, m.diagnose) for m in others]
    for m in others:
        m.diagnose = lambda: []
    try:
        with restored(puppets, "diagnose", "treat"), restored(bus, "call_cluster"):
            puppets.diagnose = lambda: ISSUES
            puppets.treat = lambda issue: treated.append(issue["action"]) or \
                f"treated {issue['action']}"
            bus.call_cluster = lambda verb, **kw: {"ok": True, "lease": "anton",
                                                   "node": "n2", "result": "OK"}
            out, err, code = run_server(cmd.main, ["--fix", "--safe"])
    finally:
        for m, fn in saved:
            m.diagnose = fn
    c.expect("#359 mop server doctor --fix --safe: the output", (out, err, code), (OUT_SAFE, "", 0))
    c.expect("#359 only the safe action was executed", treated, ["login+nudge"])
    c.check("#409 server doctor is not a master MCP tool", not hasattr(cmd, "MCP"))


# ── #370: неизвестное действие лечения -- отказ, а не рестарт ───────────
# HYPOTHESIS: puppets.treat -- цепочка if по голым строкам, и всё, что не
# совпало (новое действие, опечатка), уходило в безусловный рестарт: он
# стирает разговор и может убить работу, опаснее любого отказа.
# SOLUTION: действия -- константы рядом с state._ACTIONS (одно определение
# на diagnose, treat и doctor), treat -- таблица {действие: функция}, restart
# -- её явная строка; неизвестное -- "<action> failed: unknown action" без
# вызова кластера.
# STATUS: FIXED — see #370
def check_unknown_action_370(c):
    from mop.common import state
    asked = []
    issue = {"name": "pu-mop-1", "alloc": {"NodeName": "n1"}, "diagnosis": "d"}
    with restored(puppets, "_cluster"), restored(bus, "login", "request"):
        bus.login = lambda: "anton"
        puppets._cluster = lambda verb, **kw: asked.append(verb) or {}
        bus.request = lambda *a, **kw: asked.append(a[1]) or {}
        got = puppets.treat(dict(issue, action="frobnicate"))
        c.expect("#370 an unknown action is refused", got, "frobnicate failed: unknown action")
        c.expect("#370 an unknown action reaches no cluster verb", asked, [])
        asked.clear()
        puppets.treat(dict(issue, action="restart"))
        c.expect("#370 restart is an explicit row of the table", asked, ["restart"])
    table = getattr(puppets, "TREAT", None)
    declared = getattr(state, "ACTIONS", None)
    if not c.check("#370 puppets.TREAT and state.ACTIONS exist",
                   table is not None and declared is not None):
        return
    emitted = {a for a in state._ACTIONS.values() if a}
    emitted |= {a for a in map(state.spec_action, (None, "free", "busy")) if a}
    c.check(f"#370 every action _ACTIONS/spec_action emits is declared: {emitted}",
            emitted <= set(declared))
    c.expect("#370 treat has a row for every declared action",
             sorted(table), sorted(declared))
    # diagnose кладёт действие в словарь проблемы: голая строка там -- мимо
    # объявления, её treat мог бы и не знать.
    src = open(os.path.join(ROOT, "mop", "common", "puppets.py")).read()
    c.expect("#370 diagnose names actions by constant, not by a bare string",
             re.findall(r'"action": "[^"]*"', src), [])


def main():
    c = Checks()
    doctor = registry()
    if c.check("#356 mop.server.doctor, the registry of check groups", doctor is not None):
        check_contract_356(c, doctor)
        check_registry_356(c, doctor)
        check_select_356(c, doctor)
        check_output_356(c, [[], ["puppets"]])
        check_where_358(c, doctor)
        check_disk_358(c, doctor)
        check_disk_bodies_363(c, doctor)
        check_safe_359(c, doctor)
        check_unknown_action_370(c)
    else:
        # До шва: вывод сегодняшнего командлета -- тот, что шов обязан сохранить.
        check_output_356(c, [[]])
    return c.report("doctor")


if __name__ == "__main__":
    sys.exit(main())
