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
    c.check("#445 master claim without branch", "no_branch=true" in text)
    c.check("#445 missing tools escalated rather than bypassed",
            bool(re.search(r"missing.*tool.*(?:operator|stop)", text, re.I | re.S)))


def check_tools(c, text):
    for tool in ("agents", "send", "tail", "slash", "add", "delete", "restart",
                 "node", "project_add", "landing", "profile",
                 "dev_bug_list", "dev_bug_show", "dev_bug_epic", "dev_bug_labels",
                 "dev_bug_new", "dev_bug_edit", "dev_bug_comment", "dev_bug_start",
                 "dev_bug_close", "dev_bug_relabel", "dev_bug_evidence",
                 "dev_ci_list", "dev_ci_show", "dev_ci_why", "dev_ci_log", "dev_ci_retry"):
        c.check(f"#445 tool: {tool}", f"mcp__mop__{tool}" in text)
    for stale in ("puppet(action=", "doctor(", "mop attach", "mop secret",
                  "mop server deploy", "mop server doctor", "mop mcp --check",
                  "mop dev bug", "mop dev ci"):
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
