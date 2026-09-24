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

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, ROOT)

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

    # HYPOTHESIS (#140): строка хода одна, и строкам вывода сборки на
    # терминале негде жить. SOLUTION: frame() -- кадр из нескольких строк,
    # перерисовываемый на месте: вверх на прежнюю высоту, стереть до конца
    # экрана, новые строки, обрезанные по ширине, чтобы перенос не сбил счёт.
    E = "\033"
    got = lib.frame(0, ["one", "two", "step"], 80)
    if got != f"\r{E}[Jone\ntwo\nstep":
        failed += 1
        print(f"FAIL frame from nothing: {got!r}")
    got = lib.frame(3, ["step"], 80)
    if got != f"\r{E}[2A{E}[Jstep":
        failed += 1
        print(f"FAIL frame over three rows: {got!r}")
    if lib.frame(1, [], 80) != f"\r{E}[J":
        failed += 1
        print(f"FAIL frame to nothing erases: {lib.frame(1, [], 80)!r}")
    # Цвета и управляющие символы строки ansible не должны ни красить
    # кадр, ни сдвигать курсор; ширина -- по видимым символам.
    got = lib.frame(0, [f"{E}[0;32mok: [hyper]{E}[0m\tx\r", "abcdefghij"], 6)
    if got != f"\r{E}[Jok: [\nabcde":
        failed += 1
        print(f"FAIL frame must strip escapes and cut to width: {got!r}")
    # STATUS: FIXED — see #140

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
    # Хосты форжей (#121) едут полной игре списком: роль узла доверяет ключу
    # каждого. Без них -- ключа нет вовсе, узкий прогон проектов их не
    # передаёт и роль узла не играет.
    got = lib.play_vars(["mop"], git_hosts=["dev.corp", "git.ermak.dev"])
    if json.loads(got[0]).get("mop_git_hosts") != ["dev.corp", "git.ermak.dev"]:
        failed += 1
        print(f"FAIL play_vars with git hosts: {got}")
    # #178: хосты инвентаря -- списком, только когда их дали.
    got = lib.play_vars(["mop"], inventory_hosts=["a", "b"])
    if json.loads(got[0]).get("mop_inventory_hosts") != ["a", "b"]:
        failed += 1
        print(f"FAIL play_vars with inventory hosts: {got}")
    if "mop_inventory_hosts" in json.loads(lib.play_vars(["mop"])[0]):
        failed += 1
        print("FAIL play_vars without inventory hosts must not send an empty list")
    if "mop_git_hosts" in json.loads(lib.play_vars(["mop"])[0]):
        failed += 1
        print("FAIL play_vars without git hosts must not send an empty list")
    # STATUS: FIXED — see #121

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
        # Глагол бывает и через дефис: `mop driver pve-facts` (#158).
        listed = set(re.findall(rf"^  mop {group} ([a-z][a-z-]*)", doc, re.M))
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

    # ── #111: origin из рабочей копии, одним механизмом ─────────────────
    # HYPOTHESIS: `mop project add` требовал origin всегда, а команды, что
    # умели брать его из рабочей копии, делали это каждая по-своему: своя
    # проверка на вид origin'а у master, свой запасной путь у driver build,
    # отказ cwd_origin всегда называл `mop add`.
    # SOLUTION: lib.pick_origin -- чистое решение, lib.origin -- git и отказ.
    from mop import puppets
    for good in ("git@git.ermak.dev:ermak/mop.git", "https://h/g/mop.git",
                 "/srv/git/mop.git"):
        if not puppets.looks_like_origin(good):
            failed += 1
            print(f"FAIL looks_like_origin({good!r}) must be true")
    # Голое имя и значение чужого флага, приехавшее позиционно, -- не origin.
    for bad in ("mop", "opus", "", "  "):
        if puppets.looks_like_origin(bad):
            failed += 1
            print(f"FAIL looks_like_origin({bad!r}) must be false")

    here = "git@git.ermak.dev:ermak/mop.git"
    other = "git@git.ermak.dev:rugent/rugent.git"
    # Явный аргумент старше рабочей копии.
    if lib.pick_origin(other, here) != other:
        failed += 1
        print("FAIL pick_origin: an explicit origin must win over the working copy")
    # Нет аргумента -- рабочая копия.
    if lib.pick_origin(None, here) != here:
        failed += 1
        print("FAIL pick_origin: without an argument the working copy's origin")
    # Отказы -- ValueError с причиной, а не None: None дальше читался бы как
    # проект с пустым именем.
    for arg, cwd, why in (("opus", here, "doesn't look like a git-origin"),
                          (None, None, "no origin given")):
        try:
            got = lib.pick_origin(arg, cwd)
        except ValueError as e:
            if why not in str(e):
                failed += 1
                print(f"FAIL pick_origin({arg!r}, {cwd!r}) refused without the reason: {e}")
            continue
        failed += 1
        print(f"FAIL pick_origin({arg!r}, {cwd!r}) must refuse, got {got!r}")
    # Неверный явный аргумент не подменяется рабочей копией: опечатка иначе
    # молча завела бы не тот проект.
    # STATUS: FIXED — see #111

    failed += check_mcp_declarations()
    failed += check_refusals()
    failed += check_refusals_163()
    failed += check_output_rules()
    failed += check_empty_llm()
    failed += check_deploy_check()
    failed += check_inventory_drivers()
    failed += check_pool_uniform()
    failed += check_node_memory_197()
    failed += check_fallback_model_183()
    failed += check_bus_import_169()      # последними: перезагружают модули
    failed += check_nomad_import_187()

    print("cli: FAILED" if failed else "cli: ok")
    return 1 if failed else 0


