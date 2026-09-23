#!/usr/bin/env python3
"""Проверка диспетчера командлетов без пула: python3 tests/cli.py

Командлеты живут модулями в mop/cli (#75): секции — плоские имена, группы —
подпакеты с глаголами. Чистое здесь — каталог имён с отказом на дубль,
разбор argv в (модуль, аргументы) и описание команды из докстринга без
импорта модуля. Сам запуск команд — только руками.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import cli  # noqa: E402
from mop.cli import lib  # noqa: E402

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

    # deploy на python (#76): чистое — отвергнутые старые цели, недостающие
    # файлы MOP_BODY_EXTRA, имена проектов из origin'ов и легаси, --extra-vars.
    # HYPOTHESIS: deploy на bash, зовёт mop config/projects/list подпроцессом.
    # SOLUTION: mop.cli.pool.deploy поверх библиотеки. STATUS: FIXED — see #76
    try:
        from mop.cli.pool import deploy
        from mop import projects
    except ImportError as e:
        print(f"FAIL {e}")
        return 1
    for argv, want in [(["nomad"], "nomad"), (["pool"], "pool"), (["all"], "all"),
                       ([], None), (["git@h:g/x.git"], None), (["mop"], None)]:
        if deploy.refused_target(argv) != want:
            failed += 1
            print(f"FAIL refused_target({argv}) != {want!r}")
    root = tempfile.mkdtemp(prefix="mop-test-deploy-")
    open(os.path.join(root, "sandbox.yaml"), "w").close()
    if deploy.missing_extras("sandbox.yaml, other.yaml,", root) != ["other.yaml"]:
        failed += 1
        print(f"FAIL missing_extras: {deploy.missing_extras('sandbox.yaml, other.yaml,', root)}")
    if deploy.missing_extras("", root) != []:
        failed += 1
        print("FAIL missing_extras of an empty setting must be empty")
    if projects.names({"git@h:g/proj.git", "git@h:g/mop.git"}, {"legacy"}) != ["legacy", "mop", "proj"]:
        failed += 1
        print(f"FAIL projects.names: {projects.names({'git@h:g/proj.git', 'git@h:g/mop.git'}, {'legacy'})}")
    # Списком, а не строкой: `--extra-vars mop_projects=[...]` ansible берёт как
    # строку и проходит по её символам, порождая пользователей `master-[`.
    ev = lib.play_vars(["mop", "proj"], {"proj": {"asks": {}}})
    if json.loads(ev[0]) != {"mop_projects": ["mop", "proj"]} or json.loads(ev[1]) != {"mop_manifests": {"proj": {"asks": {}}}}:
        failed += 1
        print(f"FAIL play_vars: {ev}")
    # Узкий прогон проектов (#79) идёт без манифестов: их читают слои узла и
    # тела, а не роль шины. Лишний --extra-vars пустым словарём стирал бы
    # манифесты, уже разложенные полной игрой.
    if lib.play_vars(["mop"]) != [json.dumps({"mop_projects": ["mop"]})]:
        failed += 1
        print(f"FAIL play_vars without manifests: {lib.play_vars(['mop'])}")
    # Лимиты папетов (#107) едут рядом с проектами, и пустые тоже: пустой
    # словарь -- правда контроллера «лимитов нет», и сервер обязан её
    # получить, иначе снятый лимит жил бы там дальше. Сама функция чистая:
    # файл читает play(), а не она.
    got = lib.play_vars(["mop"], limits={})
    if got != [json.dumps({"mop_projects": ["mop"], "mop_limits": {}})]:
        failed += 1
        print(f"FAIL play_vars with empty limits: {got}")
    got = lib.play_vars(["mop"], limits={"mop": 2})
    if json.loads(got[0]).get("mop_limits") != {"mop": 2}:
        failed += 1
        print(f"FAIL play_vars with limits: {got}")
    # STATUS: FIXED — see #107

    # Локаль прогонов (#92): ansible требует UTF-8 и берёт её из окружения, а
    # свежая машина несёт LANG=C. Ставит её диспетчер, потому что зовут
    # прогон и `mop setup`, и `mop deploy`, и сборка образа.
    # Локаль прогона считается от локали УСТАНОВКИ, а не от унаследованного
    # окружения. Окружение как раз и бывает сломано: ssh привозит LC_* с
    # машины оператора, и на сервере такой локали нет — ansible тогда не
    # стартует, хотя LANG и LC_CTYPE выглядят исправными.
    have = {"C.UTF-8", "ru_RU.UTF-8"}
    for wanted, want in [("ru_RU.UTF-8", "ru_RU.UTF-8"),   # есть на машине
                         ("de_DE.UTF-8", "C.UTF-8"),       # нет — запасная
                         ("C", "C.UTF-8"),                 # не UTF-8 — запасная
                         ("", "C.UTF-8")]:
        got = cli.run_locale(wanted, usable=have.__contains__)
        if got != want:
            failed += 1
            print(f"FAIL run_locale({wanted!r}) -> {got!r}, wanted {want!r}")
    # Запасная берётся, даже если и её на машине нет: сказать нечего, а
    # C.UTF-8 встроена в glibc и есть везде, где есть сам glibc.
    if cli.run_locale("ru_RU.UTF-8", usable=lambda _: False) != "C.UTF-8":
        failed += 1
        print("FAIL run_locale must fall back to C.UTF-8")

    # Чем запускать команду, которой нужны права root (#91). Под root —
    # ничем: повышать нечего, а на выделенном сервере ещё и нечем, там
    # `sudo` попросту не стоит, и команда падала трассировкой на первом же
    # шаге установки.
    from mop.cli.pool import setup as pool_setup
    if pool_setup.elevate(uid=0) != []:
        failed += 1
        print(f"FAIL elevate as root: {pool_setup.elevate(uid=0)}")
    if pool_setup.elevate(uid=1000) != ["sudo"]:
        failed += 1
        print(f"FAIL elevate as a user: {pool_setup.elevate(uid=1000)}")

    # Группы разрезаны по глаголам (#77): каждый глагол из usage группы
    # (`  mop <группа> <глагол>`) — свой модуль, и диспетчер находит его по
    # имени; таблиц VERBS в группах нет.
    # HYPOTHESIS: группа — один модуль с VERBS. SOLUTION: подпакет, глагол —
    # модуль, __init__ отвечает без глагола. STATUS: FIXED — see #77
    import re
    have = cli.verbs()
    for group in sorted(g for _, g, _ in [(0, s, p) for s, _, p in cli.scan() if p]):
        path = os.path.join(cli.PACKAGE, group, "__init__.py")
        with open(path) as f:
            doc = f.read()
        listed = set(re.findall(rf"^  mop {group} ([a-z]+)", doc, re.M))
        missing = listed - have.get(group, set())
        if missing:
            failed += 1
            print(f"FAIL group {group}: verbs without a module: {sorted(missing)}")
        if not listed:
            failed += 1
            print(f"FAIL group {group}: its docstring lists no verbs")

    # Каждый модуль команды определяет `main` ровно один раз, и никакое имя
    # верхнего уровня не определяется дважды: второе определение молча
    # затирает первое, и py_compile этого не видит. Поймано на живом узле —
    # `_inner_script` переименовали в `main` вместе с `v_run`, и врапер
    # падал NameError после ensure.
    import ast as _ast
    for root, _, files in os.walk(cli.PACKAGE):
        for f in files:
            if not f.endswith(".py") or f.startswith("_") and f != "__init__.py":
                continue
            path = os.path.join(root, f)
            if root == cli.PACKAGE:
                continue
            with open(path) as fh:
                tree = _ast.parse(fh.read())
            defs = [n.name for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))]
            dup = sorted({d for d in defs if defs.count(d) > 1})
            if dup:
                failed += 1
                print(f"FAIL {os.path.relpath(path)}: defined twice: {dup}")
            is_section_init = f == "__init__.py" and os.path.basename(root) in cli.SECTIONS
            if not is_section_init and "main" not in defs and not any(
                    isinstance(n, _ast.Assign) and any(getattr(t, "id", "") == "main" for t in n.targets)
                    for n in tree.body):
                failed += 1
                print(f"FAIL {os.path.relpath(path)}: no main(argv)")

    print("cli: FAILED" if failed else "cli: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
