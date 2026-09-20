#!/usr/bin/env python3
"""Проверка настроек без пула: python3 tests/config.py

Здесь только то, что СЧИТАЕТСЯ, а не лежит литералом: у вычисленного дефолта
есть шанс оказаться неверным, и проявляется это далеко от config.py.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import tempfile
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

    # ── старшинство источников ───────────────────────────────────────────
    # Окружение > node.env > .env > дефолт. Ярус node.env появился потому, что
    # .env НА УЗЛЫ НЕ ЕДЕТ (там креды GitLab), а тринадцать настроек читаются
    # именно на узле — все MOP_PVE_*, потолок памяти тела, пользователь и дом.
    # Пока яруса не было, вписанное в .env значение доезжало до мастера и НЕ
    # доезжало до узла: сборка образа клала тело на одно хранилище, драйвер на
    # узле искал его на другом, и не жаловался никто.
    node = os.path.join(tempfile.mkdtemp(), "node.env")
    saved_node, saved_env = config.NODE_ENV_FILE, config.ENV_FILE
    envf = os.path.join(tempfile.mkdtemp(), ".env")
    with open(envf, "w") as f:
        f.write("MOP_PVE_STORAGE=from-env-file\nMOP_PVE_CORES=2\n")
    try:
        config.NODE_ENV_FILE, config.ENV_FILE = node, envf
        config.forget()
        cases += 1
        if config.get("MOP_PVE_STORAGE") != "from-env-file":
            bad += 1
            print("FAILED  .env must outrank the default when there is no node file")

        with open(node, "w") as f:
            f.write("# узловое, положено прогоном deploy\n"
                    "MOP_PVE_STORAGE=from-node\n")
        config.forget()
        cases += 1
        if config.get("MOP_PVE_STORAGE") != "from-node":
            bad += 1
            print("FAILED  node.env must outrank .env — that is the whole point")
        cases += 1
        if config.num("MOP_PVE_CORES") != 2:
            bad += 1
            print("FAILED  a setting absent from node.env must still come from .env")

        os.environ["MOP_PVE_STORAGE"] = "from-environment"
        cases += 1
        if config.get("MOP_PVE_STORAGE") != "from-environment":
            bad += 1
            print("FAILED  the environment must outrank node.env — a one-off run "
                  "has to keep working")
        os.environ.pop("MOP_PVE_STORAGE", None)

        os.unlink(node)
        config.forget()
        cases += 1
        if config.get("MOP_PVE_STORAGE") != "from-env-file":
            bad += 1
            print("FAILED  a missing node.env is the normal case on the control "
                  "machine, not an error")
        cases += 1
        if config.effective()["MOP_PVE_CORES"][1] != ".env":
            bad += 1
            print("FAILED  effective() must name where a value came from")
        with open(node, "w") as f:
            f.write("MOP_PVE_CORES=8\n")
        config.forget()
        cases += 1
        if config.effective()["MOP_PVE_CORES"] != ("8", "node"):
            bad += 1
            print(f"FAILED  effective() must call the node file by its name: "
                  f"{config.effective()['MOP_PVE_CORES']!r}")
    finally:
        config.NODE_ENV_FILE, config.ENV_FILE = saved_node, saved_env
        config.forget()

    # ── .mop: что проект просит себе сам ─────────────────────────────────
    # Файл приезжает ИЗ ЧУЖОГО РЕПОЗИТОРИЯ и говорит, сколько ресурсов взять.
    # Поэтому разбор обязан быть проверяем здесь: что дозволено, то и
    # применяется, остальное отбрасывается ГРОМКО — молча проглоченный ключ
    # это либо не сработавшая настройка, либо сработавшая чужая.
    MOP = [
        # (что проверяем, текст .mop, принято, отброшено)
        ("размеры тела", "MOP_PVE_DISK_GB=200\nMOP_PVE_CORES=8\n",
         {"MOP_PVE_DISK_GB": "200", "MOP_PVE_CORES": "8"}, []),
        ("комментарии и пустые строки",
         "# мой проект тяжёлый\n\nMOP_PVE_DISK_GB=200\n",
         {"MOP_PVE_DISK_GB": "200"}, []),
        ("пробелы по краям", "  MOP_PVE_CORES = 8  \n",
         {"MOP_PVE_CORES": "8"}, []),
        ("кавычки снимаются", 'MOP_PVE_CORES="8"\n', {"MOP_PVE_CORES": "8"}, []),
        # Проект НЕ вправе переставить сервер пула, сменить пользователя или
        # подсунуть свои задачи ansible в образ: это не его размеры, это чужая
        # машина. Состав образа — решение УСТАНОВКИ, и MOP_BODY_EXTRA сюда не
        # входит намеренно.
        ("чужая настройка отбрасывается", "MOP_SERVER_LAN=10.0.0.1\n",
         {}, ["MOP_SERVER_LAN"]),
        ("состав образа проекту не отдан", "MOP_BODY_EXTRA=../../etc/passwd\n",
         {}, ["MOP_BODY_EXTRA"]),
        ("выдуманный ключ", "ЧТО_УГОДНО=1\n", {}, ["ЧТО_УГОДНО"]),
        ("своё и чужое вперемешку",
         "MOP_PVE_CORES=8\nMOP_USER=root\n",
         {"MOP_PVE_CORES": "8"}, ["MOP_USER"]),
        ("пустой файл", "", {}, []),
        ("строка без знака равенства", "просто текст\n", {}, []),
    ]
    for what, text, want, want_bad in MOP:
        cases += 1
        got, bad_keys = config.shard_settings(text)
        if got != want or sorted(bad_keys) != sorted(want_bad):
            bad += 1
            print(f"FAILED  .mop, {what}\n  wanted: {want!r} + отброшено {want_bad!r}"
                  f"\n  got: {got!r} + отброшено {bad_keys!r}")

    # Всё, что проект вправе просить, обязано быть настройкой: иначе оно
    # никуда не доедет, а отказа не будет.
    cases += 1
    unknown = [k for k in config.SHARD_SCOPED if k not in config.SETTINGS]
    if unknown:
        bad += 1
        print(f"FAILED  SHARD_SCOPED names settings that do not exist: {unknown}")

    # Потолок — УЗЛОВОЙ: чужой проект просит, машина решает. Без потолка .mop
    # это способ занять гипервизор, а не настройка.
    for cap in ("MOP_BODY_MEM_CAP_MB", "MOP_BODY_DISK_CAP_GB", "MOP_BODY_CORES_CAP"):
        cases += 1
        if cap not in config.NODE_SCOPED:
            bad += 1
            print(f"FAILED  {cap} must be node-scoped — the machine has the last word")

    # Список узловых настроек -- ОДИН: по нему deploy решает, что рендерить в
    # node.env. Разойдись он с тем, что читает узел, и настройка молча не
    # доедет -- ровно та беда, ради которой ярус и заводился.
    cases += 1
    unknown = [k for k in config.NODE_SCOPED if k not in config.SETTINGS]
    if unknown:
        bad += 1
        print(f"FAILED  NODE_SCOPED names settings that do not exist: {unknown}")

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
