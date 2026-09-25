#!/usr/bin/env python3
"""Диагноз неразмещённого папета без пула: python3 tests/placement.py

Джоб без аллокации в Nomad значит одно из двух: мест нет, или ни один узел
не обслуживает проект — на pve-узле папет садится только туда, где собран
образ его проекта (ограничение `${meta.mop_projects}`, #10). Второе не
лечится ожиданием, сколько бы слотов ни было свободно, а `mop doctor`
называл его первым (#118).

HYPOTHESIS: `_placement_issue` видит только счётчик Queued и не знает, есть ли
у проекта узел вообще; ограничение размещения в диагностике не учтено.
SOLUTION: сервис кластера помечает джоб в очереди `unserved`, если ни один
готовый узел пула не обслуживает его проект (та же регулярка, что уезжает в
Nomad), а диагноз читает пометку.
RESULT: 10/10.
STATUS: FIXED — see #118

Потолок памяти (#197): спека несёт ограничение `${meta.mop_mem_cap_mb} >=
потолок папета`, и очередь по нему -- ни образ, ни слоты. placement_gap
говорит, какое из ограничений держит, сравнивая как Nomad (численно).
STATUS: FIXED — see #197
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import puppets  # noqa: E402
from mop.server import spec  # noqa: E402

ORIGIN = "git@git.example.dev:someone/mop.git"


def queued_job():
    return {"ID": "pu-mop-1", "Meta": {"origin": ORIGIN},
            "JobSummary": {"Summary": {"puppets": {"Queued": 1}}}}


def node(meta, status="ready", eligible=True):
    return {"status": status, "eligible": eligible, "meta": meta}


def main():
    c = Checks()

    # Узлы, которые проект обслуживают, — тем же выражением, что и в Nomad.
    c.expect("pve node with the image", spec.unserved("mop", [node({"mop_projects": "rugent,mop"})]), False)
    c.expect("host node serves any", spec.unserved("mop", [node({"mop_projects": "any"})]), False)
    # Свидетельство #118: pve-узлы без образа, SERVES `-`.
    c.expect("pve nodes without images", spec.unserved("mop", [node({}), node({"mop_projects": ""})]), True)
    c.expect("other project's image only", spec.unserved("mop", [node({"mop_projects": "mop2"})]), True)
    # Узел с образом, на который Nomad не ставит, — не спасает.
    c.expect("serving node is down", spec.unserved("mop", [node({"mop_projects": "mop"}, status="down")]), True)
    c.expect("serving node is closed", spec.unserved("mop", [node({"mop_projects": "mop"}, eligible=False)]), True)
    c.expect("no nodes at all", spec.unserved("mop", []), True)

    # Диагноз: без узла — про образ, с узлом — про слоты.
    d = puppets._placement_issue(queued_job(), None, unserved=True)["diagnosis"]
    c.expect("unserved diagnosis names the image", "no free slots" not in d and "image" in d and "mop" in d, True)
    c.expect("unserved diagnosis names the cure", "mop project add" in d, True)
    d = puppets._placement_issue(queued_job(), None, unserved=False)["diagnosis"]
    c.expect("served but queued is about slots", d, "queued — no free slots in the pool")

    # ── #197: потолок узла против потолка папета ─────────────────────────
    # Спека несёт ограничение `${meta.mop_mem_cap_mb} >= потолок`; очередь,
    # которую держит оно, -- не образ и не слоты, и диагноз обязан это сказать.
    both = {"mop_projects": "mop"}
    c.expect("cap above the ceiling", spec.placement_gap("mop", [node({**both, "mop_mem_cap_mb": "32768"})], 16384), False)
    c.expect("cap equal to the ceiling", spec.placement_gap("mop", [node({**both, "mop_mem_cap_mb": "16384"})], 16384), False)
    c.expect("cap below the ceiling", spec.placement_gap("mop", [node({**both, "mop_mem_cap_mb": "8192"})], 16384), "memory")
    # Численно, как Nomad: лексически "9000" >= "12288".
    c.expect("cap compared as a number", spec.placement_gap("mop", [node({**both, "mop_mem_cap_mb": "9000"})], 12288), "memory")
    # Узел без ключа ограничение не проходит (Nomad: lFound=false).
    c.expect("serving node without a cap", spec.placement_gap("mop", [node(both)], 8192), "memory")
    c.expect("one of the serving nodes is big enough",
          spec.placement_gap("mop", [node({**both, "mop_mem_cap_mb": "4096"}),
                                node({**both, "mop_mem_cap_mb": "65536"})], 16384), False)
    # Большой потолок у узла, который проекта не обслуживает, не в счёт.
    c.expect("the big node has no image",
          spec.placement_gap("mop", [node({"mop_projects": "rugent", "mop_mem_cap_mb": "65536"}),
                                node({**both, "mop_mem_cap_mb": "4096"})], 16384), "memory")
    c.expect("unserved stays a yes/no over both", spec.unserved("mop", [node({**both, "mop_mem_cap_mb": "8192"})], 16384), True)
    c.expect("no image still reads as the image", spec.placement_gap("mop", [node({"mop_mem_cap_mb": "65536"})], 16384), "image")
    # Спека до #197 потолка узла не спрашивает.
    c.expect("a spec without a ceiling", spec.placement_gap("mop", [node(both)], None), False)
    d = puppets._placement_issue(queued_job(), None, unserved="memory", ceiling=16384)["diagnosis"]
    c.expect(f"memory diagnosis names the ceiling and the node's cap ({d})",
          "16384" in d and "mop_mem_cap_mb" in d and "image" not in d and "no free slots" not in d, True)
    c.expect(f"memory diagnosis names the project's ask ({d})", "MOP_MEM_MB" in d and "mop" in d, True)

    return c.report("placement")


if __name__ == "__main__":
    sys.exit(main())