def check_mcp_declarations():
    """#160: инструменты управления MCP -- объявления в самих командлетах.

    HYPOTHESIS: MCP держал вторую реализацию add/update/build руками, и она
    разошлась с командлетами (не слал workspace, собирал образ мимо сборщика).
    SOLUTION: командлет объявляет `MCP = {...}`; mop mcp находит объявления
    обходом пакета, читает их без импорта и зовёт сам командлет."""
    failed = 0
    d = tempfile.mkdtemp(prefix="mop-test-mcp-")
    p = os.path.join(d, "x.py")

    # Объявление читается без импорта, как докстринг: модуль тянет шину.
    with open(p, "w") as f:
        f.write('"""x"""\nimport nothing_such\n'
                'MCP = {"annotations": "destructive", "args": ['
                '{"name": "name", "type": "string", "required": True}]}\n')
    got = cli.declared(p)
    if (got or {}).get("annotations") != "destructive" or \
            [a["name"] for a in got.get("args", [])] != ["name"]:
        failed += 1
        print(f"FAIL declared: {got!r}")
    # Без объявления команда в MCP не видна: attach, master, code, service.
    with open(p, "w") as f:
        f.write('"""x"""\n')
    if cli.declared(p) is not None:
        failed += 1
        print("FAIL declared: a module without MCP must stay out of MCP")
    # Кривое объявление -- громкий отказ, а не молча пропавший инструмент.
    for bad in ('MCP = {"args": [{"name": "fresh", "type": "boolean"}]}',  # без флага
                'MCP = {"args": [{"name": "x", "type": "float", "flag": "--x"}]}',
                'MCP = {"annotations": "sometimes"}',
                'MCP = {"args": [{"name": "a", "type": "string"},'
                ' {"name": "b", "type": "string", "required": True}]}',  # необязательный позиционный раньше обязательного
                'MCP = {"colour": 1}',
                'MCP = dict(args=[])'):                                  # не литерал
        with open(p, "w") as f:
            f.write(bad + "\n")
        try:
            cli.declared(p)
        except ValueError:
            continue
        failed += 1
        print(f"FAIL declared must refuse: {bad}")

    # Описание инструмента -- докстринг командлета целиком: usage и смысл.
    with open(p, "w") as f:
        f.write('"""do x: mop x <name>\n\nmore text\n"""\nimport nothing_such\n')
    if cli.docstring(p) != "do x: mop x <name>\n\nmore text":
        failed += 1
        print(f"FAIL docstring: {cli.docstring(p)!r}")
    # Вывод командлета уходит модели без цветов терминала: lib.fail красит
    # всегда. Строки и табуляция остаются.
    got = lib.plain("\033[0;31mpu-mop-1: gone\033[0m\n\tnext\r")
    if got != "pu-mop-1: gone\n\tnext":
        failed += 1
        print(f"FAIL plain: {got!r}")

    # Имя инструмента -- слова команды через подчёркивание.
    found = [("core", "add", False), ("node", "", True), ("service", "mcp", False)]
    group_verbs = {"node": {"drain", "up"}}
    names = {n: words for n, words, _ in cli.tool_commands(found, group_verbs)}
    want = {"add": ["add"], "node": ["node"], "node_drain": ["node", "drain"],
            "node_up": ["node", "up"], "mcp": ["mcp"]}
    if names != want:
        failed += 1
        print(f"FAIL tool_commands: {names}")

    # argv из значений инструмента: позиционные по порядку, флаги по имени.
    args = [{"name": "name", "type": "string", "required": True},
            {"name": "origin", "type": "string"},
            {"name": "llm", "type": "string", "flag": "--llm"},
            {"name": "days", "type": "integer", "flag": "--days"},
            {"name": "fresh", "type": "boolean", "flag": "--fresh"}]
    for values, want in (
            ({"name": "pu-mop-1"}, ["pu-mop-1"]),
            ({"name": "pu-mop-1", "origin": "git@h:g/mop.git", "llm": "opus",
              "fresh": True, "days": 3},
             ["pu-mop-1", "git@h:g/mop.git", "--llm", "opus", "--days", "3", "--fresh"]),
            ({"name": "pu-mop-1", "origin": "", "llm": "", "fresh": False,
              "days": None}, ["pu-mop-1"])):
        got = cli.tool_argv(args, values)
        if got != want:
            failed += 1
            print(f"FAIL tool_argv({values}) = {got}, want {want}")
    # Отказы: нет обязательного; позиционное, похожее на флаг, -- иначе
    # модель одним значением включала бы чужой флаг командлета.
    for values in ({}, {"name": ""}, {"name": "--fresh"},
                   {"name": "pu-mop-1", "origin": "-x"}):
        try:
            got = cli.tool_argv(args, values)
        except ValueError:
            continue
        failed += 1
        print(f"FAIL tool_argv({values}) must refuse, got {got}")
    # Пропущенный необязательный позиционный, а за ним заданный -- сдвиг на
    # чужое место: отказ.
    two = [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}]
    try:
        got = cli.tool_argv(two, {"b": "x"})
        failed += 1
        print(f"FAIL tool_argv must refuse a gap in positionals, got {got}")
    except ValueError:
        pass
    # STATUS: FIXED — see #160

    # Каждое объявление в дереве разбирается: кривое уронило бы mop mcp
    # целиком на старте мастера.
    for section, name, is_pkg in cli.scan():
        paths = [cli._path_of(section, name, is_pkg)]
        if is_pkg:
            paths += [os.path.join(cli.PACKAGE, section, f"{v}.py")
                      for v in sorted(cli.verbs().get(section, ()))]
        for path in paths:
            try:
                cli.declared(path)
            except ValueError as e:
                failed += 1
                print(f"FAIL {os.path.relpath(path)}: {e}")
    return failed


def check_refusals_163():
    """#163: отказ сервиса кластера на pool и nodes читался пустым списком.

    HYPOTHESIS: puppets.pool() и nodes.rows() делали
    `bus.ask_cluster(...).get("nodes") or []`: нет прав или Nomad лежит --
    `mop node` рисовал пустую таблицу, ростер пула -- пул без узлов, а всё,
    что шло через ready_nodes (stat, disk, sweep, раздача ключей), работало
    по пустому множеству. stat отдавал отказ из main строкой.
    SOLUTION: оба читателя -- через bus.call_cluster (Refused с текстом
    сервиса), stat бросает тот же текст; диспетчер делает из этого stderr и
    ненулевой выход, stdout пуст. Узел с кривым драйвером (#175) -- строка
    с ошибкой, а не отказ списка: это другой случай.
    STATUS: FIXED — see #163"""
    import contextlib
    import importlib
    import io
    from mop import bus, puppets

    failed = 0
    reason = "no rights for nodes: project mop"

    def run(fn, argv=()):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.run(fn, list(argv))
            except SystemExit as e:
                code = e.code
        return out.getvalue(), err.getvalue(), code

    def refusal(what, got, text):
        nonlocal failed
        out, err, code = got
        if not isinstance(code, int) or code == 0 or out or err != text + "\n":
            failed += 1
            print(f"FAIL {what}: stdout {out!r}, stderr {err!r}, exit {code!r}")

    # Живой шине сюда хода нет: и соединение, и адрес -- заглушки.
    async def no_bus(*a, **k):
        raise AssertionError("a check reached the live bus")

    def no_connect(*a, **k):
        raise AssertionError("a check reached the live bus")
    keep = (bus.ask_cluster, bus.request_many, bus.connect, bus.nats.connect,
            puppets.ready_nodes, os.environ.get("MOP_SERVER_LAN"))
    try:
        bus.connect, bus.nats.connect = no_connect, no_bus
        os.environ["MOP_SERVER_LAN"] = "192.0.2.1"
        node = importlib.import_module("mop.cli.node")
        stat = importlib.import_module("mop.cli.core.stat")

        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        refusal("mop node on a refusal", run(node.main), reason)
        for fn in (puppets.pool, puppets.ready_nodes):
            try:
                got = fn()
                failed += 1
                print(f"FAIL {fn.__name__} read a refusal as {got!r}")
            except bus.Refused as e:
                if str(e) != reason:
                    failed += 1
                    print(f"FAIL {fn.__name__} lost the reason: {e!r}")
        refusal("mop stat on a pool refusal", run(stat.main), reason)
        # Подвал `mop list`: причина видна, а не пустой пул.
        if lib.pool_lines() != [f"  {reason}"]:
            failed += 1
            print(f"FAIL pool_lines on a refusal: {lib.pool_lines()!r}")

        # stat: ни один узел не ответил -- тот же текст, что раньше, но
        # отказом: stderr, ненулевой выход, stdout пуст.
        puppets.ready_nodes = lambda: {"n1", "n2"}
        bus.request_many = lambda reqs, **kw: {}
        refusal("mop stat with no node answering", run(stat.main),
                "no node answered:\n  n1: no response\n  n2: no response")

        # Пустой пул без отказа сервиса -- не «никто не ответил» с пустым
        # перечнем, а прямо: готовых узлов нет.
        puppets.ready_nodes = lambda: set()
        refusal("mop stat on an empty pool", run(stat.main),
                "no ready nodes in the pool")

        # Обычный ответ -- вывод прежний, символ в символ.
        row = {"name": "hyper", "driver": "pve", "serves": "mop", "state": "ready",
               "free_mb": 40960, "total_mb": 65536, "slots": 5}
        bus.ask_cluster = lambda verb, **kw: {"nodes": [row]}
        got = run(node.main)
        want = ("NODE   DRIVER  SERVES  STATE  FREE   TOTAL  SLOTS\n"
                "hyper  pve     mop     ready  40 GB  64 GB  5\n", "", 0)
        if got != want:
            failed += 1
            print(f"FAIL mop node on a normal answer: {got!r}")
        puppets.ready_nodes = keep[4]
        if puppets.pool() != [row]:
            failed += 1
            print(f"FAIL pool on a normal answer: {puppets.pool()!r}")
    finally:
        (bus.ask_cluster, bus.request_many, bus.connect, bus.nats.connect,
         puppets.ready_nodes) = keep[:5]
        if keep[5] is None:
            os.environ.pop("MOP_SERVER_LAN", None)
        else:
            os.environ["MOP_SERVER_LAN"] = keep[5]
    return failed


