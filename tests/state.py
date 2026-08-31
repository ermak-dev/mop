#!/usr/bin/env python3
"""Проверка puppet_state без пула: python3 tests/state.py

Пока состояние собиралось поверх alloc exec, проверить его можно было только
на живом папете — и регрессия однажды спряталась именно здесь: пробник сессии
молча падал в откат по ветке, а `mop list` выглядел исправным. С переездом на
шину агент отдаёт ФАКТЫ, а вердикт собирается чистой функцией, поэтому вся
матрица проверяется здесь.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле, и тестов на него нет.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.puppets import is_free, puppet_state  # noqa: E402

CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
WORK = {"cur": "bug/1063", "def": "master", "dirty": 0, "ahead": 0}


# Правило «free по префиксу» стоит под выбором жертв в mop gc: снести
# чужую работу из-за точного сравнения со «free» — дорогая опечатка.
FREE_CASES = [
    ("free", True),
    ("free (master)", True),
    ("free (bug/1063)", True),
    ("busy", False),
    ("idle: bug/1063 (uncommitted: 3)", False),
    ("HUNG (not responding)", False),
    ("AGENT SILENT (node agent node1 silent for 20s)", False),
    ("needs action", False),
]


def facts(session, clone=CLEAN, screen="Herding bytes"):
    return {"present": True, "screen": screen, "session": session, "clone": clone}


CASES = [
    # (что случилось, факты, ожидаемое состояние)
    ("node doesn't recognize the puppet as its own",
     {"present": False}, "HUNG (no tmux session)"),
    ("agent returned an error",
     {"error": "tmux not responding"}, "HUNG (tmux not responding)"),
    ("empty pane — puppet just came up",
     facts("idle 1 1", screen="   \n\n"), "free"),

    # Сокет = живость, файл = активность. Сочетания разбираются здесь.
    ("session alive and working", facts("busy 1 1", WORK), "busy: bug/1063"),
    ("working on the default branch", facts("busy 1 1", CLEAN), "busy"),
    ("process alive, but the socket is silent", facts("hung 1 0"), "HUNG (not responding)"),
    ("process dead, file stale", facts("idle 0 0"), "HUNG (not responding)"),
    ("stuck on an action request",
     facts("requires_action 1 1"), "needs action"),
    ("waiting for input — leave it alone", facts("waiting 1 1"), "waiting for input"),

    # Свободен = в клоне нечего терять. На этом стоит решение о диспатче.
    ("clean and everything's on origin", facts("idle 1 1", CLEAN), "free (master)"),
    ("foreign branch, but everything's pushed",
     facts("idle 1 1", WORK), "free (bug/1063)"),
    ("uncommitted files",
     facts("idle 1 1", {**WORK, "dirty": 3}), "idle: bug/1063 (uncommitted: 3)"),
    ("unpushed commits",
     facts("idle 1 1", {**WORK, "ahead": 2}), "idle: bug/1063 (unpushed: 2)"),
    ("both at once — two numbers, not a sum",
     facts("idle 1 1", {**WORK, "dirty": 3, "ahead": 2}),
     "idle: bug/1063 (uncommitted: 3, unpushed: 2)"),

    # Жалобы видны только на экране: сессия жива и отвечает, а ход выдать не может.
    ("login expired",
     facts("idle 1 1", WORK, "Login expired · Please run /login"),
     "login expired: bug/1063"),
    ("not logged in at all",
     facts("idle 1 1", CLEAN, "Not logged in · Run /login"), "not logged in"),
    # Подъём истории спрашивает, чем поднимать длинную сессию. Файл сессии при
    # этом здоров, и без экрана папет читался бы свободным.
    ("stuck on the choice of how to resume history",
     facts("idle 1 1", WORK,
           "This session is 1h 30m old and 251.5k tokens.\n"
           "  1. Resume from summary (recommended)\n"
           "  2. Resume full session as-is\n  3. Don't ask me again\n"
           "  Enter to confirm \u00b7 Esc to cancel"),
     "needs action: resume prompt"),
    ("any other dialog — by the footer, no need to know the question",
     facts("idle 1 1", CLEAN,
           "Do you want to proceed?\n  1. Yes\n  2. No\n"
           "  Enter to confirm \u00b7 Esc to cancel"),
     "needs action: dialog"),
    ("footer scrolled up — the question's already answered",
     facts("busy 1 1", CLEAN,
           "  Enter to confirm \u00b7 Esc to cancel\n"
           "Herding bytes\n  6 tasks (3 done)"), "busy"),
    ("model quota ran out",
     facts("idle 1 1", CLEAN, "You're out of usage credits. keep using Opus 4.5"),
     "no model quota: Opus 4.5"),
    ("quota was hit, but the model's already switched",
     facts("busy 1 1", CLEAN,
           "out of usage credits\nSet model to sonnet\nHerding bytes"), "busy"),
    # Отказ провайдера: в таблицу едет средний блок скобок. Код (1308) и
    # request id мастеру не говорят ничего, «Request rejected (429)» умалчивает
    # время возврата квоты — а именно оно решает, ждать папета или переводить.
    ("provider refused for quota, message wrapped by the renderer",
     facts("idle 1 1", WORK,
           "● API Error: Request rejected (429) · [1308][Usage limit reached for"
           " 5 hour. Your limit will reset at 2026-08-31\n"
           "  18:19:41][20260831150427d7cd9f9634d84ecc]\n"
           "✻ Brewed for 57m 29s · done 2:04 PM\n"
           "  6 tasks (3 done, 1 in progress, 2 open)"),
     "error: Usage limit reached for 5 hour. Your limit will reset at"
     " 2026-08-31 18:19:41"),
    ("provider refusal without brackets — show what we have",
     facts("idle 1 1", CLEAN, "● API Error: Connection error"),
     "error: Connection error"),
    # Пара снята с живого пула 2026-08-31: обе сессии несут в буфере ОДИН И ТОТ
    # ЖЕ отказ, но первую восстановили вместе со скроллбэком, и она работает.
    # Отличает их не жалоба, а то, что под ней.
    ("restored session brought a refusal from history and is working",
     facts("idle 1 1", WORK,
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "✻ Brewed for 57m 29s · done 2:04 PM\n"
           "● Session model glm-5.3 could not be restored — using opus instead\n"
           "❯ продолжай\n  \u23bf  4 skills available\n  Ran 3 shell commands"),
     "free (bug/1063)"),
    ("same complaint, but underneath is only a cut-off turn — the puppet really is stuck",
     facts("idle 1 1", WORK,
           "  Ran 2 shell commands\n"
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "✻ Baked for 49m 57s · done 2:04 PM\n"
           "  2 tasks (0 done, 1 in progress, 1 open)\n"
           "  \u25fc Add clo gitlab fetch-redemption"),
     "error: Usage limit reached"),
    ("there was a refusal, but the model's already switched",
     facts("busy 1 1", CLEAN,
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "Set model to sonnet\nHerding bytes"), "busy"),

    # Файла сессии нет (старый claude / нет python3) -> откаты.
    ("no session file, but the pane says working",
     facts("none", CLEAN, "Working... running tests"), "busy"),
    ("no session file, pane is silent -> branch alone",
     facts("none", WORK, "какой-то текст"), "busy: bug/1063"),
    ("no session file, no clone either",
     facts("none", None, "какой-то текст"), "no clone yet"),
]


def main():
    bad = 0
    for what, given, want in CASES:
        got = puppet_state(given)
        if got != want:
            bad += 1
            print(f"FAILED  {what}\n  wanted:  {want!r}\n  got: {got!r}")
    cases = len(CASES)
    for state, want in FREE_CASES:
        cases += 1
        if is_free(state) != want:
            bad += 1
            print(f"FAILED  is_free({state!r})")
    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
