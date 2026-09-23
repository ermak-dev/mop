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
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import puppets  # noqa: E402

ORIGIN = "git@git.example.dev:someone/mop.git"


def queued_job():
    return {"ID": "pu-mop-1", "Meta": {"origin": ORIGIN},
            "JobSummary": {"Summary": {"puppets": {"Queued": 1}}}}


def node(meta, status="ready", eligible=True):
    return {"status": status, "eligible": eligible, "meta": meta}


def main():
    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if got != want:
            bad += 1
            print(f"FAILED  {what}: got {got!r}, want {want!r}")

    # Узлы, которые проект обслуживают, — тем же выражением, что и в Nomad.
    check("pve node with the image", puppets.unserved("mop", [node({"mop_projects": "rugent,mop"})]), False)
    check("host node serves any", puppets.unserved("mop", [node({"mop_projects": "any"})]), False)
    # Свидетельство #118: pve-узлы без образа, SERVES `-`.
    check("pve nodes without images", puppets.unserved("mop", [node({}), node({"mop_projects": ""})]), True)
    check("other project's image only", puppets.unserved("mop", [node({"mop_projects": "mop2"})]), True)
    # Узел с образом, на который Nomad не ставит, — не спасает.
    check("serving node is down", puppets.unserved("mop", [node({"mop_projects": "mop"}, status="down")]), True)
    check("serving node is closed", puppets.unserved("mop", [node({"mop_projects": "mop"}, eligible=False)]), True)
    check("no nodes at all", puppets.unserved("mop", []), True)

    # Диагноз: без узла — про образ, с узлом — про слоты.
    d = puppets._placement_issue(queued_job(), None, unserved=True)["diagnosis"]
    check("unserved diagnosis names the image", "no free slots" not in d and "image" in d and "mop" in d, True)
    check("unserved diagnosis names the cure", "mop project add" in d, True)
    d = puppets._placement_issue(queued_job(), None, unserved=False)["diagnosis"]
    check("served but queued is about slots", d, "queued — no free slots in the pool")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
