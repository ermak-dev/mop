#!/usr/bin/env python3
"""Структурный сторож границы мастера и MCP (#445)."""
import os
import re
import sys

from _lib import Checks

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SKILL = os.path.join(ROOT, "mop", "skills", "master", "SKILL.md")


# HYPOTHESIS: прежний скилл учит мастера управлять пулом и трекером из shell,
# ссылается на исчезнувшие инструменты и пропускает полный merged-tree gate.
# SOLUTION: один канал MCP для операций мастера, профиль и реальные имена,
# исключение лишь для чтения файлов; merge и git оставлены исполнителю.
# RESULT: мастер не сможет применить shell как запасной путь при нехватке MCP.
# STATUS: FIXED — see #445

def check_control_boundary(c, text):
    c.check("#445 only MCP for master control", bool(re.search(
        r"master.*(?:pool|tracker|CI).*exclusively.*MCP", text, re.I | re.S)))
    for forbidden in ("Bash", "shell", "CLI `mop`", "Git", "HTTP", "fallback"):
        c.check(f"#445 explicit prohibition: {forbidden}", forbidden in text)
    c.check("#445 ordinary read-only file inspection permitted",
            "read-only" in text and "file" in text)
    c.check("#445 profile gate", "mcp__mop__profile" in text)
    c.check("#445 master claim without branch", "--no-branch" in text)
    c.check("#445 missing tools escalated rather than bypassed",
            bool(re.search(r"missing.*tool.*(?:operator|stop)", text, re.I | re.S)))


def check_tools(c, text):
    for tool in ("agents", "send", "tail", "slash", "add", "delete", "restart",
                 "node", "project_add", "landing", "profile"):
        c.check(f"#445 tool: {tool}", f"mcp__mop__{tool}" in text)
    # #515: группа dev не экспонируется в MCP вовсе -- скилл не учит
    # мастера звать dev_bug_*/dev_ci_*; трекер и CI идут из шелла
    # мастера (исключение #450), названным словами, без списка имён.
    c.check("#515 no developer tool may be taught to the master",
            "mcp__mop__dev_" not in text)
    c.check("#515 tracker and CI stay on the master's CLI (#450)",
            "mop dev bug" in text and "mop dev ci" in text)
    for stale in ("puppet(action=", "doctor(", "mop attach", "mop secret",
                  "mop server deploy", "mop server doctor", "mop mcp --check"):
        c.check(f"#445 no obsolete master operation: {stale}", stale not in text)


def check_landing(c, text):
    c.check("#445 integrated full suite takes project-rule precedence",
            bool(re.search(r"project.*rules.*(?:full|suite|test).*merged|"
                           r"(?:full|suite|test).*merged.*project.*rules", text, re.I | re.S)))
    c.check("#445 puppet does Git merge and push", "puppet" in text and
            "merge --no-ff" in text and "push" in text)
    c.check("#445 landing token survives", "mcp__mop__landing" in text and
            "take" in text and "give" in text)


def main():
    with open(SKILL, encoding="utf-8") as f:
        text = f.read()
    c = Checks()
    check_control_boundary(c, text)
    check_tools(c, text)
    check_landing(c, text)
    return c.report("master_skill")


if __name__ == "__main__":
    sys.exit(main())
