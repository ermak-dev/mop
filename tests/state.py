#!/usr/bin/env python3
"""Проверка slave_state без пула: python3 tests/state.py

Пока состояние собиралось поверх alloc exec, проверить его можно было только
на живом слейве — и регрессия однажды спряталась именно здесь: пробник сессии
молча падал в откат по ветке, а `mop list` выглядел исправным. С переездом на
шину агент отдаёт ФАКТЫ, а вердикт собирается чистой функцией, поэтому вся
матрица проверяется здесь.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле, и тестов на него нет.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.slaves import slave_state  # noqa: E402

CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
WORK = {"cur": "bug/1063", "def": "master", "dirty": 0, "ahead": 0}


def facts(session, clone=CLEAN, screen="Herding bytes"):
    return {"present": True, "screen": screen, "session": session, "clone": clone}


CASES = [
    # (что случилось, факты, ожидаемое состояние)
    ("узел не признаёт слейв своим",
     {"present": False}, "ЗАВИС (нет tmux-сессии)"),
    ("агент вернул ошибку",
     {"error": "tmux не отвечает"}, "ЗАВИС (tmux не отвечает)"),
    ("пустой пейн — слейв только поднялся",
     facts("idle 1 1", screen="   \n\n"), "свободен"),

    # Сокет = живость, файл = активность. Сочетания разбираются здесь.
    ("сессия жива и работает", facts("busy 1 1", WORK), "занят: bug/1063"),
    ("работает на дефолтной ветке", facts("busy 1 1", CLEAN), "занят"),
    ("процесс жив, но сокет молчит", facts("hung 1 0"), "ЗАВИС (не отвечает)"),
    ("процесс мёртв, файл протух", facts("idle 0 0"), "ЗАВИС (не отвечает)"),
    ("упёрлась в запрос действия",
     facts("requires_action 1 1"), "требует действия"),
    ("ждёт ввода — не трогаем", facts("waiting 1 1"), "ждёт ввода"),

    # Свободен = в клоне нечего терять. На этом стоит решение о диспатче.
    ("чисто и всё на origin", facts("idle 1 1", CLEAN), "свободен (master)"),
    ("чужая ветка, но всё отправлено",
     facts("idle 1 1", WORK), "свободен (bug/1063)"),
    ("несохранённые файлы",
     facts("idle 1 1", {**WORK, "dirty": 3}), "занят: bug/1063 (не закоммичено: 3)"),
    ("неотправленные коммиты",
     facts("idle 1 1", {**WORK, "ahead": 2}), "занят: bug/1063 (не отправлено: 2)"),
    ("и то и другое — двумя числами, не суммой",
     facts("idle 1 1", {**WORK, "dirty": 3, "ahead": 2}),
     "занят: bug/1063 (не закоммичено: 3, не отправлено: 2)"),

    # Жалобы видны только на экране: сессия жива и отвечает, а ход выдать не может.
    ("логин протух",
     facts("idle 1 1", WORK, "Login expired · Please run /login"),
     "логин протух: bug/1063"),
    ("не залогинен вовсе",
     facts("idle 1 1", CLEAN, "Not logged in · Run /login"), "не залогинен"),
    ("кончилась квота модели",
     facts("idle 1 1", CLEAN, "You're out of usage credits. keep using Opus 4.5"),
     "нет квоты модели: Opus 4.5"),
    ("квота была, но модель уже переключили",
     facts("busy 1 1", CLEAN,
           "out of usage credits\nSet model to sonnet\nHerding bytes"), "занят"),

    # Файла сессии нет (старый claude / нет python3) -> откаты.
    ("нет файла сессии, но пейн говорит о работе",
     facts("none", CLEAN, "Working... running tests"), "занят"),
    ("нет файла сессии, пейн молчит -> одна лишь ветка",
     facts("none", WORK, "какой-то текст"), "занят: bug/1063"),
    ("нет ни файла сессии, ни клона",
     facts("none", None, "какой-то текст"), "клона ещё нет"),
]


def main():
    bad = 0
    for what, given, want in CASES:
        got = slave_state(given)
        if got != want:
            bad += 1
            print(f"ПРОВАЛ  {what}\n  ждали:  {want!r}\n  вышло:  {got!r}")
    print(f"{len(CASES) - bad}/{len(CASES)} сошлось")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
