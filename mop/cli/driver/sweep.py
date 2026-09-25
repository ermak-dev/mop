"""mop driver sweep [--dry]: destroy bodies with no puppet left in them

The disk watchdog's tier for a node whose bodies are objects of their own.
Runs on the node, knows nothing of Nomad, judges by the absence of a tmux
session — and refuses when no body at all has one.
"""
import asyncio
import sys

from mop.cli import lib
from mop import driver

def main(argv):
    """Собрать брошенные тела: [--dry].

    Ярус сторожа диска для узла, чьи тела — отдельные объекты. Признак тот же,
    на котором стоит tier 1 в pu-sweep: нет живой tmux-сессии с именем папета.
    И предохранитель тот же, по той же причине: если ни у одного тела нет
    сессии, это не куча сирот, а недоступный tmux — узел только что поднялся
    либо тела не отвечают. Снести все тела узла никогда не бывает правильным
    ответом на такое.

    Работы в клоне не спрашиваем и спрашивать не можем: тело, до которого не
    достучаться, о своём клоне ничего не скажет. Поэтому молчащее тело
    оставляем — сироту уберёт следующий прогон, а снесённая работа не
    вернётся."""
    dry = lib.dry(argv, __doc__)
    d = driver.current()
    if not driver.is_container(driver.current_name()):
        print("bodies here are the node itself — the disk watchdog sweeps clones")
        return 0

    async def survey():
        names = await d.bodies()
        alive = []
        for n in names:
            _, code = await driver.sh(
                f"tmux -L {n} has-session -t {n} 2>/dev/null", 20, d.argv(n))
            alive.append(code == 0)
        return names, alive

    names, alive = asyncio.run(survey())
    if not names:
        print("no bodies on this machine")
        return 0
    live = sum(alive)
    print(f"bodies: {live}/{len(names)} with a live session")
    if live == 0:
        print("  ! 0 live sessions — tmux unreachable, not "
              f"{len(names)} orphans; refusing to sweep", file=sys.stderr)
        return 1
    gone = 0
    for name, ok in zip(names, alive):
        if ok:
            continue
        if dry:
            print(f"  would destroy {name}: no session")
            continue
        r = asyncio.run(d.destroy(name))
        print(f"  {name}: {r.get('error') or 'destroyed'}")
        gone += 0 if r.get("error") else 1
    if not dry:
        print(f"{gone} orphaned bod{'y' if gone == 1 else 'ies'} destroyed")
    return 0
