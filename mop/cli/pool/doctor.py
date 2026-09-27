"""pool diagnostics: mop doctor [--fix]

Catches stuck puppets, a stale login, restart backoff, exhausted model quota,
and a job spec older than the node driver — that last one looks perfectly
healthy until the scheduler moves it to a hypervisor.

--fix treats what's treatable; a silent node agent is not on this list — the
puppet may well be working fine, and a restart would kill that work in the
clone. An expired login is treated without a restart: fresh credentials are
handed out and the puppet is nudged to go on, so its conversation survives.
"""
import sys

from mop.cli import lib
from mop.client import keys
from mop.common import puppets
from mop.common.render import table


def _login():
    """Раздача кредов перед лечением. Зовём библиотеку, а не соседний
    командлет: командлет разбирает аргументы и печатает, оболочкой друг для
    друга они быть не должны. Показываем только узлы, куда креды не доехали
    (#159): успех раздачи виден по лечению ниже, а не отчётом о каждом узле."""
    results, what, _ = keys.push_login()
    for node in sorted(results):
        if results[node] != "OK":
            print(f"  {node}: {' + '.join(what)} {results[node]}")


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "fix", "type": "boolean", "flag": "--fix", "help": "treat what is treatable"}]}


def main(argv):
    fix = "--fix" in argv
    if set(argv) - {"--fix"}:
        lib.usage(__doc__)
    issues = puppets.diagnose()
    if not issues:
        print("pool is healthy: nothing stuck")
        return

    print("\n".join(table([
        (i["name"], i["alloc"]["NodeName"] if i["alloc"] else "-", i["diagnosis"],
         f"[{i['action']}]" if i["action"] and not fix else "")
        for i in issues])))
    if not fix:
        # Лечение уже названо в колонке ([restart], [model]…); совета «run
        # --fix» под таблицей нет (#159). Нечего лечить — это и говорим: у
        # «queued» и отказов лечения здесь нет, решает оператор.
        if not any(i["action"] for i in issues):
            print("\nno auto-treatment — operator's call")
        return

    print()
    if any(i["action"] == "login+nudge" for i in issues):
        if not keys.credentials_fresh():
            sys.exit("local credentials are stale or broken — log in to claude "
                     "on this machine first, then mop doctor --fix")
        _login()
    for issue in issues:
        if issue["action"]:
            print(f"  {issue['name']}: {puppets.treat(issue)}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
