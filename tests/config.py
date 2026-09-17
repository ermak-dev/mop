#!/usr/bin/env python3
"""Проверка настроек без пула: python3 tests/config.py

Здесь только то, что СЧИТАЕТСЯ, а не лежит литералом: у вычисленного дефолта
есть шанс оказаться неверным, и проявляется это далеко от config.py.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import pwd
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import config  # noqa: E402


def main():
    bad = 0
    cases = 0
    me = pwd.getpwuid(os.getuid()).pw_name

    # ЗАЩЕМЛЕНО ЖИВЫМ ОТКАЗОМ (2026-09-17, первый контейнерный папет).
    # Дефолт MOP_USER брался из $USER. Внешний врапер исполняется задачей
    # Nomad, а клиент Nomad ходит под root и свой $USER задаче отдаёт —
    # поэтому драйвер собрал `ssh root@<тело>` и получил «Permission denied»
    # при совершенно исправном ключе. uid процесса — ФАКТ, $USER — всего лишь
    # утверждение, и верить надо факту.
    saved = os.environ.get("USER")
    try:
        os.environ["USER"] = "root-from-the-nomad-client"
        cases += 1
        if config.pool_user() != me:
            bad += 1
            print(f"FAILED  pool_user() believed $USER over the real uid: "
                  f"{config.pool_user()!r}, wanted {me!r}")
        os.environ.pop("USER", None)
        cases += 1
        if config.pool_user() != me:
            bad += 1
            print("FAILED  pool_user() must work with no $USER at all — that is "
                  "exactly a Nomad task's environment")
    finally:
        os.environ.pop("USER", None)
        if saved is not None:
            os.environ["USER"] = saved

    # Настройка старше дефолта: установка, где пользователь пула не совпадает
    # с тем, под кем крутится mop, вписывает его в .env.
    cases += 1
    os.environ["MOP_USER"] = "someone-else"
    try:
        if config.get("MOP_USER") != "someone-else":
            bad += 1
            print("FAILED  MOP_USER from the environment must outrank the default")
    finally:
        os.environ.pop("MOP_USER", None)

    # Шлюз сети тел СЧИТАЕТСЯ из подсети: два места для одного адреса разошлись
    # бы молча — мост встал бы, а тела просто не достучались.
    cases += 1
    os.environ["MOP_PVE_SUBNET"] = "192.168.250.0/24"
    try:
        if config.get("MOP_PVE_GATEWAY") != "192.168.250.1":
            bad += 1
            print(f"FAILED  gateway must be derived from the subnet: "
                  f"{config.get('MOP_PVE_GATEWAY')!r}")
    finally:
        os.environ.pop("MOP_PVE_SUBNET", None)

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
