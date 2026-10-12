#!/usr/bin/env python3
"""Проверка сборки образа проекта без пула: python3 tests/image.py

Чистая часть сборки — что уезжает плейбуку в --extra-vars из манифеста.
Ошибка тихая и уже случалась (#26): None-половина, переданная как null,
не подменяется `default('')` в условиях плейбука, и len(None) ронял сборку
проекта, у которого есть только задачи. Поэтому половины без содержимого не
передаются вовсе, и это свойство держит эта проверка (#49).
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, FakeNomad, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import image  # noqa: E402

# В образ едет только sandbox (#61): bootstrap играется при старте песочницы
# сервером, и в сборке ему делать нечего.
GOT = {"project": "proj", "asks": {"MOP_MEM_MB": "2048"}, "alien": [], "legacy": [],
       "sandbox_vars": None, "sandbox_tasks": "/tmp/x/sandbox-tasks.yml",
       "bootstrap_vars": "/tmp/x/bootstrap-vars.yml",
       "bootstrap_tasks": "/tmp/x/bootstrap-tasks.yml"}


# ── #321: «контейнерный ли узел пула» -- одним определением ────────────
# Характеристика до переезда: тексты и типы отказов image, строки announce
# на смешанном пуле (контейнер, host, неизвестный драйвер), build с узлом
# и без. Зелёная на старом коде и на новом. STATUS: FIXED — see #321
POOL_321 = {"agent1": {"mop_driver": "pve"}, "host1": {"mop_driver": "host"},
            "weird": {"mop_driver": "pvee"}}


def check_node_check_321(c):
    def api():
        return FakeNomad(meta={n: dict(m) for n, m in POOL_321.items()})
    for node, want in (
            ("ghost", "no node ghost in the pool: agent1, host1, weird"),
            ("host1", "host1: its bodies are not containers, there is no image to build there"),
            ("weird", "weird: unknown driver 'pvee'; available: host, pve (docs/DRIVER.md)")):
        try:
            image.container_nodes(api(), node)
            c.fail(f"#321 container_nodes({node!r}) must refuse")
        except RuntimeError as e:
            c.expect(f"#321 container_nodes({node!r}): RuntimeError text", str(e), want)
    c.expect("#321 container_nodes(agent1): only it",
             image.container_nodes(api(), "agent1"), {"agent1": {"mop_driver": "pve"}})
    c.expect("#321 container_nodes(): every node, as today",
             sorted(image.container_nodes(api())), ["agent1", "host1", "weird"])

    a = api()
    c.expect("#321 announce on a mixed pool: a row per node, host and bad driver named",
             image.announce("proj", api=a),
             [("agent1", "announced, serves proj"), ("host1", "not a container node"),
              ("weird", "weird: unknown driver 'pvee'; available: host, pve (docs/DRIVER.md)")])
    c.expect("#321 announce(node): only it", image.announce("other", api=api(), node="agent1"),
             [("agent1", "announced, serves other")])

    calls = []
    # account (#570): заглушка clear берёт список build'а -- контракт тот же,
    # что у настоящей.
    with patched(image, clear=lambda project, force=False, api=None, node=None,
                             account=None: calls.append(("clear", node)) or account,
                 bake=lambda *a, node=None, **kw: calls.append(("bake", node)) or 0,
                 restore=lambda gone, api=None: None):
        a = api()
        got = image.build("git@h:g/proj.git", {"project": "proj"}, api=a, node="agent1")
        c.expect("#321 build(node): clears and bakes there, announces only there",
                 (calls, got["announced"]),
                 ([("clear", "agent1"), ("bake", "agent1")],
                  [("agent1", "announced, serves proj")]))
        c.expect("#321 build(node): the pool's meta read once, not once per check",
                 sum(1 for x in a.calls if x[0] == "nodes_meta"), 1)
        for node in ("host1", "ghost"):
            calls.clear()
            try:
                image.build("git@h:g/proj.git", {"project": "proj"}, api=api(), node=node)
                c.fail(f"#321 build({node}) must refuse")
            except RuntimeError:
                c.expect(f"#321 build({node}): refused before the first stop", calls, [])
        calls.clear()
        got = image.build("git@h:g/proj.git", {"project": "proj"}, api=api())
        c.expect("#321 build(): every node's row, as today",
                 (calls, [n for n, _ in got["announced"]]),
                 ([("clear", None), ("bake", None)], ["agent1", "host1", "weird"]))


# ── #570: отказ посреди снятия не оставляет снятых молча ───────────────
# HYPOTHESIS: build звал clear до try, а clear записывал папета в gone
# лишь после всех трёх шагов снятия: отказ wait/wipe ВТОРОГО папета уносил
# из build исключение целиком, и УЖЕ СНЯТОГО первого не поднимал никто --
# список затронутых погибал вместе с исключением, и молча.
# SOLUTION: clear ведёт список затронутых списком вызывающего и записывает
# каждого ДО первого шага снятия, а build держит clear под той же защитой,
# что и bake: отказ посреди подготовки или сборки поднимает снятых и
# доносит исходный отказ; отказ подъёма дописывается к исходному, а не
# затирает его; нетронутые не поднимаются.
# RESULT: RED на исходном коде -- 7 отказов, все по делу (снятые не
# подняты, ветка потеряна, шага подъёма нет, отказ подъёма затёрт);
# мутации ловятся каждая своей половиной: запись ПОСЛЕ шагов снятия -- 4
# отказа (учёт половин), clear до try -- 7 (учёт + затирание), отказ
# подъёма, затирающий исходный, -- 7 (видны оба отказа).
# STATUS: FIXED — see #570
class RefusingNomad(FakeNomad):
    """FakeNomad, чей register отказывает поимённо: подъём снятого папета
    обязан быть отказоспособным и в проверке, а не только в жизни."""

    def __init__(self, *a, register_refuses=(), **kw):
        super().__init__(*a, **kw)
        self.register_refuses = set(register_refuses)

    def register(self, job):
        who = (job.get("Job") or job).get("ID")
        if who in self.register_refuses:
            raise RuntimeError(f"Nomad refused {who}")
        super().register(job)


def rows_570(*who, origin="git@h:g/p.git"):
    """rows для project_rows: (имя, узел[, ветка]) -> свободные тела."""
    from mop.server import spec
    out = []
    for name, node, *branch in who:
        job = spec.job_spec(name, origin, branch=branch[0] if branch else None)["Job"]
        out.append({"name": name, "node": node, "job": job, "container": True,
                    "state": "free", "kind": "free"})
    return out


def run_570(rows, meta, stop_refuses=(), wipe_refuses=(), register_refuses=(),
            bake_rc=0, bake_raises=None, force=False):
    """Один прогон image.build над подделками: Nomad -- RefusingNomad,
    остановка и снос тел -- заглушки шины, плейбук -- заглушка bake.
    -> {result, err, api, steps, baked}."""
    from mop.common import puppets

    def wait(name):
        if name in stop_refuses:
            raise RuntimeError(f"allocation {name} won't stop -- node on fire")

    def wipe(node, name, *a, **k):
        if name in wipe_refuses:
            raise RuntimeError(f"wipe of {name} on {node} refused -- agent said no")

    baked = []

    def bake_stub(*a, **k):
        baked.append(1)
        if bake_raises is not None:
            raise bake_raises
        return bake_rc

    steps, api = [], RefusingNomad(meta=meta, register_refuses=register_refuses)
    with patched(image, project_rows=lambda project, api=None: rows,
                 bake=bake_stub), \
            patched(puppets, _wait_stopped=wait, wipe=wipe):
        try:
            result = image.build("git@h:g/p.git", {"project": "p"}, force=force,
                                 on_step=steps.append, api=api)
            err = None
        except Exception as e:  # noqa: BLE001 -- исход прогона и есть предмет проверки
            result, err = None, e
    return {"result": result, "err": err, "api": api, "steps": steps,
            "baked": len(baked)}


def check_clear_recovery_570(c):
    pool = {"n1": {"mop_driver": "pve"}, "n2": {"mop_driver": "pve"}}
    raising = "raising the project's puppets again"

    # Сбой сноса второго из двух: первый уже снят -- обязан подняться.
    r = run_570(rows_570(("pu-p-1", "n1", "feat/a"), ("pu-p-2", "n2")), pool,
                wipe_refuses=("pu-p-2",))
    c.expect("#570 wipe failure of the second: the original refusal reaches the caller",
             str(r["err"]), "wipe of pu-p-2 on n2 refused -- agent said no")
    c.expect("#570 wipe failure of the second: both touched puppets are raised again",
             sorted(r["api"].jobs), ["pu-p-1", "pu-p-2"])
    c.check("#570 wipe failure of the second: the accounted branch travels through",
            ((r["api"].jobs.get("pu-p-1") or {}).get("Meta") or {}).get("branch") == "feat/a",
            r["api"].jobs.get("pu-p-1"))
    c.check("#570 wipe failure of the second: bake never ran, the raising step did show",
            r["baked"] == 0 and raising in r["steps"], (r["baked"], r["steps"]))

    # Ошибка ожидания после deregister: снятый по джобу тоже учтён.
    r = run_570(rows_570(("pu-p-1", "n1"), ("pu-p-2", "n2")), pool,
                stop_refuses=("pu-p-2",))
    c.expect("#570 wait failure after deregister: the original refusal reaches the caller",
             str(r["err"]), "allocation pu-p-2 won't stop -- node on fire")
    c.expect("#570 wait failure after deregister: the half-cleared one is raised again too",
             sorted(r["api"].jobs), ["pu-p-1", "pu-p-2"])

    # Третий, до которого не дошли: не сносится и не поднимается.
    r = run_570(rows_570(("pu-p-1", "n1"), ("pu-p-2", "n2"), ("pu-p-3", "other")),
                pool, wipe_refuses=("pu-p-2",))
    c.expect("#570 a puppet past the failure is neither cleared nor raised",
             (sorted(r["api"].jobs),
              [a[1] for a in r["api"].calls if a[0] == "deregister"]),
             (["pu-p-1", "pu-p-2"], ["pu-p-1", "pu-p-2"]))

    # Отказ плейбука: снятые поднимаются, отказ доходит как есть.
    r = run_570(rows_570(("pu-p-1", "n1")), pool,
                bake_raises=RuntimeError("ansible-playbook exploded"))
    c.expect("#570 bake failure: the original refusal reaches the caller",
             str(r["err"]), "ansible-playbook exploded")
    c.expect("#570 bake failure: the cleared puppet is raised again",
             sorted(r["api"].jobs), ["pu-p-1"])

    # rc плейбука: подъём при любом исходе, объявление -- только после успеха.
    r = run_570(rows_570(("pu-p-1", "n1")), pool, bake_rc=3)
    c.expect("#570 bake rc: the puppet is raised and the build is not passed off as good",
             (r["err"], r["result"]["rc"], r["result"]["announced"],
              sorted(r["api"].jobs)),
             (None, 3, [], ["pu-p-1"]))

    # Отказ подъёма не затирает исходный отказ: видны оба.
    r = run_570(rows_570(("pu-p-1", "n1"), ("pu-p-2", "n2")), pool,
                wipe_refuses=("pu-p-2",), register_refuses=("pu-p-1",))
    c.check("#570 both the wipe refusal and the raising refusal are visible",
            isinstance(r["err"], RuntimeError)
            and "wipe of pu-p-2 on n2 refused" in str(r["err"])
            and "not raised again: Nomad refused pu-p-1" in str(r["err"]),
            str(r["err"]))
    c.expect("#570 the one whose raising worked is raised",
             sorted(r["api"].jobs), ["pu-p-2"])

    # Успех: прежний итог, мета -- прежняя.
    r = run_570(rows_570(("pu-p-1", "n1", "feat/a"), ("pu-p-2", "n2")), pool)
    c.expect("#570 a successful build: the result as before",
             (r["err"], r["result"]["rc"],
              [p["name"] for p in r["result"]["gone"]], r["result"]["announced"],
              r["baked"]),
             (None, 0, ["pu-p-1", "pu-p-2"],
              [("n1", "announced, serves p"), ("n2", "announced, serves p")], 1))
    c.expect("#570 a successful build: gone carries the meta",
             r["result"]["gone"][0],
             {"name": "pu-p-1", "origin": "git@h:g/p.git", "branch": "feat/a", "node": "n1"})

    # Занятый папет: отказ до первого останова, никого не подняли.
    busy = rows_570(("pu-p-1", "n1"), ("pu-p-2", "n2"))
    busy[1]["kind"] = "busy"
    r = run_570(busy, pool)
    c.check("#570 a busy puppet still refuses before the first stop",
            isinstance(r["err"], RuntimeError) and "pu-p-2" in str(r["err"])
            and "not free" in str(r["err"]), str(r["err"]))
    c.expect("#570 the refusal deregistered and raised no one",
             (r["api"].calls, r["steps"]), ([], ["stopping the project's bodies"]))


def main():
    c = Checks()
    check_node_check_321(c)
    check_clear_recovery_570(c)
    extra = image.extra_vars("git@h:g/proj.git", GOT)
    want = {"mop_project": "proj", "mop_origin": "git@h:g/proj.git",
            "mop_project_asks": {"MOP_MEM_MB": "2048"},
            "mop_project_tasks": "/tmp/x/sandbox-tasks.yml"}
    c.expect("extra_vars", extra, want)
    c.check("extra_vars: a None half must be absent, not null", "mop_project_vars" not in extra)
    # Пересборка — операция над ПРОЕКТОМ (#60): тела проекта на контейнерных
    # узлах сносятся до сборки (у шаблона с живым клоном не отнять место), и
    # занятый папет — отказ, если не сказано --force. Тела на узлах, где тело
    # равно узлу, сборка не касается вовсе.
    # HYPOTHESIS: plan_clear нет, сборка сносит образ, а папеты остаются жить
    # на старом. SOLUTION: чистый план из строк ростера. STATUS: FIXED — see #60
    rows = [
        {"name": "pu-proj-1", "node": "hyper", "container": True, "kind": "free"},
        {"name": "pu-proj-2", "node": "hyper", "container": True, "kind": "busy"},
        {"name": "pu-proj-3", "node": "mate", "container": False, "kind": "busy"},
        {"name": "pu-proj-4", "node": "hyper", "container": True, "kind": "silent"},
    ]
    try:
        try:
            image.plan_clear(rows)
            c.fail("plan_clear: a busy puppet on a container node must refuse")
        except RuntimeError as e:
            for who in ("pu-proj-2", "pu-proj-4"):
                c.check(f"plan_clear: the refusal must name {who}", who in str(e), e)
            c.check("plan_clear: the refusal names a puppet that does not block",
                    not ("pu-proj-3" in str(e) or "pu-proj-1" in str(e)), e)
        c.expect("plan_clear(force)", image.plan_clear(rows, force=True),
                 ["pu-proj-1", "pu-proj-2", "pu-proj-4"])
        c.expect("plan_clear(free only)", image.plan_clear(rows[:1]), ["pu-proj-1"])
    except AttributeError:
        c.fail("image.plan_clear is missing")
    # Сборка на одном узле (#280): на rumop образ на agent2 запечатался голым
    # (#278), а на agent1 папеты проекта заняты работой. Пересборка всего
    # проекта отбивалась бы по занятым (или --force убил бы их работу), хотя
    # образ agent1 цел. С названным узлом считаются только его тела: чужой
    # занятый папет не блокирует, снимаются только свои, плейбук получает
    # mop_node и играет только там.
    # HYPOTHESIS: plan_clear и bake не знают узла -- сборка всегда над всеми
    # контейнерными узлами. SOLUTION: node= у plan_clear/clear/bake/announce/
    # build, mop_node в extra-vars плейбука, end_host на чужих узлах.
    # STATUS: FIXED — see #280
    both = [
        {"name": "pu-proj-1", "node": "agent1", "container": True, "kind": "busy"},
        {"name": "pu-proj-2", "node": "agent2", "container": True, "kind": "failing"},
        {"name": "pu-proj-3", "node": "agent2", "container": True, "kind": "free"},
    ]
    try:
        try:
            c.expect("plan_clear(node): a busy puppet elsewhere does not block",
                     image.plan_clear(both, node="agent2", force=True),
                     ["pu-proj-2", "pu-proj-3"])
        except RuntimeError as e:
            c.fail(f"plan_clear(node): refused by another node's puppet: {e}")
        try:
            image.plan_clear(both, node="agent2")
            c.fail("plan_clear(node): a not-free puppet on the node must still refuse")
        except RuntimeError as e:
            c.check("plan_clear(node): the refusal names only the node's puppets",
                    "pu-proj-2" in str(e) and "pu-proj-1" not in str(e), e)
        c.expect("plan_clear(node): only the node's puppets are cleared",
                 image.plan_clear(both, node="agent1", force=True), ["pu-proj-1"])
        c.expect("plan_clear(no node): everyone, as before",
                 image.plan_clear(both, force=True),
                 ["pu-proj-1", "pu-proj-2", "pu-proj-3"])
    except TypeError as e:
        c.fail(f"plan_clear takes no node: {e}")
    try:
        c.check("play_extra: no node -> no mop_node",
                "mop_node" not in image.play_extra("git@h:g/proj.git", GOT, False, None))
        got = image.play_extra("git@h:g/proj.git", GOT, True, "agent2")
        c.expect("play_extra: node -> mop_node", got.get("mop_node"), "agent2")
        c.expect("play_extra: fresh still travels", got.get("mop_fresh"), True)
        c.expect("play_extra: the manifest half is extra_vars",
                 {k: v for k, v in got.items() if k not in ("mop_node", "mop_fresh")},
                 image.extra_vars("git@h:g/proj.git", GOT))
    except AttributeError:
        c.fail("image.play_extra is missing")
    return c.report("image")


if __name__ == "__main__":
    sys.exit(main())
