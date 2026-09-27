"""mop driver sweep [--dry]: destroy bodies with no puppet left in them

The disk watchdog's tier for a node whose bodies are objects of their own.
Runs on the node, knows nothing of Nomad. A body is destroyed only when its
tmux answers with no session and its clone holds no work (or there is no
clone at all); a body whose clone holds work, whose clone cannot be read, or
which does not answer is kept and named. Refuses when no body at all has a
session.
"""
import asyncio
import sys

from mop.cli import lib
from mop import driver
from mop.common.domain import CloneFacts, Gone, holds_work
from mop.node import agent

# tmux has-session: 0 -- сессия есть, 1 -- нет (tmux ответил). Прочее --
# ssh не дошёл (255) или тело молчит (None, таймаут): о сессии не известно.
NO_SESSION = 1


def plan(name, tmux_code, clone, clone_dir_code):
    """Факты тела без живой сессии -> ("destroy" | "keep", причина).
    Чистая функция.

    tmux_code -- код has-session; clone -- CloneFacts.to_dict() либо None
    (проба клона не ответила числами); clone_dir_code -- код `test -d` на
    каталог клона, спрошенный только когда clone None: 1 -- каталога нет.

    Правило работы одно на всех -- domain.holds_work (#266): несохранённое,
    неотправленное, клон не на своём доме. Нет сессии -- ещё не «нечего
    терять»: папет на подъёме и папет с падающим bootstrap'ом живут без
    tmux часами (#346). «Не знаю» -- оставить: снесённая работа не
    вернётся, а сироту уберёт следующий прогон."""
    if tmux_code != NO_SESSION:
        why = "timeout" if tmux_code is None else f"exit {tmux_code}"
        return "keep", f"body unreachable (tmux check: {why})"
    if clone is None:
        # Каталога клона нет -- работы нет по определению: тело, где клон не
        # встал, иначе копилось бы вечно. Не прочитался -- не знаем.
        if clone_dir_code == 1:
            return "destroy", "no session, no clone"
        return "keep", "no session, clone unreadable"
    facts = CloneFacts.from_dict(clone)
    if holds_work(facts):
        return "keep", f"no session, clone holds work ({', '.join(facts.work())})"
    return "destroy", "no session, clean clone"


def main(argv):
    """Собрать брошенные тела: [--dry].

    Ярус сторожа диска для узла, чьи тела — отдельные объекты. Предохранитель
    тот же, что у tier 1 в pu-sweep: если ни у одного тела нет сессии, это не
    куча сирот, а недоступный tmux — узел только что поднялся либо тела не
    отвечают. Снести все тела узла никогда не бывает правильным ответом на
    такое. Что сносить из остальных, решает plan."""
    dry = lib.dry(argv, __doc__)
    d = driver.current()
    if not driver.is_container(driver.current_name()):
        print("bodies here are the node itself — the disk watchdog sweeps clones")
        return 0

    async def survey():
        names = await d.bodies()
        codes = []
        for n in names:
            _, code = await driver.sh(driver.Tmux(n).alive(), 20, d.argv(n))
            codes.append(code)
        return names, codes

    async def facts(name, code):
        """Клон спрашиваем только у тела, чей tmux ответил «сессии нет»:
        живому не нужно, молчащее не ответит (#346)."""
        if code != NO_SESSION:
            return None, None
        clone = await agent.clone_facts(name)
        if clone is not None:
            return clone, None
        _, dir_code = await driver.sh(f"test -d {driver.clone_dir(name)}", 20, d.argv(name))
        return None, dir_code

    names, codes = asyncio.run(survey())
    if not names:
        print("no bodies on this machine")
        return 0
    live = sum(code == 0 for code in codes)
    print(f"bodies: {live}/{len(names)} with a live session")
    if live == 0:
        print("  ! 0 live sessions — tmux unreachable, not "
              f"{len(names)} orphans; refusing to sweep", file=sys.stderr)
        return 1
    gone = 0
    for name, code in zip(names, codes):
        if code == 0:
            continue
        action, why = plan(name, code, *asyncio.run(facts(name, code)))
        if action == "keep":
            print(f"  kept {name}: {why}")
            continue
        if dry:
            print(f"  would destroy {name}: {why}")
            continue
        r = asyncio.run(d.destroy(name))
        done = Gone.from_dict(r)
        print(f"  {name}: {'destroyed' if done else r.get('error')}")
        gone += 1 if done else 0
    if not dry:
        print(f"{gone} orphaned bod{'y' if gone == 1 else 'ies'} destroyed")
    return 0
