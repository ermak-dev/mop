"""garbage on the pool's nodes: mop server sweep [--dry]

Destroys orphan bodies — those no job names any more — and only lists
build bodies: a running sealed image is either a live build or a broken
one, and they look the same from here. --dry lists without destroying.
Run on the server, not a master shell: what stands on a node is every
tenant's fact. `mop driver sweep` is the node half: it judges by the
absence of a tmux session.
"""
import sys

from mop.cli import lib
from mop.cli.server import _maintenance
from mop.common import bus, puppets
from mop.common.render import table


def main(argv):
    dry = lib.dry(argv, __doc__)

    nodes = sorted(puppets.ready_nodes())
    if not nodes:
        # Отказ, а не «чисто» (#179): осмотра не было.
        print("no ready nodes", file=sys.stderr)
        return 1
    answers = bus.request_many("junk", nodes)

    # Узел, который не ответил, — не пустой узел. Промолчавший считался бы
    # «без тел», и его сироты уехали бы из отчёта молча, а молчание здесь
    # означает ровно противоположное: мы про него ничего не знаем.
    silent = {n: bus.failure(answers.get(n)) for n in nodes}
    silent = {n: why for n, why in silent.items() if why}
    for n, why in silent.items():
        print(f"  ! {n}: {why} — not swept, its bodies are unknown", file=sys.stderr)

    # Все джобы пула, а не только своего проекта: сирота чужого проекта — такой
    # же занятый контейнер, и «не мой» не делает его чьим-то.
    heard = [n for n in nodes if n not in silent]
    if not heard:
        # Ни один не ответил — это НЕ «чисто». Печатать здесь «нечего
        # убирать» значило бы отчитаться об осмотре, которого не было.
        print("no node answered — nothing was examined, let alone swept", file=sys.stderr)
        return 1

    # Пустой список джобов — это не «весь пул мусор», это Nomad, который
    # ничего не сказал. Снос по такому признаку был бы худшим из возможных
    # исходов, и предохранитель тут того же рода, что у `mop driver sweep`:
    # признак, верный поодиночке, перестаёт им быть, когда совпадает у всех.
    known = {j["ID"] for j in puppets.jobs()}
    if not known:
        print("Nomad lists no puppets at all — that reads as 'every body is "
              "garbage', which is never the right answer; refusing to sweep",
              file=sys.stderr)
        return 1

    rows = puppets.classify_junk({n: answers[n] for n in heard}, known)
    if not rows:
        # Нечего убирать -- успех, и он молчит (#159). Промолчавшие узлы уже
        # названы выше, и код выхода за них отвечает.
        return 1 if silent else 0

    out = [("NODE", "KIND", "NAME", "WHY")]
    for r in rows:
        out.append((r["node"], r["kind"], r["name"], r["detail"]))
    print("\n".join(table(out)))

    doomed = [r for r in rows if r["sweepable"]]
    if not doomed:
        print("\nnothing here is safe to destroy without a human")
        return 1 if silent else 0
    if dry:
        for r in doomed:
            print(f"  would destroy {r['name']} on {r['node']}")
        return 0

    gone = 0
    for r in doomed:
        a = bus.request(r["node"], "wipe", name=r["name"], timeout=puppets.WIPE_TIMEOUT)
        why = a.get("error")
        print(f"  {r['name']}: {why or 'destroyed'}")
        gone += 0 if why else 1
    print(f"{gone} of {len(doomed)} destroyed")
    return 1 if silent or gone != len(doomed) else 0



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(_maintenance.only_server(main))