def check_refusals():
    """#146: отказ сервиса кластера -- исключение, а не поле, которое каждый
    командлет проверял сам.

    HYPOTHESIS: около двадцати мест делали `got = bus.ask_cluster(...); if
    got.get("error")` и сообщали отказ четырьмя способами; lib.require_job,
    alloc_of, running_alloc выходили из процесса, и MCP держал свою копию
    «аллокация должна работать» -- mcp.puppet_alloc, которая отказ сервиса
    («не твой папет», «нет такого») читала как «не размещён» и теряла причину.
    SOLUTION: bus.Refused и строгий bus.call_cluster; puppets.running_alloc
    бросает LookupError с причиной; диспетчер и loud в MCP превращают
    исключение в одну строку.
    RESULT: командлеты зовут bus.call_cluster и puppets.running_alloc,
    lib.guard спрашивает шину один раз и отдаёт спеку.
    STATUS: FIXED — see #146"""
    import contextlib
    import io
    from mop import bus, puppets

    failed = 0
    reason = "pu-mop-9 belongs to project other, not to mop"

    # Строгий вызов: отказ -- Refused с причиной как есть, ответ -- как есть.
    keep = bus.ask_cluster
    try:
        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        try:
            got = bus.call_cluster("alloc", name="pu-mop-9")
            failed += 1
            print(f"FAIL call_cluster must raise Refused on a refusal, got {got!r}")
        except bus.Refused as e:
            if str(e) != reason:
                failed += 1
                print(f"FAIL call_cluster lost the reason: {e!r}")
        if not issubclass(bus.Refused, RuntimeError):
            failed += 1
            print("FAIL Refused must be a RuntimeError: callers catch that already")
        bus.ask_cluster = lambda verb, **kw: {"ok": True, "verb": verb, **kw}
        got = bus.call_cluster("spec", name="pu-mop-9")
        if got != {"ok": True, "verb": "spec", "name": "pu-mop-9",
                   "timeout": bus.TIMEOUT, "project": None}:
            failed += 1
            print(f"FAIL call_cluster must pass the answer through: {got!r}")

        # running_alloc: отказ сервиса доходит причиной, а не «не размещён».
        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        try:
            puppets.running_alloc("pu-mop-9")
            failed += 1
            print("FAIL running_alloc must raise on a refusal")
        except LookupError:
            failed += 1
            print("FAIL running_alloc read a refusal as a missing allocation")
        except bus.Refused as e:
            if str(e) != reason:
                failed += 1
                print(f"FAIL running_alloc lost the reason: {e!r}")
        # Не работает -- LookupError, у падающего -- с причиной падения.
        task = {"state": "pending", "failed": False, "restarts": 3, "exit": 1,
                "next_s": 20}
        for alloc, want in ((None, "pu-mop-9 not running"),
                            ({"ClientStatus": "pending", "NodeName": "n1", "task": task,
                              "reason": "no bus credentials"},
                             "pu-mop-9: FAILED: no bus credentials")):
            bus.ask_cluster = lambda verb, a=alloc, **kw: {"ok": True, "alloc": a}
            try:
                got = puppets.running_alloc("pu-mop-9")
                failed += 1
                print(f"FAIL running_alloc({alloc!r}) must raise, got {got!r}")
            except LookupError as e:
                if not str(e).startswith(want):
                    failed += 1
                    print(f"FAIL running_alloc: {e!r}, want {want!r}...")
        live = {"ClientStatus": "running", "NodeName": "n1"}
        bus.ask_cluster = lambda verb, **kw: {"ok": True, "alloc": live}
        if puppets.running_alloc("pu-mop-9") != live:
            failed += 1
            print("FAIL running_alloc must return the running allocation")

        # guard: один вопрос шине, и его ответ -- вызывающему, а не второй
        # такой же запрос следом (update спрашивал spec дважды).
        asked = []
        spec = {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}
        bus.ask_cluster = lambda verb, **kw: asked.append(verb) or spec
        keep_project, bus.PROJECT = bus.PROJECT, "mop"
        try:
            got = lib.guard("pu-mop-9")
        finally:
            bus.PROJECT = keep_project
        if asked != ["spec"] or got != spec:
            failed += 1
            print(f"FAIL guard: asked {asked}, returned {got!r}")
    finally:
        bus.ask_cluster = keep

    # Диспетчер: отказ -- ровно одна строка в stderr с причиной, ненулевой
    # выход, в stdout ничего: stdout модель читает как ответ команды.
    for exc in (bus.Refused(reason), LookupError(reason)):
        def fn(_argv, exc=exc):
            raise exc
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.run(fn, [])
            except SystemExit as e:
                code = e.code
        name = type(exc).__name__
        if not isinstance(code, int) or code == 0:
            failed += 1
            print(f"FAIL run({name}): exit {code!r}, want a non-zero int")
        if err.getvalue() != reason + "\n":
            failed += 1
            print(f"FAIL run({name}): stderr {err.getvalue()!r}, want one line")
        if out.getvalue():
            failed += 1
            print(f"FAIL run({name}): stdout {out.getvalue()!r}")

    # Своих копий больше нет: помощники, выходившие из процесса, и ручные
    # проверки поля error у сервиса кластера в командлетах.
    for gone in ("require_job", "alloc_of", "running_alloc", "running_node"):
        if hasattr(lib, gone):
            failed += 1
            print(f"FAIL lib.{gone} must be gone: puppets.running_alloc / bus.call_cluster")
    # Два исключения: у ping в cluster check поле error рядом с ok -- это
    # «сервис жив, Nomad нет», а не отказ; deploy отдаёт ответ целиком
    # projects.for_deploy, и отказ там -- заметка, а не конец прогона.
    allowed = {os.path.join("cluster", "check.py"), os.path.join("pool", "deploy.py")}
    for root, _, files in os.walk(cli.PACKAGE):
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, cli.PACKAGE)
            if not f.endswith(".py") or rel in allowed:
                continue
            with open(path) as fh:
                if "bus.ask_cluster(" in fh.read():
                    failed += 1
                    print(f"FAIL {rel}: bus.ask_cluster by hand, use bus.call_cluster")
    return failed


# ── #159: правила вывода ──────────────────────────────────────────────────────
# HYPOTHESIS: капс для выразительности (THIS IS THE ONLY CHANNEL, ONLY, FREE,
# ORPHAN), эхо параметров («restarting X on Y...») и советы «run X» в успешном
# выводе. Вывод читает модель через MCP: крик и советы — шум, а успешная быстрая
# команда отвечает ей `done`.
# SOLUTION: слова переписаны, быстрые команды на успехе молчат, отказ — в stderr.
#
# Капс допустим только для того, что заглавное буквально: статусы, протоколы,
# идентификаторы, заголовки колонок, плейсхолдеры в usage. Список закрыт:
# новое слово в нём — осознанное решение, а не молчаливое разрешение.
CAPS_OK = {
    # статусы, которые читают глазами и модель
    "FAILED", "AGENT", "SILENT", "DELIVERED", "HUNG", "MUST", "SHOULD",
    # протоколы, сигналы, литералы чужих программ
    "JSON", "NATS", "PATH", "PYTHONPATH", "HEAD", "TERM", "SIGHUP", "VMID",
    "PLAY", "RECAP",
    # идентификаторы в тексте
    "SECTIONS",
    # плейсхолдеры в строках usage
    "ADDRESS", "ADDR", "PROFILE", "TEXT", "VALUE", "LOGIN", "NAME",
    # заголовки колонок, названные в тексте
    "MASTER", "OWNER", "CACHE",
}
CAPS_WORD = r"(?<![A-Za-z0-9_$.{/-])[A-Z]{4,}(?![A-Za-z0-9_])(?!\.md)"


