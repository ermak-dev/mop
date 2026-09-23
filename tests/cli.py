#!/usr/bin/env python3
"""Проверка диспетчера командлетов без пула: python3 tests/cli.py

Командлеты живут модулями в mop/cli (#75): секции — плоские имена, группы —
подпакеты с глаголами. Чистое здесь — каталог имён с отказом на дубль,
разбор argv в (модуль, аргументы) и описание команды из докстринга без
импорта модуля. Сам запуск команд — только руками.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import cli  # noqa: E402

# (каталог, имя, подпакет?) — то, что находит обход mop/cli.
FOUND = [
    ("core", "add", False), ("core", "list", False), ("pool", "deploy", False),
    ("service", "mcp", False), ("driver", "", True), ("bug", "", True),
]
VERBS = {"driver": {"build", "run"}, "bug": set()}


def main():
    failed = 0
    # HYPOTHESIS: каталога нет — диспетчер bash ищет файл по имени в bin/.
    # SOLUTION: catalog() из обхода пакета, resolve() по нему.
    # STATUS: FIXED — see #75
    cat = cli.catalog(FOUND)
    want = {"add": "mop.cli.core.add", "list": "mop.cli.core.list",
            "deploy": "mop.cli.pool.deploy", "mcp": "mop.cli.service.mcp",
            "driver": "mop.cli.driver", "bug": "mop.cli.bug"}
    if cat != want:
        failed += 1
        print(f"FAIL catalog: {cat}")
    # Одно имя в двух секциях — отказ, а не «кто первый».
    try:
        cli.catalog(FOUND + [("pool", "add", False)])
        failed += 1
        print("FAIL catalog: a duplicate name must refuse")
    except RuntimeError as e:
        if "add" not in str(e):
            failed += 1
            print(f"FAIL catalog: the refusal must name the duplicate: {e}")

    # Плоская команда: модуль и остаток argv.
    for argv, want in [
        (["list"], ("mop.cli.core.list", [])),
        (["add", "--llm", "x"], ("mop.cli.core.add", ["--llm", "x"])),
        # Группа с глаголом-модулем: модуль глагола, остаток без глагола.
        (["driver", "build", "mop"], ("mop.cli.driver.build", ["mop"])),
        # Группа без такого модуля-глагола: сама группа, argv целиком —
        # так переезжают группы, пока их не разрезали (#77).
        (["bug", "new", "t"], ("mop.cli.bug", ["new", "t"])),
        (["driver"], ("mop.cli.driver", [])),
    ]:
        got = cli.resolve(argv, cat, VERBS)
        if got != want:
            failed += 1
            print(f"FAIL resolve({argv}): {got} != {want}")
    if cli.resolve(["nope"], cat, VERBS) is not None:
        failed += 1
        print("FAIL resolve of an unknown name must be None")
    if cli.resolve([], cat, VERBS) is not None:
        failed += 1
        print("FAIL resolve of nothing must be None")

    # Описание — первая строка докстринга, прочитанная без импорта: импорт
    # тянул бы шину и падал бы на машине без nats-py ради строки help.
    d = tempfile.mkdtemp(prefix="mop-test-cli-")
    p = os.path.join(d, "x.py")
    with open(p, "w") as f:
        f.write('"""do a thing: mop x [--flag]\n\nmore text\n"""\nimport nothing_such\n')
    if cli.describe(p) != "do a thing: mop x [--flag]":
        failed += 1
        print(f"FAIL describe: {cli.describe(p)!r}")
    with open(p, "w") as f:
        f.write("x = 1\n")
    if cli.describe(p) != "":
        failed += 1
        print("FAIL describe of a module without a docstring must be empty")

    print("cli: FAILED" if failed else "cli: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
