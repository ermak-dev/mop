#!/usr/bin/env python3
"""Структурный сторож границы мастера и MCP (#445)."""
import os
import re
import sys

import hermetic  # noqa: F401 -- настройки не с этой машины (#209)
from _lib import Checks

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SKILL = os.path.join(ROOT, "mop", "skills", "master", "SKILL.md")
sys.path.insert(0, ROOT)

from mop.common import state  # noqa: E402


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
    c.check("#560 claiming a ticket never switches the master's working copy",
            bool(re.search(r"claim[^.]*never switch", text, re.I)))


def check_tools(c, text):
    for tool in ("agents", "send", "tail", "slash", "add", "delete", "restart",
                 "node", "project_add", "landing", "profile"):
        c.check(f"#445 tool: {tool}", f"mcp__mop__{tool}" in text)
    # #515: группа dev не экспонируется в MCP вовсе -- скилл не учит
    # мастера звать dev_bug_*/dev_ci_*; трекер и CI идут из шелла
    # мастера (исключение #450), названным словами, без списка имён.
    c.check("#515 no developer tool may be taught to the master",
            "mcp__mop__dev_" not in text)
    # #560: прежний страж #515 требовал в тексте `mop dev bug` и
    # `mop dev ci` -- это трекер и CI самого проекта mop. Скилл общий для
    # любого проекта пула: CLI проекта в нём не называется вовсе, процесс
    # поставки мастер берёт из правил проекта или у оператора.
    c.check("#560 the skill names no project's own development CLI",
            "mop dev" not in text)
    for stale in ("puppet(action=", "doctor(", "mop attach", "mop secret",
                  "mop server deploy", "mop server doctor", "mop mcp --check"):
        c.check(f"#445 no obsolete master operation: {stale}", stale not in text)


# HYPOTHESIS (#560): скилл писался на проекте mop и выдал его трекер, CI и
# правила (gate #544, язык тикетов, готовность эпика) за общий протокол:
# мастер другого проекта собирал бы очередь из трекера mop.
# SOLUTION: очередь, трекер, CI, раскатка и закрытие -- по процессу
# поставки из правил проекта или по запросам оператора; специфика mop --
# в CLAUDE.md mop, не в скилле.
# RESULT: RED на скилле a1d152f, GREEN после обобщения.
# STATUS: FIXED — see #560
def check_project_process(c, text):
    text = " ".join(text.split())
    c.check("#560 the queue comes from the project's delivery rules or the operator",
            bool(re.search(r"queue[^.]*(?:project's rules|delivery process)[^.]*operator",
                           text, re.I)))
    c.check("#560 no rule of one project is spelled as the skill's own",
            not re.search(r"\bfor mop\b|\bin mop\b", text, re.I))
    c.check("#560 no tracker issue of one project is cited", not re.search(r"#\d+", text))


# HYPOTHESIS (#561): таблица состояний скилла поплыла от state.py: в ней
# было несуществующее `not logged in` и не было `starting`, `FAILED: …`,
# `HUNG (no answer)` -- мастер не узнавал то, что показывает состав пула.
# SOLUTION: каждый вид из state._SHOW (кроме «other» -- незнакомый статус,
# показанный как есть) обязан быть назван в скилле своим началом строки;
# вид login строит единственный источник -- «login expired».
# RESULT: RED на скилле 1054724, GREEN после сокращения и сверки.
# STATUS: FIXED — see #561
def check_states(c, text):
    mark, branch = "\x00", "\x01"
    for kind, show in state._SHOW.items():
        if kind == "other":
            continue
        shown = "login expired" if kind == "login" else show(mark, branch)
        head = shown.split(mark)[0].split(branch)[0].rstrip(" (:")
        c.check(f"#561 the skill names the {kind!r} roster state as `{head}`",
                f"`{head}" in text, head)
    c.check("#561 no roster state the code cannot show", "not logged in" not in text)


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
    check_project_process(c, text)
    check_states(c, text)
    check_landing(c, text)
    return c.report("master_skill")


if __name__ == "__main__":
    sys.exit(main())