def caps_hits(files):
    """Слова капсом в пользовательских строках: литералы и докстринги модулей
    (usage и описания инструментов MCP). Докстринги функций — русские
    комментарии, строки без единой строчной буквы — заголовки таблиц и токены."""
    import ast
    import re
    hits = []
    for f in files:
        with open(f) as fh:
            tree = ast.parse(fh.read())
        skip = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                    and isinstance(n.body[0], ast.Expr) \
                    and isinstance(n.body[0].value, ast.Constant):
                skip.add(id(n.body[0].value))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in skip:
                continue
            if not re.search(r"[a-zа-я]", n.value):
                continue
            for m in re.finditer(CAPS_WORD, n.value):
                if m.group() not in CAPS_OK:
                    hits.append(f"{os.path.relpath(f, ROOT)}:{n.lineno}: {m.group()}")
    return hits


def silent_run(main, argv):
    """(stdout, stderr, код) командлета."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv) or 0
        except SystemExit as e:
            code = e.code
    return out.getvalue(), err.getvalue(), code


def check_output_rules():
    """STATUS: FIXED — see #159"""
    import glob
    import re
    from mop import bus, keys, llm, puppets
    failed = 0
    files = sorted(glob.glob(os.path.join(ROOT, "mop", "cli", "**", "*.py"), recursive=True)) \
        + [os.path.join(ROOT, "mop", "channel.py"), os.path.join(ROOT, "mop", "agent.py")]
    for hit in caps_hits(files):
        failed += 1
        print(f"FAIL caps for emphasis: {hit}")

    os.environ.setdefault("MOP_SERVER_LAN", "10.0.0.1")
    keep = (bus.call_cluster, puppets.running_alloc, lib.guard, puppets.diagnose,
            keys.llm_keys_blob, llm.profiles)
    try:
        bus.call_cluster = lambda verb, **kw: {"ok": True}
        puppets.running_alloc = lambda name: {"ClientStatus": "running", "NodeName": "n1"}
        lib.guard = lambda name: {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}
        # Быстрая команда на успехе молчит: MCP ответит модели `done`.
        from mop.cli.core import restart
        from mop.cli.node import drain, forget
        for what, main, argv in (("restart", restart.main, ["pu-mop-1"]),
                                 ("node drain", drain.main, ["n1"]),
                                 ("node forget", forget.main, ["n1"])):
            out, err, code = silent_run(main, argv)
            if out or err or code:
                failed += 1
                print(f"FAIL {what} must be silent on success: out {out!r} err {err!r} code {code!r}")
        from mop.cli.pool import doctor
        from mop.cli.pool import llm as llm_cmd
        # Здоровый пул — одна строка результата, её и показываем.
        puppets.diagnose = lambda: []
        out, _, _ = silent_run(doctor.main, [])
        if out != "pool is healthy: nothing stuck\n":
            failed += 1
            print(f"FAIL doctor on a healthy pool: {out!r}")
        # Советов «run X» в успешном выводе нет: таблица уже говорит [restart].
        puppets.diagnose = lambda: [{"name": "pu-mop-1", "alloc": {"NodeName": "n1"},
                                     "diagnosis": "HUNG (not responding)", "action": "restart"}]
        out, _, _ = silent_run(doctor.main, [])
        keys.llm_keys_blob = lambda: ("", None)
        llm.profiles = lambda: {"glm": {"key": "Z_AI_KEY", "doc": "", "env": {}}}
        out2, _, _ = silent_run(llm_cmd.main, [])
        for what, text in (("doctor", out), ("llm", out2)):
            if re.search(r"\bmop [a-z]+", text):
                failed += 1
                print(f"FAIL {what} advises another command on success: {text!r}")
    finally:
        (bus.call_cluster, puppets.running_alloc, lib.guard, puppets.diagnose,
         keys.llm_keys_blob, llm.profiles) = keep
    failed += check_output_rest()
    return failed


def check_output_rest():
    """Остаток #159: setup, sweep, driver build. STATUS: FIXED — see #159"""
    import shutil
    import subprocess
    from mop import bus, image, playvars, puppets
    from mop.cli.driver import build
    from mop.cli.pool import setup, sweep
    failed = 0

    # setup: шаг установки — строка хода на терминале, не на терминале
    # тишина; предупреждение про claude — в stderr, stdout пуст.
    keep = (shutil.which, subprocess.run, playvars.playbook_vars)
    ran = []
    try:
        shutil.which = lambda cmd: "/usr/bin/sudo" if cmd == "sudo" else None
        subprocess.run = lambda args, **kw: ran.append(args) or subprocess.CompletedProcess(args, 0)
        playvars.playbook_vars = lambda: {}
        out, err, code = silent_run(setup.main, ["--operator"])
    finally:
        shutil.which, subprocess.run, playvars.playbook_vars = keep
    if out or code or "claude is not in PATH" not in err or len(ran) != 3:
        failed += 1
        print(f"FAIL setup: stdout {out!r}, stderr {err!r}, code {code!r}, ran {len(ran)} commands")

    # sweep: нечего убирать — успех, и он молчит.
    keep = (puppets.ready_nodes, bus.request_many, puppets.jobs, puppets.classify_junk)
    try:
        puppets.ready_nodes = lambda: {"n1"}
        bus.request_many = lambda asks, **kw: {"n1": {"bodies": [], "templates": []}}
        puppets.jobs = lambda *a, **kw: [{"ID": "pu-a-1"}]
        puppets.classify_junk = lambda answers, known: []
        out, err, code = silent_run(sweep.main, [])
    finally:
        puppets.ready_nodes, bus.request_many, puppets.jobs, puppets.classify_junk = keep
    if out or err or code:
        failed += 1
        print(f"FAIL sweep with nothing to sweep must be silent: {out!r} {err!r} {code!r}")

    # driver build: без эха того, откуда прочитан .mop.
    keep = (image.prepare, image.build)
    try:
        image.prepare = lambda origin, root: {"project": "p", "asks": {}, "alien": [], "legacy": []}
        image.build = lambda origin, got, **kw: {"gone": [], "announced": [], "rc": 0}
        out, err, code = silent_run(build.main, ["git@h:g/p.git"])
    finally:
        image.prepare, image.build = keep
    if out or code:
        failed += 1
        print(f"FAIL driver build echoes on success: {out!r} code {code!r}")
    failed += check_output_179()
    failed += check_output_182()
    failed += check_restore_all_188()
    return failed


