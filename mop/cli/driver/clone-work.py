"""mop driver clone-work <dir>: may a clone with no live session be removed — exit 0 yes, 3 no

Asked by the disk watchdog's host tier for each clone whose puppet has no
tmux session. The answer is the pool's one rule of work in a clone:
uncommitted, unpushed, or off its home branch holds work, and a clone that
cannot be read is kept. A directory without .git is no clone and may go.
Prints the reason on one line. Any exit other than 0 and 3 is a failed
check, not a verdict: the watchdog keeps the clone and says so.
"""
import asyncio
import os
import shlex

from mop.cli import lib
from mop import driver
from mop.cli.driver import sweep
from mop.node import agent

KEEP = 3
# Корни клонов host-узла -- те же, что CLONE_GLOBS сторожа: нынешний и два
# наследных, где ещё лежат клоны прежних пулов.
ROOTS = ("puppets", "slaves", "wk")


def refusal(path, home):
    """Путь клона -> None либо почему он не наш. Чистая функция.

    Путь приходит из скрипта, а не с шины, но проба исполняет в нём git:
    только абсолютный каталог прямо под одним из корней клонов дома, без
    `..` и без скрытых имён."""
    if not os.path.isabs(path) or os.path.normpath(path) != path.rstrip("/"):
        return f"{path}: not an absolute normalized path"
    parent, name = os.path.split(path.rstrip("/"))
    if parent not in {os.path.join(home, r) for r in ROOTS} or not name or name.startswith("."):
        return f"{path}: not a clone under ~/{{{','.join(ROOTS)}}}"
    return None


def verdict(clone, has_git):
    """Факты клона без сессии -> (можно ли сносить, причина). Чистая.

    Причины и правило -- sweep.plan, то же, что у контейнерного яруса."""
    action, why = sweep.plan(None, sweep.NO_SESSION, clone if has_git else None,
                             None if has_git else 1)
    return action == "destroy", why


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    path = argv[0]
    bad = refusal(path, os.path.expanduser("~"))
    if bad:
        lib.fail(bad)
        return 2
    has_git = os.path.isdir(os.path.join(path, ".git"))
    clone = None
    if has_git:
        out, _ = asyncio.run(driver.sh(agent.clone_probe(shlex.quote(path))))
        clone = agent.clone_facts_of(out)
    ok, why = verdict(clone, has_git)
    print(why)
    return 0 if ok else KEEP
