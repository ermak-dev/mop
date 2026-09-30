"""pool diagnostics: mop doctor [group] [--fix [--safe]]

Catches stuck puppets, a stale login, restart backoff, exhausted model quota,
and a job spec older than the node driver — that last one looks perfectly
healthy until the scheduler moves it to a hypervisor. The disk group asks each
node's agent what the disk watchdog would sweep there and who is under disk
pressure; --fix sweeps for real.

The checks come in groups, a module each in mop/client/doctor/: `mop doctor`
runs them all, `mop doctor <group>` runs one; a name that is not a group is
refused with the list of groups.

--fix treats what's treatable; a silent node agent is not on this list — the
puppet may well be working fine, and a restart would kill that work in the
clone. An expired login is treated without a restart: the puppet's lease is
pushed to it again from the server's credential registry and the puppet is
nudged to go on, so its conversation survives. A puppet without a lease is
not treated: give it one with mop update --cred.

--fix --safe treats only what cannot break work — login+nudge and the disk
sweep — and names the rest without executing it: restarts, alloc stops,
/model and spec updates stay the operator's call. The server runs it hourly
(mop-doctor.timer), its output goes to the journal.
"""
import sys

from mop.cli import lib
from mop.client import doctor
from mop.common.render import table


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "fix", "type": "boolean", "flag": "--fix", "help": "treat what is treatable"},
    {"name": "safe", "type": "boolean", "flag": "--safe",
     "help": "with fix: treat only what cannot break work (login+nudge, sweep)"}]}


def main(argv):
    try:
        chosen, fix, safe = doctor.select(list(doctor.groups()), argv)
    except ValueError as e:
        lib.usage(f"{e}\n\n{__doc__}")
    # Группы (#356): каждая проблема помнит свою группу -- её и лечит.
    found = []
    for name in chosen:
        check = doctor.module(name)
        found += [(check, issue) for issue in check.diagnose()]
    issues = [issue for _, issue in found]
    if not issues:
        print("pool is healthy: nothing stuck")
        return

    print("\n".join(table([
        (i["name"], doctor.where(i), i["diagnosis"],
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
    # Подготовка группы до лечения (раздача кредов перед login+nudge):
    # отказ любой из них останавливает --fix целиком, как и до шва.
    for name in chosen:
        check = doctor.module(name)
        refusal, lines = check.prepare([i for c, i in found if c is check])
        if refusal:
            sys.exit(refusal)
        for line in lines:
            print(line)
    for check, issue in found:
        if doctor.treats(issue, safe):
            print(f"  {issue['name']}: {check.treat(issue)}")
        elif issue["action"]:
            # --safe (#359): небезопасное названо, но не исполнено.
            print(f"  {issue['name']}: [{issue['action']}] not treated — "
                  f"--safe treats only {', '.join(doctor.SAFE)}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