def check_output_179():
    """HYPOTHESIS (#179): после #159 остались отказы `mop sweep` в stdout (а
    «no ready nodes» ещё и с кодом 0 -- отказ читался успехом),
    предупреждения `mop driver build` в stdout и эхо `mop update` на успехе.
    SOLUTION: отказы и предупреждения -- в stderr теми же словами, «no ready
    nodes» -- код 1, update на успехе молчит (MCP ответит done, как restart).
    STATUS: FIXED — see #179"""
    from mop import bus, image, keys, llm, puppets
    from mop.cli.core import update
    from mop.cli.driver import build
    from mop.cli.pool import sweep
    failed = 0

    def expect(what, got, text, nonzero):
        nonlocal failed
        out, err, code = got
        if out or text not in err or (bool(code) != nonzero):
            failed += 1
            print(f"FAIL {what}: stdout {out!r}, stderr {err!r}, code {code!r}")

    keep = (puppets.ready_nodes, bus.request_many, puppets.jobs, puppets.classify_junk)
    try:
        puppets.ready_nodes = lambda: set()
        expect("sweep with no ready nodes", silent_run(sweep.main, []),
               "no ready nodes", True)
        puppets.ready_nodes = lambda: {"n1"}
        bus.request_many = lambda asks, **kw: {"n1": None}
        expect("sweep where no node answered", silent_run(sweep.main, []),
               "no node answered", True)
        bus.request_many = lambda asks, **kw: {"n1": {"bodies": [], "templates": []}}
        puppets.jobs = lambda *a, **kw: []
        expect("sweep where Nomad lists no puppets", silent_run(sweep.main, []),
               "Nomad lists no puppets", True)
        # Промолчавший узел при ответившем соседе: отказ по нему -- тоже в
        # stderr, а код выхода за него отвечает.
        bus.request_many = lambda asks, **kw: {"n1": {"bodies": [], "templates": []},
                                               "n2": None}
        puppets.ready_nodes = lambda: {"n1", "n2"}
        puppets.jobs = lambda *a, **kw: [{"ID": "pu-a-1"}]
        puppets.classify_junk = lambda answers, known: []
        expect("sweep with one silent node", silent_run(sweep.main, []),
               "n2: no response", True)
    finally:
        puppets.ready_nodes, bus.request_many, puppets.jobs, puppets.classify_junk = keep

    keep = (image.prepare, image.build)
    try:
        image.prepare = lambda origin, root: {"project": "p", "asks": {},
                                              "alien": ["MOP_X"], "legacy": [".mop.yaml"]}
        image.build = lambda origin, got, **kw: {"gone": [], "announced": [], "rc": 0}
        out, err, code = silent_run(build.main, ["git@h:g/p.git"])
    finally:
        image.prepare, image.build = keep
    if out or code or "MOP_X is not a project's to set — ignored" not in err \
            or "read as .mop/sandbox.yaml for the transition" not in err:
        failed += 1
        print(f"FAIL driver build warnings must go to stderr: {out!r} {err!r} {code!r}")

    calls = []
    keep = (lib.guard, bus.call_cluster, keys.push_llm_keys, lib.workspace_text)
    try:
        lib.guard = lambda name: {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}
        bus.call_cluster = lambda verb, **kw: calls.append(verb) or {"ok": True}
        keys.push_llm_keys = lambda profile: None
        lib.workspace_text = lambda origin: ""
        for argv in (["pu-mop-1"], ["pu-mop-1", "--fresh"], ["pu-mop-1", "git@h:g/other.git"]):
            calls.clear()
            out, err, code = silent_run(update.main, argv)
            if out or err or code or calls != ["update"]:
                failed += 1
                print(f"FAIL update {argv} must be silent on success: out {out!r} "
                      f"err {err!r} code {code!r} calls {calls}")
    finally:
        lib.guard, bus.call_cluster, keys.push_llm_keys, lib.workspace_text = keep
    return failed



def check_output_182():
    """HYPOTHESIS (#182): после #179 `mop driver build` эхом печатал
    содержимое манифеста («asks for k=v»), отчёт о пересозданных телах и
    объявлении образа, а `mop cluster users` -- «changed/unchanged».
    SOLUTION: сборка показывает шаги (lib.Progress: строка на терминале,
    тишина не на терминале), факты -- шагами, а не отчётом; отказ подъёма
    папета и объявления образа -- stderr с именем; users на успехе молчит.
    STATUS: FIXED — see #182"""
    from mop import bootstrap, image, natsconf, nomad, projects, spec
    from mop.cli.cluster import users
    from mop.cli.driver import build
    failed = 0
    undo = no_network()
    through = lambda main, argv: silent_run(lambda a: cli.run(main, a), argv)
    keep = (image.prepare, image.build, image.clear, image.bake, image.announce,
            nomad.register, spec.job_spec, projects.read, natsconf.apply,
            natsconf.reload)
    try:
        got = {"project": "p", "asks": {"MOP_CORES": "8"}, "alien": [], "legacy": []}
        image.prepare = lambda origin, root: got
        gone = [{"name": "pu-p-1", "origin": "git@h:g/p.git", "llm": "claude", "node": "hyper"}]
        image.clear = lambda project, force=False: list(gone)
        image.bake = lambda *a, **kw: 0
        image.announce = lambda project: [("hyper", "announced, serves p"),
                                          ("gpu", "not a container node"),
                                          ("old", "already announced")]
        spec.job_spec = lambda *a, **kw: {}
        nomad.register = lambda job: None

        # Успех не на терминале -- тишина: ни эха манифеста, ни отчёта.
        out, err, code = through(build.main, ["git@h:g/p.git"])
        if out or err or code:
            failed += 1
            print(f"FAIL driver build must be silent on success off a TTY: "
                  f"out {out!r} err {err!r} code {code!r}")

        # Папет не поднялся -- громко, с именем и причиной.
        def refuse(job):
            raise RuntimeError("Nomad said no")
        nomad.register = refuse
        out, err, code = through(build.main, ["git@h:g/p.git"])
        if out or not code or "pu-p-1" not in err or "Nomad said no" not in err:
            failed += 1
            print(f"FAIL a failed re-raise must reach stderr with the puppet: "
                  f"out {out!r} err {err!r} code {code!r}")
        nomad.register = lambda job: None

        # Узел, которому образ объявить не вышло (#175), -- stderr.
        image.announce = lambda project: [("hyper", "announced, serves p"),
                                          ("bad", "bad: unknown driver 'bogus'")]
        out, err, code = through(build.main, ["git@h:g/p.git"])
        if out or "bad: unknown driver 'bogus'" not in err or "hyper" in err:
            failed += 1
            print(f"FAIL a failed announcement must reach stderr alone: "
                  f"out {out!r} err {err!r} code {code!r}")

        # cluster users на успехе молчит -- и с --reload.
        projects.read = lambda: ["git@h:g/p.git"]
        natsconf.apply = lambda names, creds: (True, None)
        natsconf.reload = lambda: None
        for argv in ([], ["--reload"]):
            out, err, code = through(users.main, argv)
            if out or err or code:
                failed += 1
                print(f"FAIL cluster users {argv} must be silent on success: "
                      f"out {out!r} err {err!r} code {code!r}")
    finally:
        (image.prepare, image.build, image.clear, image.bake, image.announce,
         nomad.register, spec.job_spec, projects.read, natsconf.apply,
         natsconf.reload) = keep
        undo()
    return failed


def check_restore_all_188():
    """HYPOTHESIS (#188): image.restore поднимал папетов до первого отказа
    Nomad; остальные снятые в `gone` не пробовались вовсе, и об этом не
    говорил никто -- пересборка оставляла их лежать.
    SOLUTION: пробовать каждого, отказы собрать и бросить одним исключением,
    по строке на папета («<папет> on <узел>: not raised again: <причина>»).
    STATUS: FIXED — see #188"""
    from mop import image, nomad, spec
    failed = 0
    undo = no_network()
    keep = (nomad.register, spec.job_spec)
    gone = [{"name": f"pu-p-{i}", "origin": "git@h:g/p.git", "llm": "claude",
             "node": "hyper"} for i in (1, 2, 3)]
    try:
        spec.job_spec = lambda name, origin, llm: {"ID": name}
        for refused in ({"pu-p-1"}, {"pu-p-1", "pu-p-3"}, set()):
            registered = []

            def register(job, refused=refused):
                if job["ID"] in refused:
                    raise RuntimeError(f"Nomad refused {job['ID']}")
                registered.append(job["ID"])
            nomad.register = register
            try:
                image.restore(gone)
                err = None
            except RuntimeError as e:
                err = str(e)
            want = [p["name"] for p in gone if p["name"] not in refused]
            if registered != want:
                failed += 1
                print(f"FAIL restore with {sorted(refused)} refused registered "
                      f"{registered}, wanted {want}")
            if not refused:
                if err is not None:
                    failed += 1
                    print(f"FAIL restore with nothing refused raised: {err!r}")
                continue
            lines = (err or "").splitlines()
            want_lines = [f"{n} on hyper: not raised again: Nomad refused {n}"
                          for n in sorted(refused)]
            if lines != want_lines:
                failed += 1
                print(f"FAIL restore with {sorted(refused)} refused: {err!r}, "
                      f"wanted {want_lines}")
    finally:
        nomad.register, spec.job_spec = keep
        undo()
    return failed


