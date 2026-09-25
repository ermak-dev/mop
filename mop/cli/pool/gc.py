"""recycle free puppets on nodes low on space: mop gc [--dry]

A node with less than MOP_GC_FREE_MIN_GB free gets its free puppets
recycled (clone reset to HEAD, target wiped) — no more than
MOP_GC_MAX_PER_RUN per run. Run this as the operator, not from a master
shell: disk pressure on a node is a fact about every tenant, not one project.
"""
from mop.cli import lib
from mop.common import bus, config, puppets, state


def main(argv):
    dry = lib.dry(argv, __doc__)
    limit = config.num("MOP_GC_FREE_MIN_GB")
    cap = config.num("MOP_GC_MAX_PER_RUN")
    rows = puppets.puppet_rows()
    nodes = sorted({r.node for r in rows if r.node != "-"})
    disks = bus.request_many({n: {"verb": "disk"} for n in nodes})

    # Давление и кандидаты: узлы от самого тесного, внутри узла — от самого
    # жирного (место папета уже спрослено глаголом sizes, дополнительных
    # поездок сортировка не стоит).
    plan = []
    for n in nodes:
        d = disks.get(n)
        why = bus.failure(d)
        if why:
            print(f"  {n}: free space unknown — {why}")
            continue
        if d["free_gb"] >= limit:
            continue
        cands = [r for r in rows if r.node == n and state.is_free(r.kind)]
        if not cands:
            print(f"  {n}: {d['free_gb']} GB free < {limit}, "
                  f"but no free puppets")
            continue
        names = [r.name for r in sorted(cands, key=lambda r: -(r.disk_kb or 0))]
        plan.append((d["free_gb"], n, names))
    if not plan:
        print(f"no pressure: every node with puppets has at least {limit} GB free")
        return

    done = 0
    for free_gb, n, names in sorted(plan):
        for name in names:
            if done >= cap:
                return
            print(f"{n}: {free_gb} GB free < {limit} — recycling {name}"
                  + (" [dry]" if dry else ""))
            if not dry:
                puppets.recycle(name)
            done += 1



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
