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
    ("AGENT SILENT (агент узла node1 молчит 20с)", False),
    ("needs action", False),
]


def facts(session, clone=CLEAN, screen="Herding bytes"):
    return {"present": True, "screen": screen, "session": session, "clone": clone}


CASES = [
    # (что случилось, факты, ожидаемое состояние)
    ("узел не признаёт папет своим",
     {"present": False}, "HUNG (no tmux session)"),
    ("агент вернул ошибку",
     {"error": "tmux не отвечает"}, "HUNG (tmux не отвечает)"),
    ("пустой пейн — папет только поднялся",
     facts("idle 1 1", screen="   \n\n"), "free"),

    # Сокет = живость, файл = активность. Сочетания разбираются здесь.
    ("сессия жива и работает", facts("busy 1 1", WORK), "busy: bug/1063"),
    ("работает на дефолтной ветке", facts("busy 1 1", CLEAN), "busy"),
    ("процесс жив, но сокет молчит", facts("hung 1 0"), "HUNG (not responding)"),
    ("процесс мёртв, файл протух", facts("idle 0 0"), "HUNG (not responding)"),
    ("упёрлась в запрос действия",
     facts("requires_action 1 1"), "needs action"),
    ("ждёт ввода — не трогаем", facts("waiting 1 1"), "waiting for input"),

    # Свободен = в клоне нечего терять. На этом стоит решение о диспатче.
    ("чисто и всё на origin", facts("idle 1 1", CLEAN), "free (master)"),
    ("чужая ветка, но всё отправлено",
     facts("idle 1 1", WORK), "free (bug/1063)"),
    ("несохранённые файлы",
     facts("idle 1 1", {**WORK, "dirty": 3}), "idle: bug/1063 (uncommitted: 3)"),
    ("неотправленные коммиты",
     facts("idle 1 1", {**WORK, "ahead": 2}), "idle: bug/1063 (unpushed: 2)"),
    ("и то и другое — двумя числами, не суммой",
     facts("idle 1 1", {**WORK, "dirty": 3, "ahead": 2}),
     "idle: bug/1063 (uncommitted: 3, unpushed: 2)"),

    # Жалобы видны только на экране: сессия жива и отвечает, а ход выдать не может.
    ("логин протух",
     facts("idle 1 1", WORK, "Login expired · Please run /login"),
     "login expired: bug/1063"),
    ("не залогинен вовсе",
     facts("idle 1 1", CLEAN, "Not logged in · Run /login"), "not logged in"),
    # Подъём истории спрашивает, чем поднимать длинную сессию. Файл сессии при
    # этом здоров, и без экрана папет читался бы свободным.
    ("встал на выборе, чем поднимать историю",
     facts("idle 1 1", WORK,
           "This session is 1h 30m old and 251.5k tokens.\n"
           "  1. Resume from summary (recommended)\n"
           "  2. Resume full session as-is\n  3. Don't ask me again\n"
           "  Enter to confirm \u00b7 Esc to cancel"),
     "needs action: resume prompt"),
    ("любой другой диалог — по футеру, вопрос знать не обязательно",
     facts("idle 1 1", CLEAN,
           "Do you want to proceed?\n  1. Yes\n  2. No\n"
           "  Enter to confirm \u00b7 Esc to cancel"),
     "needs action: диалог"),
    ("футер уехал вверх — вопрос уже отвечен",
     facts("busy 1 1", CLEAN,
           "  Enter to confirm \u00b7 Esc to cancel\n"
           "Herding bytes\n  6 tasks (3 done)"), "busy"),
    ("кончилась квота модели",
     facts("idle 1 1", CLEAN, "You're out of usage credits. keep using Opus 4.5"),
     "no model quota: Opus 4.5"),
    ("квота была, но модель уже переключили",
     facts("busy 1 1", CLEAN,
           "out of usage credits\nSet model to sonnet\nHerding bytes"), "busy"),
    # Отказ провайдера: в таблицу едет средний блок скобок. Код (1308) и
    # request id мастеру не говорят ничего, «Request rejected (429)» умалчивает
    # время возврата квоты — а именно оно решает, ждать папета или переводить.
    ("провайдер отказал по квоте, сообщение перенесено рендером",
     facts("idle 1 1", WORK,
           "● API Error: Request rejected (429) · [1308][Usage limit reached for"
           " 5 hour. Your limit will reset at 2026-08-31\n"
           "  18:19:41][20260831150427d7cd9f9634d84ecc]\n"
           "✻ Brewed for 57m 29s · done 2:04 PM\n"
           "  6 tasks (3 done, 1 in progress, 2 open)"),
     "error: Usage limit reached for 5 hour. Your limit will reset at"
     " 2026-08-31 18:19:41"),
    ("отказ провайдера без скобок — показываем что есть",
     facts("idle 1 1", CLEAN, "● API Error: Connection error"),
     "error: Connection error"),
    # Пара снята с живого пула 2026-08-31: обе сессии несут в буфере ОДИН И ТОТ
    # ЖЕ отказ, но первую восстановили вместе со скроллбэком, и она работает.
    # Отличает их не жалоба, а то, что под ней.
    ("восстановленная сессия принесла отказ из истории и работает",
     facts("idle 1 1", WORK,
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "✻ Brewed for 57m 29s · done 2:04 PM\n"
           "● Session model glm-5.3 could not be restored — using opus instead\n"
           "❯ продолжай\n  \u23bf  4 skills available\n  Ran 3 shell commands"),
     "free (bug/1063)"),
    ("та же жалоба, но под ней только оборванный ход — папет правда встал",
     facts("idle 1 1", WORK,
           "  Ran 2 shell commands\n"
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "✻ Baked for 49m 57s · done 2:04 PM\n"
           "  2 tasks (0 done, 1 in progress, 1 open)\n"
           "  \u25fc Add clo gitlab fetch-redemption"),
     "error: Usage limit reached"),
    ("отказ был, но модель уже переключили",
     facts("busy 1 1", CLEAN,
           "● API Error: Request rejected (429) · [1308][Usage limit reached]\n"
           "Set model to sonnet\nHerding bytes"), "busy"),

    # Файла сессии нет (старый claude / нет python3) -> откаты.
    ("нет файла сессии, но пейн говорит о работе",
     facts("none", CLEAN, "Working... running tests"), "busy"),
    ("нет файла сессии, пейн молчит -> одна лишь ветка",
     facts("none", WORK, "какой-то текст"), "busy: bug/1063"),
    ("нет ни файла сессии, ни клона",
     facts("none", None, "какой-то текст"), "no clone yet"),
]


def main():
    bad = 0
    for what, given, want in CASES:
        got = puppet_state(given)
        if got != want:
            bad += 1
            print(f"ПРОВАЛ  {what}\n  ждали:  {want!r}\n  вышло: {got!r}")
    cases = len(CASES)
    for state, want in FREE_CASES:
        cases += 1
        if is_free(state) != want:
            bad += 1
            print(f"ПРОВАЛ  is_free({state!r})")
    print(f"{cases - bad}/{cases} сошлось")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