def check_empty_llm():
    """HYPOTHESIS (#164): `--llm` без значения давал профиль "", и отказ
    llm.require звучал как «no LLM profile (empty)» — не про флаг, который
    забыли заполнить. SOLUTION: пустое значение — ошибка использования в
    parse_llm, до реестра профилей. STATUS: FIXED — see #164"""
    failed = 0

    def through_dispatcher(argv):
        return silent_run(lambda a: cli.run(lambda x: lib.parse_llm(x) and None, a), argv)
    for argv in (["--llm", ""], ["pu-mop-1", "--llm"], ["--llm="], ["--llm", "--fresh"]):
        out, err, code = through_dispatcher(argv)
        lines = err.strip().splitlines()
        if out or not code or len(lines) != 1 or "Traceback" in err \
                or not lines[0].startswith("--llm needs a profile name"):
            failed += 1
            print(f"FAIL parse_llm({argv}): out {out!r} err {err!r} code {code!r}")
    got = lib.parse_llm(["pu-mop-1", "--llm", "claude"])
    if got != ("claude", ["pu-mop-1"]):
        failed += 1
        print(f"FAIL parse_llm with a profile: {got!r}")
    if lib.parse_llm(["pu-mop-1"]) != (None, ["pu-mop-1"]):
        failed += 1
        print("FAIL parse_llm without --llm must leave the profile unset")
    # Неизвестный профиль — прежний отказ, слово в слово.
    out, err, code = through_dispatcher(["--llm", "no-such"])
    if not code or not err.startswith("no LLM profile no-such; available: "):
        failed += 1
        print(f"FAIL an unknown profile keeps its refusal: {err!r} {code!r}")
    return failed


# ── #186: драйвер узла из инвентаря — до плейбука ────────────────────────────
# Вывод настоящего `ansible-inventory --list` (ansible-core 2.21) на инвентаре:
#   puppet: plain (без переменной), explicit (mop_driver: host), hyper,
#           typo (mop_driver: pvee); proxmox: hyper, vars mop_driver: pve;
#   server: localhost.
# Хост без своих переменных есть только в списке группы, в _meta.hostvars
# его нет, — и драйвер у него MOP_DRIVER, как у `mop_driver | default(MOP_DRIVER)`
# в шаблонах.
INVENTORY_TYPO = {
    "_meta": {"hostvars": {
        "explicit": {"mop_driver": "host"},
        "hyper": {"mop_driver": "pve", "mop_pve_vmid_base": "9000"},
        "localhost": {"ansible_connection": "local"},
        "typo": {"mop_driver": "pvee"}}},
    "all": {"children": ["ungrouped", "server", "puppet", "proxmox"]},
    "proxmox": {"hosts": ["hyper"]},
    "puppet": {"hosts": ["plain", "explicit", "hyper", "typo"]},
    "server": {"hosts": ["localhost"]},
}
INVENTORY_CLEAN = {**INVENTORY_TYPO, "_meta": {"hostvars": {
    **INVENTORY_TYPO["_meta"]["hostvars"], "typo": {"mop_driver": "pve"}}}}


def check_inventory_drivers():
    """HYPOTHESIS (#186): опечатка в mop_driver инвентаря доезжала до meta
    Nomad, и ловили её только читатели (driver.of_node, #175). SOLUTION: deploy
    прогоняет драйвер каждого хоста через of_node до плейбука.
    STATUS: FIXED — see #186"""
    from mop.cli.pool import deploy
    failed = 0
    fn = getattr(deploy, "driver_refusals", None)
    got = fn(INVENTORY_TYPO, "host") if fn else None
    if got is None or len(got) != 1 or not got[0].startswith("typo: unknown driver 'pvee'"):
        failed += 1
        print(f"FAIL driver_refusals: {got!r}, wanted one refusal for typo/pvee")
    if fn and fn(INVENTORY_CLEAN, "host"):
        failed += 1
        print(f"FAIL a clean inventory refused: {fn(INVENTORY_CLEAN, 'host')!r}")
    # Пусто и нет ключа -- драйвер по умолчанию, как у of_node; а битый
    # MOP_DRIVER ловится на тех, кому он достаётся.
    if fn:
        bad = fn(INVENTORY_CLEAN, "pvee")
        if sorted(r.split(":")[0] for r in bad) != ["localhost", "plain"]:
            failed += 1
            print(f"FAIL a broken MOP_DRIVER must name the hosts that inherit it: {bad!r}")
        empty = {**INVENTORY_CLEAN, "_meta": {"hostvars": {"plain": {"mop_driver": ""}}}}
        if fn(empty, "host"):
            failed += 1
            print("FAIL an empty mop_driver is the default driver, not a refusal")
    return failed


