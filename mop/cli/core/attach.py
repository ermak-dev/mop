"""attach to a puppet's tmux session: mop attach <name>

The only subcommand that goes over ssh instead of the bus: a live terminal
isn't something a bus verb can hand over.
"""
import os
import shlex

from mop.cli import lib
from mop import driver
from mop.common import puppets
from mop.common.domain import Alloc


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    a, node_driver = puppets.running(name)
    node = Alloc.from_dict(a).node
    # Чем входят в тело, знает драйвер узла: у host это сразу tmux, у
    # контейнерного — ещё один ssh внутрь. Драйвер приезжает вместе с
    # аллокацией (глагол `alloc`), вторым запросом за ним не ходим; узел,
    # ничего о драйвере не сказавший, ведёт себя как раньше.
    d = driver.module(driver.of_node({"mop_driver": node_driver}, node))
    inside = " ".join(shlex.quote(x) for x in d.attach_argv(name))
    # Имя узла в Nomad — имя из инвентаря, а инвентарь берёт способ дозвона
    # из ~/.ssh/config по тому же имени. Таблицы имён между ними нет и не нужно.
    os.execvp("ssh", ["ssh", "-t", node, inside])



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
