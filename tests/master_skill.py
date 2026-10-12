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

# HYPOTHESIS (#559): граница «только MCP» из #445 мешает мастеру делать его
# работу: проверять ветки git, сливать собственную правку, проверять живую
# установку. Пользователь: мастер выполняет любые нужные ему команды.
# SOLUTION: смена контракта #445 -- запреты shell/Git/CLI и «стоп при
# нехватке инструмента» заменены явной границей: команды любые, MCP --
# предпочтительный путь для операций с пулом, действия масштаба оператора --
# по его явному указанию, секреты не печатаются. Остальные стражи #445
# (профиль, --no-branch, инструменты, gate проекта, токен) не тронуты.
# RESULT: RED на прежнем скилле (запреты на месте, новой границы нет),
# GREEN после правки.
# STATUS: FIXED — see #559
def check_control_boundary(c, text):
    # Фразы сверяются по тексту со схлопнутыми пробелами: перенос строки
    # посреди запрета не должен делать его «исчезнувшим».
    text = " ".join(text.split())
    c.check("#559 the master runs whatever commands it needs",
            "runs whatever commands it needs" in text)
    for gone in ("exclusively through mop MCP", "never merges, checks, commits",
                 "Do not invent a substitute", "not even for a read"):
        c.check(f"#559 the MCP-only prohibition is gone: {gone!r}", gone not in text)
    c.check("#559 MCP stays the preferred path for pool operations", bool(re.search(
        r"MCP[^.]*preferred[^.]*pool|pool[^.]*MCP[^.]*preferred", text, re.I)))
    c.check("#559 a missing or failing MCP tool falls back to the same mop command",
            bool(re.search(r"missing or (?:fails|failing)[^.]*`mop", text, re.I)))
    c.check("#559 operator-scale actions wait for the operator's explicit direction",
            "operator's explicit direction" in text)
    c.check("#559 secrets are never printed", bool(re.search(
        r"never (?:print|echo)[^.]*(?:secret|key|\.env)", text, re.I)))
    c.check("#445 profile gate", "mcp__mop__profile" in text)
    c.check("#445 master claim without branch", "--no-branch" in text)


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
    text = " ".join(text.split())
    c.check("#445 integrated full suite takes project-rule precedence",
            bool(re.search(r"project.*rules.*(?:full|suite|test).*merged|"
                           r"(?:full|suite|test).*merged.*project.*rules", text, re.I | re.S)))
    c.check("#445 puppet does Git merge and push", "puppet" in text and
            "merge --no-ff" in text and "push" in text)
    c.check("#559 the master may land its own change or on the operator's word",
            bool(re.search(r"master[^.]*lands? [^.]*(?:own|itself)[^.]*operator", text, re.I)))
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