def check_node_memory_197():
    """HYPOTHESIS (#197): строка mop_mem_mb в инвентаре не работает нигде:
    на host-узле спеку строит сервер своим MOP_MEM_MB (и #190 отказывал),
    на pve память тела шла из образа, то есть из `.mop`. SOLUTION: память --
    свойство папета (дефолт -> .env -> `.mop`), строку deploy отвергает на
    любом узле; потолок узла mop_body_mem_cap_mb остаётся узловым и едет в
    meta Nomad, где `>=` сравнивает численно только целые -- поэтому потолок
    не из одного целого числа тоже отказ.
    STATUS: FIXED — see #197"""
    from mop.cli.pool import deploy
    failed = 0
    undo = no_network()
    try:
        fn = getattr(deploy, "memory_refusals", None)
        if fn is None:
            print("FAIL no deploy.memory_refusals")
            return 1

        def listing(hostvars, groups=None):
            out = {"_meta": {"hostvars": hostvars},
                   "puppet": {"hosts": ["plain", "hyper", "odd"]}}
            out.update(groups or {})
            return out
        cases = [
            ("clean", listing({"hyper": {"mop_driver": "pve"}}), "32768", []),
            ("mop_mem_mb on a host node", listing({"odd": {"mop_mem_mb": "12288"}}), "32768",
             ["odd: mop_mem_mb"]),
            ("mop_mem_mb on a pve node", listing({"hyper": {"mop_driver": "pve", "mop_mem_mb": 8192}}),
             "32768", ["hyper: mop_mem_mb"]),
            # Как на mop.corp.ermak.dev: строка в группе puppet. ansible-inventory
            # --list сводит её в hostvars каждого хоста; --export оставил бы в
            # vars группы -- отказ обязан видеть оба места.
            ("mop_mem_mb in a group's vars",
             listing({}, {"puppet": {"hosts": ["plain"], "vars": {"mop_mem_mb": "8192"}}}),
             "32768", ["puppet: mop_mem_mb"]),
            ("a node's cap is a node setting", listing({"hyper": {"mop_body_mem_cap_mb": "65536"}}),
             "32768", []),
            ("a node's cap from YAML as a number", listing({"hyper": {"mop_body_mem_cap_mb": 65536}}),
             "32768", []),
            ("a node's cap in gigabytes", listing({"hyper": {"mop_body_mem_cap_mb": "32G"}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            ("a node's cap with a space", listing({"hyper": {"mop_body_mem_cap_mb": " 32768"}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            ("a node's cap as a fraction", listing({"hyper": {"mop_body_mem_cap_mb": "1.5"}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            ("an empty cap", listing({"hyper": {"mop_body_mem_cap_mb": ""}}),
             "32768", ["hyper: mop_body_mem_cap_mb"]),
            # Потолок установки достаётся каждому хосту без своей строки.
            ("the installation's cap is broken", listing({"hyper": {"mop_body_mem_cap_mb": "4096"}}),
             "32G", ["MOP_BODY_MEM_CAP_MB"]),
        ]
        for what, lst, cap, want in cases:
            got = fn(lst, cap)
            heads = sorted(g.split("=")[0] for g in got)
            if heads != sorted(want):
                failed += 1
                print(f"FAIL memory_refusals, {what}: {got!r}, wanted lines for {want}")
        got = fn(listing({"odd": {"mop_mem_mb": "12288"}}), "32768")
        text = " ".join(got)
        if not ("not a node setting" in text and ".env" in text and ".mop" in text
                and "remove the line" in text):
            failed += 1
            print(f"FAIL memory_refusals must say where memory comes from now: {got!r}")
    finally:
        undo()
    return failed


def check_pool_uniform():
    """HYPOTHESIS (#190): спеку папета строит сервер своими MOP_HOME,
    MOP_USER, MOP_PUPPET_SEED, MOP_MEM_MB, а агент и `mop driver run` на узле
    читают их из node.env этого узла. Строка хоста в инвентаре, перекрывшая
    любую из них, молча разводит врапер и агента: клон ложится туда, куда
    агент не смотрит. SOLUTION: config.POOL_UNIFORM и отказ deploy до
    плейбука, если хост задаёт им значение, отличное от установки.
    STATUS: FIXED — see #190

    MOP_MEM_MB здесь больше нет (#197): память -- свойство папета, не узла,
    и строку mop_mem_mb в инвентаре отвергает check_node_memory_197 на любом
    узле, с любым значением."""
    from mop import config
    from mop.cli.pool import deploy
    failed = 0
    undo = no_network()
    try:
        uniform = getattr(config, "POOL_UNIFORM", None)
        if set(uniform or ()) != {"MOP_HOME", "MOP_USER", "MOP_PUPPET_SEED"}:
            failed += 1
            print(f"FAIL config.POOL_UNIFORM {uniform!r}: paths and user on every host")
        if hasattr(config, "HOST_UNIFORM"):
            failed += 1
            print("FAIL config.HOST_UNIFORM is gone with #197: MOP_MEM_MB is not a node setting")
        fn = getattr(deploy, "uniform_refusals", None)
        installed = {n: config.get(n) for n in (uniform or ())}

        def listing(hostvars):
            return {"_meta": {"hostvars": hostvars},
                    "puppet": {"hosts": ["plain", "hyper", "odd"]}}
        cases = [
            ("an override", {"odd": {"mop_home": "/srv/elsewhere"}}, ["odd: mop_home"]),
            ("two settings on one host", {"odd": {"mop_user": "someone",
                                                  "mop_home": "/srv/x"}},
             ["odd: mop_home", "odd: mop_user"]),
            ("the same value", {"odd": {"mop_home": installed.get("MOP_HOME")}}, []),
            ("no override", {"hyper": {"mop_driver": "pve"}}, []),
            # Память -- не этой проверки (#197): её отвергает своя.
            ("mop_mem_mb is not a uniform setting", {"odd": {"mop_mem_mb": "8192"}}, []),
            # Пути и пользователь -- общие и для контейнерного узла.
            ("mop_home on a pve node", {"hyper": {"mop_driver": "pve", "mop_home": "/srv/x"}},
             ["hyper: mop_home"]),
        ]
        for what, hv, want in cases:
            got = fn(listing(hv), installed, "host") if fn else None
            heads = sorted(g.split("=")[0] for g in got) if got is not None else None
            if heads != want:
                failed += 1
                print(f"FAIL uniform_refusals, {what}: {got!r}, wanted lines for {want}")
    finally:
        undo()
    return failed


def check_deploy_check():
    """HYPOTHESIS (#177): доказать, что правка deploy/ не меняет узлы, было
    нечем — `mop deploy` аргументов не берёт, и #157 подкладывал на PATH
    обёртку ansible-playbook. А после плейбука deploy пишет файлы
    (creds.collect). SOLUTION: `mop deploy --check` — --check --diff каждому
    ansible-playbook, и ничего пишущего после плейбука. STATUS: FIXED — see #177"""
    import shutil
    import subprocess
    from mop import bus, config, creds, playvars, projects
    from mop.cli.pool import deploy
    failed = 0
    d = tempfile.mkdtemp(prefix="mop-test-deploy-check-")
    inventory, key = os.path.join(d, "inventory.yaml"), os.path.join(d, "id")
    for f in (inventory, key, key + ".pub"):
        open(f, "w").close()
    calls, collected, checked = [], [], []
    listing = [INVENTORY_CLEAN]
    undo = no_network()
    keep = (shutil.which, config.require, subprocess.call, subprocess.run, playvars.playbook_vars,
            deploy.missing_extras, deploy.link, deploy.manifests, deploy.check,
            bus.ask_cluster, projects.for_deploy, projects.read, creds.collect,
            creds.operator, dict(os.environ))
    try:
        shutil.which = lambda cmd: f"/usr/bin/{cmd}"
        config.require = lambda *a: None
        subprocess.call = lambda argv, **kw: calls.append(argv) or 0
        subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, json.dumps(listing[0]), "")
        playvars.playbook_vars = lambda: {"MOP_OPERATOR_SUBJECTS": ["x"]}
        deploy.missing_extras = lambda setting, root: []
        deploy.link = lambda name, target: None
        deploy.manifests = lambda origins: {}
        deploy.check = lambda: checked.append(1)
        bus.ask_cluster = lambda *a, **kw: {"ok": True, "projects": []}
        projects.for_deploy = lambda answer, local: ([], None)
        projects.read = lambda: []
        creds.collect = lambda *a, **kw: collected.append(1) or []
        creds.operator = lambda dest: "anton"
        os.environ.update({"INVENTORY": inventory, "MOP_GIT_KEY": key})
        for argv, dry in (([], False), (["--check"], True)):
            calls.clear(), collected.clear(), checked.clear()
            out, err, code = silent_run(deploy.main, argv)
            plays = [c for c in calls if c and c[0] == "ansible-playbook"]
            flags = {"--check", "--diff"} & {a for c in plays for a in c}
            if code or not plays or flags != ({"--check", "--diff"} if dry else set()):
                failed += 1
                print(f"FAIL deploy {argv}: code {code!r}, plays {plays!r}, err {err!r}")
            # #178: хосты инвентаря едут плейбуку -- их файлом кладёт роль
            # cluster, и по нему forget отказывает узлу, который deploy
            # поставил бы снова.
            sent = [json.loads(c[i + 1]) for c in plays for i, a in enumerate(c)
                    if a == "--extra-vars"]
            hosts = [v["mop_inventory_hosts"] for v in sent if "mop_inventory_hosts" in v]
            if hosts != [["explicit", "hyper", "localhost", "plain", "typo"]]:
                failed += 1
                print(f"FAIL deploy {argv} must send the inventory's hosts to the "
                      f"playbook (#178): {hosts!r}")
            if dry and collected:
                failed += 1
                print("FAIL deploy --check must not collect server credentials (it writes)")
            if not dry and not collected:
                failed += 1
                print("FAIL deploy without --check must still collect server credentials")
        # Прочие аргументы — по-прежнему отказ, и до плейбука.
        for argv in (["pool"], ["git@h:g/p.git"], ["--check", "extra"], ["--diff"]):
            calls.clear()
            out, err, code = silent_run(deploy.main, argv)
            if not code or calls:
                failed += 1
                print(f"FAIL deploy {argv} must be refused before any playbook: code {code!r}")
        # #190: перекрытая на хосте настройка, из которой сервер строит спеку.
        listing[0] = {**INVENTORY_CLEAN, "_meta": {"hostvars": {
            **INVENTORY_CLEAN["_meta"]["hostvars"], "plain": {"mop_home": "/srv/elsewhere"}}}}
        for argv in ([], ["--check"]):
            calls.clear(), collected.clear()
            out, err, code = silent_run(deploy.main, argv)
            plays = [c for c in calls if c and c[0] == "ansible-playbook"]
            lines = [lib.plain(l) for l in err.strip().splitlines()]
            if code != 1 or plays or len(lines) != 1 or not lines[0].startswith("plain: mop_home="):
                failed += 1
                print(f"FAIL deploy {argv} over a host override of MOP_HOME: code {code!r}, "
                      f"plays {plays!r}, err {err!r}")
        # #186: опечатка в драйвере хоста — отказ до плейбука, строка на хост.
        listing[0] = INVENTORY_TYPO
        for argv in ([], ["--check"]):
            calls.clear(), collected.clear()
            out, err, code = silent_run(deploy.main, argv)
            plays = [c for c in calls if c and c[0] == "ansible-playbook"]
            lines = [lib.plain(l) for l in err.strip().splitlines()]
            if code != 1 or plays or collected or len(lines) != 1 \
                    or not lines[0].startswith("typo: unknown driver 'pvee'"):
                failed += 1
                print(f"FAIL deploy {argv} over a driver typo: code {code!r}, "
                      f"plays {plays!r}, err {err!r}")
    finally:
        undo()
        (shutil.which, config.require, subprocess.call, subprocess.run, playvars.playbook_vars,
         deploy.missing_extras, deploy.link, deploy.manifests, deploy.check,
         bus.ask_cluster, projects.for_deploy, projects.read, creds.collect,
         creds.operator, env) = keep
        os.environ.clear()
        os.environ.update(env)
    return failed


class NetworkGuard(Exception):
    """Проверка попыталась выйти в сеть."""


def no_network():
    """Предохранитель от живой шины для проверки, которая гоняет командлет
    через cli.run (#169). Ниже mop, поэтому его не обходит ни перезагрузка
    модулей, ни прежний bus в атрибуте пакета: socket.connect (им идёт и
    asyncio-соединение nats-py) и nats.connect настоящего пакета бросают;
    креды папета (MOP_BUS_CONFIG) убраны, сервер -- TEST-NET 192.0.2.1.
    -> функция отката."""
    import socket

    def refuse(*a, **k):
        raise NetworkGuard("the check tried to reach the network")

    real_nats = sys.modules.get("nats")
    saved = {"sock": socket.socket.connect, "sock_ex": socket.socket.connect_ex,
             "nats": getattr(real_nats, "connect", None),
             "env": {k: os.environ.get(k) for k in ("MOP_BUS_CONFIG", "MOP_SERVER_LAN",
                                                      "MOP_SERVER_DIR")}}
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    if real_nats is not None:
        real_nats.connect = refuse
    os.environ.pop("MOP_BUS_CONFIG", None)
    os.environ.pop("MOP_SERVER_DIR", None)
    os.environ["MOP_SERVER_LAN"] = "192.0.2.1"

    def undo():
        socket.socket.connect = saved["sock"]
        socket.socket.connect_ex = saved["sock_ex"]
        if real_nats is not None:
            real_nats.connect = saved["nats"]
        for k, v in saved["env"].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return undo


def check_bus_import_169():
    """HYPOTHESIS (#169): mop/bus.py при импорте без nats-py зовёт sys.exit --
    библиотека кончает процесс сама, и тот, кто её импортировал, не может ни
    перехватить отказ, ни сказать его своими словами (#150: `-m mop.agent`
    печатал текст bus вместо своего).
    SOLUTION: bus бросает ImportError с тем же текстом; диспетчер импортирует
    командлет внутри cli.run, и run делает из ImportError одну строку в
    stderr и код 1.
    STATUS: FIXED — see #169"""
    return missing_library("nats", "mop.bus", "bus library required: pip install --user "
                           "--break-system-packages nats-py")


def check_nomad_import_187():
    """HYPOTHESIS (#187): mop/nomad.py при импорте без python-nomad зовёт
    sys.exit -- тот же дефект, что у bus в #169: библиотека кончает процесс
    сама.
    SOLUTION: как в #169 -- ImportError с тем же текстом, одну строку из него
    делает cli.run.
    STATUS: FIXED — see #187"""
    return missing_library("nomad", "mop.nomad", "API library required: pip install --user "
                           "--break-system-packages python-nomad")


def missing_library(lib, module, text):
    """Импорт module без библиотеки lib -- ImportError с текстом text, а
    командлет, которому module нужен, через cli.run -- одна строка в stderr и
    код 1. Модули перезагружаются: проверка идёт последней."""
    import importlib
    import importlib.abc
    failed = 0

    class NoLib(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == lib or name.startswith(lib + "."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    def evict(lib_too):
        # Из sys.modules И из атрибутов пакета: `from mop import bus` берёт
        # атрибут пакета, и без этого вернул бы прежний bus с настоящим nats.
        for name in list(sys.modules):
            if (lib_too and (name == lib or name.startswith(lib + "."))) or \
                    (name.startswith("mop.") and name != "mop.cli"):
                parent, _, child = name.rpartition(".")
                if parent in sys.modules and getattr(sys.modules[parent], child, None) \
                        is sys.modules[name]:
                    delattr(sys.modules[parent], child)
                del sys.modules[name]

    # Предохранитель -- до всего: однажды неполное выселение модулей отправило
    # из этой проверки настоящий restart на живую шину (отбит правами).
    undo = no_network()
    try:
        import socket
        socket.create_connection(("192.0.2.1", 4222), timeout=1)
        failed += 1
        print("FAIL the network guard did not hold")
        undo()
        return failed
    except NetworkGuard:
        pass
    keep = dict(sys.modules)
    blocker = NoLib()
    sys.meta_path.insert(0, blocker)
    try:
        evict(True)
        try:
            importlib.import_module(module)
            failed += 1
            print(f"FAIL importing {module} without {lib} must raise ImportError")
        except ImportError as e:
            if str(e) != text:
                failed += 1
                print(f"FAIL {module} without {lib}: {e!r}, wanted {text!r}")
        except SystemExit as e:
            failed += 1
            print(f"FAIL {module} without {lib} ends the process: SystemExit({e.code!r})")
        evict(False)
        # Командлет, которому нужна шина, через диспетчер: одна строка, код 1.
        # Без аргументов: если шина вдруг импортируется, командлет остановится
        # на usage, а не пойдёт в сеть.
        import contextlib
        import io
        err = io.StringIO()
        code = "no exit"
        with contextlib.redirect_stderr(err):
            try:
                cli.run(cli.command("mop.cli.core.restart"), [])
            except SystemExit as e:
                code = e.code
            except BaseException as e:
                code = f"escaped {type(e).__name__}"
        if code != 1 or err.getvalue() != text + "\n":
            failed += 1
            print(f"FAIL a commandlet without {lib} through cli.run: code {code!r}, "
                  f"stderr {err.getvalue()!r}")
    finally:
        undo()
        sys.meta_path.remove(blocker)
        sys.modules.clear()
        sys.modules.update(keep)
        # Пакеты держат подмодули и атрибутом: вернуть и их, иначе следующий
        # `from mop import x` достал бы копию из этой проверки.
        for name, mod in keep.items():
            if "." in name:
                parent, child = name.rsplit(".", 1)
                if parent in keep:
                    setattr(keep[parent], child, mod)
    return failed


def check_fallback_model_183():
    """HYPOTHESIS (#183): puppets.py читал MOP_FALLBACK_MODEL на уровне модуля,
    и всякий, кто импортирует puppets (сервис кластера), считался читающим
    эту настройку, хотя применяет её один treat() -- `mop doctor --fix` на
    машине оператора.
    SOLUTION: treat() читает её при вызове; на уровне модуля чтения нет.
    STATUS: FIXED — see #183"""
    import ast
    from mop import puppets
    failed = 0
    tree = ast.parse(open(os.path.join(ROOT, "mop", "puppets.py")).read())
    top = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                        ast.ClassDef))]
    if any("MOP_FALLBACK_MODEL" in ast.unparse(n) for n in top):
        failed += 1
        print("FAIL puppets.py reads MOP_FALLBACK_MODEL at import, not in treat()")
    typed = []
    undo = no_network()
    keep = (puppets.switch_model, os.environ.get("MOP_FALLBACK_MODEL"))
    try:
        # force -- лечение проходит ворота владения (#40).
        puppets.switch_model = lambda node, name, model, force=False: typed.append(model)
        os.environ["MOP_FALLBACK_MODEL"] = "sonnet-for-183"
        got = puppets.treat({"action": "model", "name": "pu-mop-1",
                             "alloc": {"NodeName": "n1"}})
    finally:
        puppets.switch_model = keep[0]
        if keep[1] is None:
            os.environ.pop("MOP_FALLBACK_MODEL", None)
        else:
            os.environ["MOP_FALLBACK_MODEL"] = keep[1]
        undo()
    if got != "/model sonnet-for-183" or typed != ["sonnet-for-183"]:
        failed += 1
        print(f"FAIL treat(model) must use MOP_FALLBACK_MODEL as it is at the call: "
              f"{got!r}, typed {typed}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
