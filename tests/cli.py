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
from _lib import (Checks, NetworkGuard, no_network, offline, patched,  # noqa: E402,F401
                  patched_env, restored, run_command)
sys.path.insert(0, ROOT)

from mop import cli  # noqa: E402
from mop.cli import lib  # noqa: E402
from mop.cli.core import _common  # noqa: E402
from mop.cli.server import _play  # noqa: E402
from mop.common import domain  # noqa: E402

# (каталог, имя, подпакет?) — то, что находит обход mop/cli.
FOUND = [
    ("core", "add", False), ("core", "list", False), ("pool", "deploy", False),
    ("service", "mcp", False), ("driver", "", True), ("bug", "", True),
    ("dev", "", True),
]
# Дерево глаголов (#253): модуль -- {}, подгруппа -- её дерево. Множество
# прежнего контракта сюда не годится: у подгруппы есть свои глаголы.
VERBS = {"driver": {"build": {}, "run": {}}, "bug": {},
         "dev": {"bug": {"new": {}, "list": {}}, "ci": {"list": {}}}}


def main():
    c = Checks()
    # HYPOTHESIS: каталога нет — диспетчер bash ищет файл по имени в bin/.
    # SOLUTION: catalog() из обхода пакета, resolve() по нему.
    # STATUS: FIXED — see #75
    cat = cli.catalog(FOUND)
    want = {"add": "mop.cli.core.add", "list": "mop.cli.core.list",
            "deploy": "mop.cli.pool.deploy", "mcp": "mop.cli.service.mcp",
            "driver": "mop.cli.driver", "bug": "mop.cli.bug", "dev": "mop.cli.dev"}
    c.expect("catalog", cat, want)
    # Одно имя в двух секциях — отказ, а не «кто первый».
    try:
        cli.catalog(FOUND + [("pool", "add", False)])
        c.fail("catalog: a duplicate name must refuse")
    except RuntimeError as e:
        c.check(f"catalog: the refusal must name the duplicate: {e}", not ("add" not in str(e)))

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
        # Вложенные группы (#253): спуск по дереву, пока следующий argv --
        # глагол; подгруппа без глагола отвечает сама, чужое слово -- ей же.
        (["dev", "bug", "new", "t"], ("mop.cli.dev.bug.new", ["t"])),
        (["dev", "bug"], ("mop.cli.dev.bug", [])),
        (["dev", "bug", "nope"], ("mop.cli.dev.bug", ["nope"])),
        (["dev", "x"], ("mop.cli.dev", ["x"])),
    ]:
        got = cli.resolve(argv, cat, VERBS)
        c.expect(f"resolve({argv})", got, want)
    c.check("resolve of an unknown name must be None", not (cli.resolve(["nope"], cat, VERBS) is not None))
    c.check("resolve of nothing must be None", not (cli.resolve([], cat, VERBS) is not None))

    # HYPOTHESIS (#140): строка хода одна, и строкам вывода сборки на
    # терминале негде жить. SOLUTION: frame() -- кадр из нескольких строк,
    # перерисовываемый на месте: вверх на прежнюю высоту, стереть до конца
    # экрана, новые строки, обрезанные по ширине, чтобы перенос не сбил счёт.
    E = "\033"
    got = lib.frame(0, ["one", "two", "step"], 80)
    c.expect("frame from nothing", got, f"\r{E}[Jone\ntwo\nstep")
    got = lib.frame(3, ["step"], 80)
    c.expect("frame over three rows", got, f"\r{E}[2A{E}[Jstep")
    c.expect("frame to nothing erases", lib.frame(1, [], 80), f"\r{E}[J")
    # Цвета и управляющие символы строки ansible не должны ни красить
    # кадр, ни сдвигать курсор; ширина -- по видимым символам.
    got = lib.frame(0, [f"{E}[0;32mok: [hyper]{E}[0m\tx\r", "abcdefghij"], 6)
    c.expect("frame must strip escapes and cut to width", got, f"\r{E}[Jok: [\nabcde")
    # STATUS: FIXED — see #140

    # Описание — первая строка докстринга, прочитанная без импорта: импорт
    # тянул бы шину и падал бы на машине без nats-py ради строки help.
    d = tempfile.mkdtemp(prefix="mop-test-cli-")
    p = os.path.join(d, "x.py")
    with open(p, "w") as f:
        f.write('"""do a thing: mop x [--flag]\n\nmore text\n"""\nimport nothing_such\n')
    c.expect("describe", cli.describe(p), "do a thing: mop x [--flag]")
    with open(p, "w") as f:
        f.write("x = 1\n")
    c.expect("describe of a module without a docstring must be empty", cli.describe(p), "")

    # deploy на python (#76): чистое — отвергнутые старые цели, недостающие
    # файлы MOP_BODY_EXTRA, имена проектов из origin'ов и легаси, --extra-vars.
    # HYPOTHESIS: deploy на bash, зовёт mop server config/projects/list подпроцессом.
    # SOLUTION: mop.cli.pool.deploy поверх библиотеки. STATUS: FIXED — see #76
    try:
        from mop.cli.server import deploy
        from mop.common import projects
    except ImportError as e:
        c.fail(f"{e}")
        return c.report("cli")
    # Деплой не трогает ~/etc (#311): ссылки ~/etc/nomad и ~/etc/nats были
    # стыком с личным инструментом автора (net setup) и никем больше не
    # читаются; старые цели запуска отвергает общий отказ «takes no
    # arguments» (#79). HYPOTHESIS: deploy.link заводит ссылки каждым
    # прогоном, RUN_TARGETS дублирует общий отказ. SOLUTION: оба удалены.
    # STATUS: FIXED — see #311
    src = open(deploy.__file__, encoding="utf-8").read()
    c.check("#311 deploy knows nothing of ~/etc", "~/etc" not in src and not hasattr(deploy, "link"))
    c.check("#311 no run-target table besides the generic refusal",
            not hasattr(deploy, "RUN_TARGETS") and not hasattr(deploy, "refused_target"))
    root = tempfile.mkdtemp(prefix="mop-test-deploy-")
    open(os.path.join(root, "sandbox.yaml"), "w").close()
    c.expect("missing_extras", deploy.missing_extras("sandbox.yaml, other.yaml,", root), ["other.yaml"])
    c.expect("missing_extras of an empty setting must be empty", deploy.missing_extras("", root), [])
    c.expect("projects.names", projects.names({"git@h:g/proj.git", "git@h:g/mop.git"}, {"legacy"}),
             ["legacy", "mop", "proj"])
    # Списком, а не строкой: `--extra-vars mop_projects=[...]` ansible берёт как
    # строку и проходит по её символам, порождая пользователей `master-[`.
    ev = _play.play_vars(["mop", "proj"], {"proj": {"asks": {}}})
    c.check(f"play_vars: {ev}", not (json.loads(ev[0]) != {"mop_projects": ["mop", "proj"]}
                                     or json.loads(ev[1]) != {"mop_manifests": {"proj": {"asks": {}}}}))
    # Узкий прогон проектов (#79) идёт без манифестов: их читают слои узла и
    # тела, а не роль шины. Лишний --extra-vars пустым словарём стирал бы
    # манифесты, уже разложенные полной игрой.
    c.expect("play_vars without manifests", _play.play_vars(["mop"]), [json.dumps({"mop_projects": ["mop"]})])
    # Лимиты папетов (#107) едут рядом с проектами, и пустые тоже: пустой
    # словарь -- правда контроллера «лимитов нет», и сервер обязан её
    # получить, иначе снятый лимит жил бы там дальше. Сама функция чистая:
    # файл читает play(), а не она.
    got = _play.play_vars(["mop"], limits={})
    c.expect("play_vars with empty limits", got, [json.dumps({"mop_projects": ["mop"], "mop_limits": {}})])
    got = _play.play_vars(["mop"], limits={"mop": 2})
    c.expect(f"play_vars with limits: {got}", json.loads(got[0]).get("mop_limits"), {"mop": 2})
    # STATUS: FIXED — see #107
    # Хосты форжей (#121) едут полной игре списком: роль узла доверяет ключу
    # каждого. Без них -- ключа нет вовсе, узкий прогон проектов их не
    # передаёт и роль узла не играет.
    got = _play.play_vars(["mop"], git_hosts=["dev.corp", "git.ermak.dev"])
    c.expect(f"play_vars with git hosts: {got}", json.loads(got[0]).get("mop_git_hosts"),
             ["dev.corp", "git.ermak.dev"])
    # #178: хосты инвентаря -- списком, только когда их дали.
    got = _play.play_vars(["mop"], inventory_hosts=["a", "b"])
    c.expect(f"play_vars with inventory hosts: {got}",
             json.loads(got[0]).get("mop_inventory_hosts"), ["a", "b"])
    c.check("play_vars without inventory hosts must not send an empty list",
            not ("mop_inventory_hosts" in json.loads(_play.play_vars(["mop"])[0])))
    c.check("play_vars without git hosts must not send an empty list",
            not ("mop_git_hosts" in json.loads(_play.play_vars(["mop"])[0])))
    # STATUS: FIXED — see #121

    # Локаль прогонов (#92): ansible требует UTF-8 и берёт её из окружения, а
    # свежая машина несёт LANG=C. Ставит её диспетчер, потому что зовут
    # прогон и `mop setup`, и `mop server deploy`, и сборка образа.
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
        c.expect(f"run_locale({wanted!r})", got, want)
    # Запасная берётся, даже если и её на машине нет: сказать нечего, а
    # C.UTF-8 встроена в glibc и есть везде, где есть сам glibc.
    c.expect("run_locale must fall back to C.UTF-8",
             cli.run_locale("ru_RU.UTF-8", usable=lambda _: False), "C.UTF-8")

    # Чем запускать команду, которой нужны права root (#91). Под root —
    # ничем: повышать нечего, а на выделенном сервере ещё и нечем, там
    # `sudo` попросту не стоит, и команда падала трассировкой на первом же
    # шаге установки.
    from mop.cli.pool import _self as pool_setup   # общее двух setup (#259)
    c.expect("elevate as root", pool_setup.elevate(uid=0), [])
    c.expect("elevate as a user", pool_setup.elevate(uid=1000), ["sudo"])

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
        # Глагол бывает и через дефис: `mop server pve-facts` (#158).
        listed = set(re.findall(rf"^  mop {group} ([a-z][a-z-]*)", doc, re.M))
        missing = listed - set(have.get(group) or {})   # дерево (#253): глаголы -- ключи
        c.check(f"group {group}: verbs without a module: {sorted(missing)}", not (missing))
        c.check(f"group {group}: its docstring lists no verbs", not (not listed))

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
            c.check(f"{os.path.relpath(path)}: defined twice: {dup}", not (dup))
            is_section_init = f == "__init__.py" and os.path.basename(root) in cli.SECTIONS
            c.check(f"{os.path.relpath(path)}: no main(argv)",
                    not (not is_section_init and "main" not in defs and not any(
                        isinstance(n, _ast.Assign) and any(getattr(t, "id", "") == "main" for t in n.targets)
                        for n in tree.body)))

    # ── #111: origin из рабочей копии, одним механизмом ─────────────────
    # HYPOTHESIS: `mop project add` требовал origin всегда, а команды, что
    # умели брать его из рабочей копии, делали это каждая по-своему: своя
    # проверка на вид origin'а у master, свой запасной путь у driver build,
    # отказ cwd_origin всегда называл `mop add`.
    # SOLUTION: lib.pick_origin -- чистое решение, lib.origin -- git и отказ.
    from mop.common import puppets
    for good in ("git@git.ermak.dev:ermak/mop.git", "https://h/g/mop.git",
                 "/srv/git/mop.git"):
        c.check(f"looks_like_origin({good!r}) must be true", not (not puppets.looks_like_origin(good)))
    # Голое имя и значение чужого флага, приехавшее позиционно, -- не origin.
    for bad in ("mop", "opus", "", "  "):
        c.check(f"looks_like_origin({bad!r}) must be false", not (puppets.looks_like_origin(bad)))

    here = "git@git.ermak.dev:ermak/mop.git"
    other = "git@git.ermak.dev:rugent/rugent.git"
    # Явный аргумент старше рабочей копии.
    c.expect("pick_origin: an explicit origin must win over the working copy",
             lib.pick_origin(other, here), other)
    # Нет аргумента -- рабочая копия.
    c.expect("pick_origin: without an argument the working copy's origin", lib.pick_origin(None, here), here)
    # Отказы -- ValueError с причиной, а не None: None дальше читался бы как
    # проект с пустым именем.
    for arg, cwd, why in (("opus", here, "doesn't look like a git-origin"),
                          (None, None, "no origin given")):
        try:
            got = lib.pick_origin(arg, cwd)
        except ValueError as e:
            c.check(f"pick_origin({arg!r}, {cwd!r}) refused without the reason: {e}", not (why not in str(e)))
            continue
        c.fail(f"pick_origin({arg!r}, {cwd!r}) must refuse, got {got!r}")
    # Неверный явный аргумент не подменяется рабочей копией: опечатка иначе
    # молча завела бы не тот проект.
    # STATUS: FIXED — see #111

    check_mcp_declarations(c)
    check_refusals(c)
    check_refusals_163(c)
    check_output_rules(c)
    check_empty_llm(c)
    check_empty_value(c)
    check_deploy_check(c)
    check_inventory_drivers(c)
    check_pool_uniform(c)
    check_node_memory_197(c)
    check_fallback_model_183(c)
    check_agent_on_node_172(c)
    check_bus_import_169(c)      # последними: перезагружают модули
    check_nomad_import_187(c)
    return c.report("cli")


def check_mcp_declarations(c):
    """#160: инструменты управления MCP -- объявления в самих командлетах.

    HYPOTHESIS: MCP держал вторую реализацию add/update/build руками, и она
    разошлась с командлетами (не слал workspace, собирал образ мимо сборщика).
    SOLUTION: командлет объявляет `MCP = {...}`; mop mcp находит объявления
    обходом пакета, читает их без импорта и зовёт сам командлет."""
    d = tempfile.mkdtemp(prefix="mop-test-mcp-")
    p = os.path.join(d, "x.py")

    # Объявление читается без импорта, как докстринг: модуль тянет шину.
    with open(p, "w") as f:
        f.write('"""x"""\nimport nothing_such\n'
                'MCP = {"annotations": "destructive", "args": ['
                '{"name": "name", "type": "string", "required": True}]}\n')
    got = cli.declared(p)
    c.check(f"declared: {got!r}", not ((got or {}).get("annotations") != "destructive" or
                                       [a["name"] for a in got.get("args", [])] != ["name"]))
    # Без объявления команда в MCP не видна: attach, master, code, service.
    with open(p, "w") as f:
        f.write('"""x"""\n')
    c.check("declared: a module without MCP must stay out of MCP", not (cli.declared(p) is not None))
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
        c.fail(f"declared must refuse: {bad}")

    # Описание инструмента -- докстринг командлета целиком: usage и смысл.
    with open(p, "w") as f:
        f.write('"""do x: mop x <name>\n\nmore text\n"""\nimport nothing_such\n')
    c.expect("docstring", cli.docstring(p), "do x: mop x <name>\n\nmore text")
    # Вывод командлета уходит модели без цветов терминала: lib.fail красит
    # всегда. Строки и табуляция остаются.
    got = lib.plain("\033[0;31mpu-mop-1: gone\033[0m\n\tnext\r")
    c.expect("plain", got, "pu-mop-1: gone\n\tnext")

    # Имя инструмента -- слова команды через подчёркивание.
    found = [("core", "add", False), ("node", "", True), ("service", "mcp", False)]
    group_verbs = {"node": {"drain": {}, "up": {}}}
    names = {n: words for n, words, _ in cli.tool_commands(found, group_verbs)}
    want = {"add": ["add"], "node": ["node"], "node_drain": ["node", "drain"],
            "node_up": ["node", "up"], "mcp": ["mcp"]}
    c.expect("tool_commands", names, want)

    # ── #253: вложенные группы -- дерево, имена по пути, приватное, прежние имена
    # HYPOTHESIS: диспетчер знает один уровень, `mop dev bug new` и
    # `mop server user add` некуда положить. SOLUTION: verbs() -- дерево,
    # tool_commands спускается по нему и именует инструмент словами через
    # подчёркивание; пространства из PRIVATE (dev) в MCP не выдаются --
    # внутренние команды разработчика модели не нужны; unalias переписывает
    # прежнее имя по таблице LEGACY молча, на один релиз.
    # STATUS: FIXED — see #253
    nested = [("core", "add", False), ("server", "", True), ("dev", "", True)]
    tree = {"server": {"deploy": {}, "user": {"add": {}, "passwd": {}}},
            "dev": {"bug": {"new": {}}}}
    got = {n: (words, os.path.relpath(path, cli.PACKAGE))
           for n, words, path in cli.tool_commands(nested, tree, private=())}
    want = {"add": (["add"], "core/add.py"),
            "server": (["server"], "server/__init__.py"),
            "server_deploy": (["server", "deploy"], "server/deploy.py"),
            "server_user": (["server", "user"], "server/user/__init__.py"),
            "server_user_add": (["server", "user", "add"], "server/user/add.py"),
            "server_user_passwd": (["server", "user", "passwd"], "server/user/passwd.py"),
            "dev": (["dev"], "dev/__init__.py"),
            "dev_bug": (["dev", "bug"], "dev/bug/__init__.py"),
            "dev_bug_new": (["dev", "bug", "new"], "dev/bug/new.py")}
    c.expect("#253 nested tool_commands", got, want)
    names = {n for n, _, _ in cli.tool_commands(nested, tree)}
    c.check(f"#253 PRIVATE must hide dev from the tools and keep server: {sorted(names)}",
            not (any(n.startswith("dev") for n in names) or "server_user_add" not in names))
    c.check(f"#253 dev must be private: {cli.PRIVATE}", not ("dev" not in cli.PRIVATE))
    table = {"bug": ("dev", "bug"), "web": ("server", "web")}
    for argv, want in [(["bug", "new", "t"], ["dev", "bug", "new", "t"]),
                       (["web", "--port", "1"], ["server", "web", "--port", "1"]),
                       (["list"], ["list"]), ([], [])]:
        got = cli.unalias(argv, table)
        c.expect(f"#253 unalias({argv})", got, want)
    c.check("#253 LEGACY must be the alias table", not (not isinstance(cli.LEGACY, dict)))
    # verbs() читает дерево с диска: подпакет группы -- подгруппа.
    d = tempfile.mkdtemp(prefix="mop-test-tree-")
    for rel in ("g/__init__.py", "g/a.py", "g/s/__init__.py", "g/s/b.py", "g/_hidden.py"):
        os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
        open(os.path.join(d, rel), "w").write('"""x"""\n')
    c.expect("#253 verbs tree", cli.verbs(d), {"g": {"a": {}, "s": {"b": {}}}})

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
        c.expect(f"tool_argv({values})", got, want)
    # Отказы: нет обязательного; позиционное, похожее на флаг, -- иначе
    # модель одним значением включала бы чужой флаг командлета.
    for values in ({}, {"name": ""}, {"name": "--fresh"},
                   {"name": "pu-mop-1", "origin": "-x"}):
        try:
            got = cli.tool_argv(args, values)
        except ValueError:
            continue
        c.fail(f"tool_argv({values}) must refuse, got {got}")
    # Пропущенный необязательный позиционный, а за ним заданный -- сдвиг на
    # чужое место: отказ.
    two = [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}]
    try:
        got = cli.tool_argv(two, {"b": "x"})
        c.fail(f"tool_argv must refuse a gap in positionals, got {got}")
    except ValueError:
        pass
    # STATUS: FIXED — see #160

    # Каждое объявление в дереве разбирается: кривое уронило бы mop mcp
    # целиком на старте мастера.
    for _, _, path in cli.tool_commands(cli.scan(), cli.verbs(), private=()):
        try:
            cli.declared(path)
        except ValueError as e:
            c.fail(f"{os.path.relpath(path)}: {e}")


def check_refusals_163(c):
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
    import importlib
    from mop.common import bus, puppets

    reason = "no rights for nodes: project mop"

    def run(fn, argv=()):
        return run_command(fn, argv, via_cli=True)

    def refusal(what, got, text):
        out, err, code = got
        c.check(f"{what}: stdout {out!r}, stderr {err!r}, exit {code!r}",
                not (not isinstance(code, int) or code == 0 or out or err != text + "\n"))

    # Живой шине сюда хода нет: и соединение, и адрес -- заглушки.
    async def no_bus(*a, **k):
        raise AssertionError("a check reached the live bus")

    def no_connect(*a, **k):
        raise AssertionError("a check reached the live bus")
    real_ready_nodes = puppets.ready_nodes
    with patched(bus, connect=no_connect), patched(bus.nats, connect=no_bus), \
            restored(bus, "ask_cluster", "request_many"), restored(puppets, "ready_nodes"), \
            patched_env(MOP_SERVER_LAN="192.0.2.1"):
        node = importlib.import_module("mop.cli.node")
        stat = importlib.import_module("mop.cli.core.stat")

        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        refusal("mop node on a refusal", run(node.main), reason)
        for fn in (puppets.pool, puppets.ready_nodes):
            try:
                got = fn()
                c.fail(f"{fn.__name__} read a refusal as {got!r}")
            except bus.Refused as e:
                c.expect(f"{fn.__name__} lost the reason: {e!r}", str(e), reason)
        refusal("mop stat on a pool refusal", run(stat.main), reason)
        # Подвал `mop list`: причина видна, а не пустой пул.
        c.expect("pool_lines on a refusal", lib.pool_lines(), [f"  {reason}"])

        # stat: ни один узел не ответил -- тот же текст, что раньше, но
        # отказом: stderr, ненулевой выход, stdout пуст.
        puppets.ready_nodes = lambda: {"n1", "n2"}
        bus.request_many = lambda verb, nodes, **kw: {}
        refusal("mop stat with no node answering", run(stat.main),
                "no node answered:\n  n1: no response\n  n2: no response")

        # Пустой пул без отказа сервиса -- не «никто не ответил» с пустым
        # перечнем, а прямо: готовых узлов нет.
        puppets.ready_nodes = lambda: set()
        refusal("mop stat on an empty pool", run(stat.main),
                "no ready nodes in the pool")

        # Обычный ответ -- вывод прежний, символ в символ.
        row = {"name": "hyper", "driver": "pve", "serves": "mop", "state": "ready",
               "free_mb": 40960, "total_mb": 65536, "slots": 5, "slots_total": 8}
        # #243: слоты -- свободно/всего; всего не знает сервис старше -- «-».
        old = {k: v for k, v in row.items() if k != "slots_total"}
        down = {"name": "mate", "driver": "host", "serves": "-", "state": "down",
                "free_mb": None, "total_mb": None, "slots": None, "slots_total": None}
        bus.ask_cluster = lambda verb, **kw: {"nodes": [row, dict(old, name="old"), down]}
        got = run(node.main)
        want = ("NODE   DRIVER  SERVES  STATE  FREE   TOTAL  SLOTS\n"
                "hyper  pve     mop     ready  40 GB  64 GB  5/8\n"
                "old    pve     mop     ready  40 GB  64 GB  5/-\n"
                "mate   host    -       down   -      -      -\n", "", 0)
        c.expect("mop node on a normal answer", got, want)
        pool = [{"name": "gpu", "status": "ready", "free_mb": 2048, "total_mb": 40960,
                 "slots": 0, "slots_total": 5},
                {"name": "old", "status": "ready", "free_mb": 2048, "total_mb": 40960,
                 "slots": 0},
                {"name": "off", "status": "down"}]
        bus.ask_cluster = lambda verb, **kw: {"nodes": pool}
        want = ["  gpu: free 2/40 GB, slots 0/5", "  old: free 2/40 GB, slots 0/-",
                "  off: down"]
        c.expect("pool_lines", lib.pool_lines(), want)
        bus.ask_cluster = lambda verb, **kw: {"nodes": [pool[0]]}

        # #245: stat --users -- по людям, от самого прожорливого; узел со
        # старым агентом (без by_login) -- в «-». --puppets и --users разом --
        # usage.
        def u(i, o, w, r):
            return {"input": i, "output": o, "cache_write": w, "cache_read": r}
        puppets.ready_nodes = lambda: {"hyper", "gpu"}
        bus.request_many = lambda verb, nodes, **kw: {
            "hyper": {"ok": True, "usage": {"pu-mop-1": {"2026-09-22": u(2000, 500, 0, 10000)}},
                      "by_login": {"pu-mop-1": {"anton": {"2026-09-22": u(2000, 500, 0, 10000)}}}},
            "gpu": {"ok": True, "usage": {"pu-mop-2": {"2026-09-22": u(10, 0, 0, 0)}}}}
        out, err, code = run(stat.main, ["--users"])
        want = ("USER   INPUT  OUTPUT  CACHE-W  CACHE-R  TOTAL\n"
                "anton  2.0k   500     0        10k      12k\n"
                "-      10     0       0        0        10\n")
        c.check(f"#245 mop stat --users: exit {code!r}, stderr {err!r}, stdout {out!r}",
                not (code or err or want not in out))
        out, err, code = run(stat.main, ["--puppets", "--users"])
        c.check(f"#245 --puppets with --users must be a usage error: {out!r}", not (not code))
        puppets.ready_nodes = real_ready_nodes
        # #277: ответ глагола pool -- форма pool, а не строка nodes: у той нет
        # статуса, и PoolNode её не примет.
        c.expect("pool on a normal answer", puppets.pool(), [domain.PoolNode.from_pool(pool[0])])


def check_refusals(c):
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
    from mop.common import bus, puppets

    reason = "pu-mop-9 belongs to project other, not to mop"

    # Строгий вызов: отказ -- Refused с причиной как есть, ответ -- как есть.
    with restored(bus, "ask_cluster"):
        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        try:
            got = bus.call_cluster("alloc", name="pu-mop-9")
            c.fail(f"call_cluster must raise Refused on a refusal, got {got!r}")
        except bus.Refused as e:
            c.expect(f"call_cluster lost the reason: {e!r}", str(e), reason)
        c.check("Refused must be a RuntimeError: callers catch that already",
                not (not issubclass(bus.Refused, RuntimeError)))
        bus.ask_cluster = lambda verb, **kw: {"ok": True, "verb": verb, **kw}
        got = bus.call_cluster("spec", name="pu-mop-9")
        c.expect("call_cluster must pass the answer through", got,
                 {"ok": True, "verb": "spec", "name": "pu-mop-9",
                  "timeout": bus.TIMEOUT, "project": None})

        # running_alloc: отказ сервиса доходит причиной, а не «не размещён».
        bus.ask_cluster = lambda verb, **kw: {"error": reason}
        try:
            puppets.running_alloc("pu-mop-9")
            c.fail("running_alloc must raise on a refusal")
        except LookupError:
            c.fail("running_alloc read a refusal as a missing allocation")
        except bus.Refused as e:
            c.expect(f"running_alloc lost the reason: {e!r}", str(e), reason)
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
                c.fail(f"running_alloc({alloc!r}) must raise, got {got!r}")
            except LookupError as e:
                c.check(f"running_alloc: {e!r}, want {want!r}...", not (not str(e).startswith(want)))
        live = {"ClientStatus": "running", "NodeName": "n1"}
        bus.ask_cluster = lambda verb, **kw: {"ok": True, "alloc": live}
        c.expect("running_alloc must return the running allocation",
                 puppets.running_alloc("pu-mop-9"), live)

        # guard: один вопрос шине, и его ответ -- вызывающему, а не второй
        # такой же запрос следом (update спрашивал spec дважды).
        asked = []
        spec = {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}
        bus.ask_cluster = lambda verb, **kw: asked.append(verb) or spec
        with patched(bus, PROJECT="mop"):
            got = lib.guard("pu-mop-9")
        c.check(f"guard: asked {asked}, returned {got!r}", not (asked != ["spec"] or got != spec))

    # Диспетчер: отказ -- ровно одна строка в stderr с причиной, ненулевой
    # выход, в stdout ничего: stdout модель читает как ответ команды.
    for exc in (bus.Refused(reason), LookupError(reason)):
        def fn(_argv, exc=exc):
            raise exc
        out, err, code = run_command(fn, [], via_cli=True)
        name = type(exc).__name__
        c.check(f"run({name}): exit {code!r}, want a non-zero int",
                not (not isinstance(code, int) or code == 0))
        c.expect(f"run({name}): stderr must be one line", err, reason + "\n")
        c.check(f"run({name}): stdout {out!r}", not (out))

    # Своих копий больше нет: помощники, выходившие из процесса, и ручные
    # проверки поля error у сервиса кластера в командлетах.
    for gone in ("require_job", "alloc_of", "running_alloc", "running_node"):
        c.check(f"lib.{gone} must be gone: puppets.running_alloc / bus.call_cluster",
                not (hasattr(lib, gone)))
    # Два исключения: у ping в cluster check поле error рядом с ok -- это
    # «сервис жив, Nomad нет», а не отказ; deploy отдаёт ответ целиком
    # projects.for_deploy, и отказ там -- заметка, а не конец прогона.
    allowed = {os.path.join("server", "cluster", "check.py"), os.path.join("server", "deploy.py")}
    for root, _, files in os.walk(cli.PACKAGE):
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, cli.PACKAGE)
            if not f.endswith(".py") or rel in allowed:
                continue
            with open(path) as fh:
                c.check(f"{rel}: bus.ask_cluster by hand, use bus.call_cluster",
                        not ("bus.ask_cluster(" in fh.read()))


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
    "JSON", "NATS", "LDAP", "PATH", "PYTHONPATH", "HEAD", "TERM", "SIGHUP", "VMID",
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


def check_output_rules(c):
    """STATUS: FIXED — see #159"""
    import glob
    import re
    from mop.common import bus, llm, puppets
    from mop.client import keys
    files = sorted(glob.glob(os.path.join(ROOT, "mop", "cli", "**", "*.py"), recursive=True)) \
        + [os.path.join(ROOT, "mop", "client", "channel.py"), os.path.join(ROOT, "mop", "node", "agent.py")]
    for hit in caps_hits(files):
        c.fail(f"caps for emphasis: {hit}")

    os.environ.setdefault("MOP_SERVER_LAN", "10.0.0.1")
    with restored(bus, "call_cluster"), restored(puppets, "running_alloc", "diagnose"), \
            restored(lib, "guard"), restored(keys, "llm_keys_blob"), restored(llm, "profiles"):
        bus.call_cluster = lambda verb, **kw: {"ok": True}
        puppets.running_alloc = lambda name: {"ClientStatus": "running", "NodeName": "n1"}
        lib.guard = lambda name: {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}
        # Быстрая команда на успехе молчит: MCP ответит модели `done`.
        from mop.cli.core import restart
        from mop.cli.node import drain, forget
        for what, main, argv in (("restart", restart.main, ["pu-mop-1"]),
                                 ("node drain", drain.main, ["n1"]),
                                 ("node forget", forget.main, ["n1"])):
            out, err, code = run_command(main, argv)
            c.check(f"{what} must be silent on success: out {out!r} err {err!r} code {code!r}",
                    not (out or err or code))
        from mop.cli.pool import doctor
        from mop.cli.pool import llm as llm_cmd
        # Здоровый пул — одна строка результата, её и показываем.
        puppets.diagnose = lambda: []
        out, _, _ = run_command(doctor.main, [])
        c.expect("doctor on a healthy pool", out, "pool is healthy: nothing stuck\n")
        # Советов «run X» в успешном выводе нет: таблица уже говорит [restart].
        puppets.diagnose = lambda: [{"name": "pu-mop-1", "alloc": {"NodeName": "n1"},
                                     "diagnosis": "HUNG (not responding)", "action": "restart"}]
        out, _, _ = run_command(doctor.main, [])
        keys.llm_keys_blob = lambda: ("", None)
        llm.profiles = lambda: {"glm": {"key": "Z_AI_KEY", "doc": "", "env": {}}}
        out2, _, _ = run_command(llm_cmd.main, [])
        for what, text in (("doctor", out), ("llm", out2)):
            c.check(f"{what} advises another command on success: {text!r}",
                    not (re.search(r"\bmop [a-z]+", text)))
    check_output_rest(c)


def check_output_rest(c):
    """Остаток #159: setup, sweep, driver build. STATUS: FIXED — see #159"""
    import shutil
    import subprocess
    from mop.common import bus, puppets
    from mop.server import image, playvars
    from mop.cli.driver import build
    from mop.cli.pool import setup, sweep

    # setup: шаг установки — строка хода на терминале, не на терминале
    # тишина; предупреждение про claude — в stderr, stdout пуст.
    ran = []
    with restored(shutil, "which"), restored(subprocess, "run"), restored(playvars, "playbook_vars"):
        shutil.which = lambda cmd: "/usr/bin/sudo" if cmd == "sudo" else None
        subprocess.run = lambda args, **kw: ran.append(args) or subprocess.CompletedProcess(args, 0)
        playvars.playbook_vars = lambda: {}
        out, err, code = run_command(setup.main, [])
        # #259: mop setup -- машина оператора, флаг ушёл; контроллер --
        # mop server setup, с ролью controller и без слова про claude.
        _, _, refused = run_command(setup.main, ["--operator"])
        from mop.cli.server import setup as server_setup
        ran_before = len(ran)
        s_out, s_err, s_code = run_command(server_setup.main, [])
        server_role = [a for a in ran[ran_before:] if "ansible-playbook" in a]
    c.check(f"setup: stdout {out!r}, stderr {err!r}, code {code!r}, ran {len(ran)} commands",
            not (out or code or "claude is not in PATH" not in err or len(ran) - 3 != 3))
    c.check("#259 mop setup --operator must be a usage refusal: the flag is gone", not (not refused))
    c.check(f"#259 mop server setup: code {s_code!r}, stderr {s_err!r}, ran {server_role!r}",
            not (s_code or s_out or "claude" in s_err or not server_role
                 or '{"mop_role": "controller"}' not in server_role[0]))

    # sweep: нечего убирать — успех, и он молчит.
    with patched(puppets, ready_nodes=lambda: {"n1"}, jobs=lambda *a, **kw: [{"ID": "pu-a-1"}],
                 classify_junk=lambda answers, known: []), \
            patched(bus, request_many=lambda verb, nodes, **kw: {"n1": {"bodies": [], "templates": []}}):
        out, err, code = run_command(sweep.main, [])
    c.check(f"sweep with nothing to sweep must be silent: {out!r} {err!r} {code!r}", not (out or err or code))

    # driver build: без эха того, откуда прочитан .mop.
    with patched(image, prepare=lambda origin, root: {"project": "p", "asks": {}, "alien": [], "legacy": []},
                 build=lambda origin, got, **kw: {"gone": [], "announced": [], "rc": 0}):
        out, err, code = run_command(build.main, ["git@h:g/p.git"])
    c.check(f"driver build echoes on success: {out!r} code {code!r}", not (out or code))
    check_output_179(c)
    check_output_182(c)
    check_restore_all_188(c)


def check_output_179(c):
    """HYPOTHESIS (#179): после #159 остались отказы `mop sweep` в stdout (а
    «no ready nodes» ещё и с кодом 0 -- отказ читался успехом),
    предупреждения `mop driver build` в stdout и эхо `mop update` на успехе.
    SOLUTION: отказы и предупреждения -- в stderr теми же словами, «no ready
    nodes» -- код 1, update на успехе молчит (MCP ответит done, как restart).
    STATUS: FIXED — see #179"""
    from mop.common import bus, llm, puppets
    from mop.server import image
    from mop.client import keys
    from mop.cli.core import update
    from mop.cli.driver import build
    from mop.cli.pool import sweep

    def in_stderr(got, text, nonzero):
        """Отказ только в stderr, с text и нужным кодом. -> (ok, подробность)."""
        out, err, code = got
        return (not (out or text not in err or (bool(code) != nonzero)),
                f"stdout {out!r}, stderr {err!r}, code {code!r}")

    with restored(puppets, "ready_nodes", "jobs", "classify_junk"), restored(bus, "request_many"):
        puppets.ready_nodes = lambda: set()
        c.check("sweep with no ready nodes",
                *in_stderr(run_command(sweep.main, []), "no ready nodes", True))
        puppets.ready_nodes = lambda: {"n1"}
        bus.request_many = lambda verb, nodes, **kw: {"n1": None}
        c.check("sweep where no node answered",
                *in_stderr(run_command(sweep.main, []), "no node answered", True))
        bus.request_many = lambda verb, nodes, **kw: {"n1": {"bodies": [], "templates": []}}
        puppets.jobs = lambda *a, **kw: []
        c.check("sweep where Nomad lists no puppets",
                *in_stderr(run_command(sweep.main, []), "Nomad lists no puppets", True))
        # Промолчавший узел при ответившем соседе: отказ по нему -- тоже в
        # stderr, а код выхода за него отвечает.
        bus.request_many = lambda verb, nodes, **kw: {"n1": {"bodies": [], "templates": []},
                                               "n2": None}
        puppets.ready_nodes = lambda: {"n1", "n2"}
        puppets.jobs = lambda *a, **kw: [{"ID": "pu-a-1"}]
        puppets.classify_junk = lambda answers, known: []
        c.check("sweep with one silent node",
                *in_stderr(run_command(sweep.main, []), "n2: no response", True))

    with patched(image, prepare=lambda origin, root: {"project": "p", "asks": {},
                                                      "alien": ["MOP_X"], "legacy": [".mop.yaml"]},
                 build=lambda origin, got, **kw: {"gone": [], "announced": [], "rc": 0}):
        out, err, code = run_command(build.main, ["git@h:g/p.git"])
    c.check(f"driver build warnings must go to stderr: {out!r} {err!r} {code!r}",
            not (out or code or "MOP_X is not a project's to set — ignored" not in err
                 or "read as .mop/sandbox.yaml for the transition" not in err))

    calls = []
    with patched(lib, guard=lambda name: {"ok": True, "meta": {"origin": "git@h:g/mop.git"}}), \
            patched(bus, call_cluster=lambda verb, **kw: calls.append(verb) or {"ok": True}), \
            patched(keys, push_llm_keys=lambda profile: None), \
            patched(_common, workspace_text=lambda origin: ""):
        for argv in (["pu-mop-1"], ["pu-mop-1", "--fresh"], ["pu-mop-1", "git@h:g/other.git"]):
            calls.clear()
            out, err, code = run_command(update.main, argv)
            c.check(f"update {argv} must be silent on success: out {out!r} "
                    f"err {err!r} code {code!r} calls {calls}",
                    not (out or err or code or calls != ["update"]))


def check_output_182(c):
    """HYPOTHESIS (#182): после #179 `mop driver build` эхом печатал
    содержимое манифеста («asks for k=v»), отчёт о пересозданных телах и
    объявлении образа, а `mop server cluster users` -- «changed/unchanged».
    SOLUTION: сборка показывает шаги (lib.Progress: строка на терминале,
    тишина не на терминале), факты -- шагами, а не отчётом; отказ подъёма
    папета и объявления образа -- stderr с именем; users на успехе молчит.
    STATUS: FIXED — see #182"""
    from mop.server import bootstrap, image, natsconf, nomad, spec
    from mop.common import projects
    from mop.cli.server.cluster import users
    from mop.cli.driver import build
    def through(main, argv):
        return run_command(main, argv, via_cli=True)
    with offline(), restored(image, "prepare", "build", "clear", "bake", "announce"), \
            restored(nomad, "register"), restored(spec, "job_spec"), restored(projects, "read"), \
            restored(natsconf, "apply", "reload", "apply_callout"):
        got = {"project": "p", "asks": {"MOP_CORES": "8"}, "alien": [], "legacy": []}
        image.prepare = lambda origin, root: got
        gone = [{"name": "pu-p-1", "origin": "git@h:g/p.git", "llm": "claude", "node": "hyper"}]
        image.clear = lambda project, force=False, api=None, node=None: list(gone)
        image.bake = lambda *a, **kw: 0
        image.announce = lambda project, api=None, node=None: [("hyper", "announced, serves p"),
                                          ("gpu", "not a container node"),
                                          ("old", "already announced")]
        spec.job_spec = lambda *a, **kw: {}
        nomad.register = lambda job: None

        # Успех не на терминале -- тишина: ни эха манифеста, ни отчёта.
        out, err, code = through(build.main, ["git@h:g/p.git"])
        c.check(f"driver build must be silent on success off a TTY: "
                f"out {out!r} err {err!r} code {code!r}", not (out or err or code))

        # Папет не поднялся -- громко, с именем и причиной.
        def refuse(job):
            raise RuntimeError("Nomad said no")
        nomad.register = refuse
        out, err, code = through(build.main, ["git@h:g/p.git"])
        c.check(f"a failed re-raise must reach stderr with the puppet: "
                f"out {out!r} err {err!r} code {code!r}",
                not (out or not code or "pu-p-1" not in err or "Nomad said no" not in err))
        nomad.register = lambda job: None

        # Узел, которому образ объявить не вышло (#175), -- stderr.
        image.announce = lambda project, api=None, node=None: [("hyper", "announced, serves p"),
                                          ("bad", "bad: unknown driver 'bogus'")]
        out, err, code = through(build.main, ["git@h:g/p.git"])
        c.check(f"a failed announcement must reach stderr alone: "
                f"out {out!r} err {err!r} code {code!r}",
                not (out or "bad: unknown driver 'bogus'" not in err or "hyper" in err))

        # cluster users на успехе молчит -- и с --reload.
        projects.read = lambda: ["git@h:g/p.git"]
        natsconf.apply = lambda names, creds: (True, None)
        natsconf.reload = lambda: None
        # callout.conf (#206) -- тот же /etc/nats: здесь не пишется.
        natsconf.apply_callout = lambda: False
        for argv in ([], ["--reload"]):
            out, err, code = through(users.main, argv)
            c.check(f"cluster users {argv} must be silent on success: "
                    f"out {out!r} err {err!r} code {code!r}", not (out or err or code))


def check_restore_all_188(c):
    """HYPOTHESIS (#188): image.restore поднимал папетов до первого отказа
    Nomad; остальные снятые в `gone` не пробовались вовсе, и об этом не
    говорил никто -- пересборка оставляла их лежать.
    SOLUTION: пробовать каждого, отказы собрать и бросить одним исключением,
    по строке на папета («<папет> on <узел>: not raised again: <причина>»).
    STATUS: FIXED — see #188"""
    from mop.server import image, nomad, spec
    gone = [{"name": f"pu-p-{i}", "origin": "git@h:g/p.git", "llm": "claude",
             "node": "hyper"} for i in (1, 2, 3)]
    with offline(), restored(nomad, "register"), restored(spec, "job_spec"):
        spec.job_spec = lambda name, origin, llm, **kw: {"ID": name}
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
            c.expect(f"restore with {sorted(refused)} refused: registered", registered, want)
            if not refused:
                c.check(f"restore with nothing refused raised: {err!r}", not (err is not None))
                continue
            lines = (err or "").splitlines()
            want_lines = [f"{n} on hyper: not raised again: Nomad refused {n}"
                          for n in sorted(refused)]
            c.expect(f"restore with {sorted(refused)} refused: {err!r}", lines, want_lines)


def check_empty_llm(c):
    """HYPOTHESIS (#164): `--llm` без значения давал профиль "", и отказ
    llm.require звучал как «no LLM profile (empty)» — не про флаг, который
    забыли заполнить. SOLUTION: пустое значение — ошибка использования в
    parse_llm, до реестра профилей. STATUS: FIXED — see #164"""

    def through_dispatcher(argv):
        return run_command(lambda x: _common.parse_llm(x) and None, argv, via_cli=True)
    for argv in (["--llm", ""], ["pu-mop-1", "--llm"], ["--llm="], ["--llm", "--fresh"]):
        out, err, code = through_dispatcher(argv)
        lines = err.strip().splitlines()
        c.check(f"parse_llm({argv}): out {out!r} err {err!r} code {code!r}",
                not (out or not code or len(lines) != 1 or "Traceback" in err
                     or not lines[0].startswith("--llm needs a profile name")))
    got = _common.parse_llm(["pu-mop-1", "--llm", "claude"])
    c.expect("parse_llm with a profile", got, ("claude", ["pu-mop-1"]))
    c.expect("parse_llm without --llm must leave the profile unset",
             _common.parse_llm(["pu-mop-1"]), (None, ["pu-mop-1"]))
    # Неизвестный профиль — прежний отказ, слово в слово.
    out, err, code = through_dispatcher(["--llm", "no-such"])
    c.check(f"an unknown profile keeps its refusal: {err!r} {code!r}",
            not (not code or not err.startswith("no LLM profile no-such; available: ")))



def check_empty_value(c):
    """HYPOTHESIS (#327): parse_value сворачивал пустое значение в None --
    тот же ответ, что «флага нет»: `mop add --cred` без имени молча
    заводил папета на первой активной аренде профиля. SOLUTION: пустое
    значение -- ошибка использования, как у --llm (#164).
    RESULT: четыре пустых формы отказывают одной строкой через диспетчер.
    STATUS: FIXED — see #327"""

    def through_dispatcher(argv):
        return run_command(lambda x: _common.parse_value(x, "--cred") and None,
                           argv, via_cli=True)
    for argv in (["pu-mop-1", "--cred"], ["--cred="], ["--cred", ""],
                 ["--cred", "--fresh"]):
        out, err, code = through_dispatcher(argv)
        lines = err.strip().splitlines()
        c.check(f"parse_value({argv}): out {out!r} err {err!r} code {code!r}",
                not (out or not code or len(lines) != 1 or "Traceback" in err
                     or lines[0] != "--cred needs a value"))
    c.expect("parse_value with a value",
             _common.parse_value(["pu-mop-1", "--cred", "alice", "--fresh"], "--cred"),
             ("alice", ["pu-mop-1", "--fresh"]))
    c.expect("parse_value with flag=value",
             _common.parse_value(["--cred=alice", "pu-mop-1"], "--cred"),
             ("alice", ["pu-mop-1"]))
    c.expect("parse_value without the flag must leave the value unset",
             _common.parse_value(["pu-mop-1", "--fresh"], "--cred"),
             (None, ["pu-mop-1", "--fresh"]))

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


def check_inventory_drivers(c):
    """HYPOTHESIS (#186): опечатка в mop_driver инвентаря доезжала до meta
    Nomad, и ловили её только читатели (driver.of_node, #175). SOLUTION: deploy
    прогоняет драйвер каждого хоста через of_node до плейбука.
    STATUS: FIXED — see #186"""
    from mop.cli.server import deploy
    fn = getattr(deploy, "driver_refusals", None)
    got = fn(INVENTORY_TYPO, "host") if fn else None
    c.check(f"driver_refusals: {got!r}, wanted one refusal for typo/pvee",
            not (got is None or len(got) != 1 or not got[0].startswith("typo: unknown driver 'pvee'")))
    clean = fn(INVENTORY_CLEAN, "host") if fn else None
    c.check(f"a clean inventory refused: {clean!r}", not (fn and clean))
    # Пусто и нет ключа -- драйвер по умолчанию, как у of_node; а битый
    # MOP_DRIVER ловится на тех, кому он достаётся.
    if fn:
        bad = fn(INVENTORY_CLEAN, "pvee")
        c.expect(f"a broken MOP_DRIVER must name the hosts that inherit it: {bad!r}",
                 sorted(r.split(":")[0] for r in bad), ["localhost", "plain"])
        empty = {**INVENTORY_CLEAN, "_meta": {"hostvars": {"plain": {"mop_driver": ""}}}}
        c.check("an empty mop_driver is the default driver, not a refusal", not (fn(empty, "host")))


def check_node_memory_197(c):
    """HYPOTHESIS (#197): строка mop_mem_mb в инвентаре не работает нигде:
    на host-узле спеку строит сервер своим MOP_MEM_MB (и #190 отказывал),
    на pve память тела шла из образа, то есть из `.mop`. SOLUTION: память --
    свойство папета (дефолт -> .env -> `.mop`), строку deploy отвергает на
    любом узле; потолок узла mop_body_mem_cap_mb остаётся узловым и едет в
    meta Nomad, где `>=` сравнивает численно только целые -- поэтому потолок
    не из одного целого числа тоже отказ.
    STATUS: FIXED — see #197"""
    from mop.cli.server import deploy
    with offline():
        fn = getattr(deploy, "memory_refusals", None)
        if not c.check("no deploy.memory_refusals", fn is not None):
            return

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
            c.expect(f"memory_refusals, {what}: {got!r}", heads, sorted(want))
        got = fn(listing({"odd": {"mop_mem_mb": "12288"}}), "32768")
        text = " ".join(got)
        c.check(f"memory_refusals must say where memory comes from now: {got!r}",
                not (not ("not a node setting" in text and ".env" in text and ".mop" in text
                          and "remove the line" in text)))


def check_pool_uniform(c):
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
    from mop.common import config
    from mop.cli.server import deploy
    with offline():
        uniform = getattr(config, "POOL_UNIFORM", None)
        c.expect("config.POOL_UNIFORM: paths and user on every host",
                 set(uniform or ()), {"MOP_HOME", "MOP_USER", "MOP_PUPPET_SEED"})
        c.check("config.HOST_UNIFORM is gone with #197: MOP_MEM_MB is not a node setting",
                not (hasattr(config, "HOST_UNIFORM")))
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
            c.expect(f"uniform_refusals, {what}: {got!r}", heads, want)


def check_deploy_check(c):
    """HYPOTHESIS (#177): доказать, что правка deploy/ не меняет узлы, было
    нечем — `mop server deploy` аргументов не берёт, и #157 подкладывал на PATH
    обёртку ansible-playbook. А после плейбука deploy пишет файлы
    (creds.collect). SOLUTION: `mop server deploy --check` — --check --diff каждому
    ansible-playbook, и ничего пишущего после плейбука. STATUS: FIXED — see #177"""
    import shutil
    import subprocess
    from mop.common import bus, config, creds, projects
    from mop.server import playvars
    from mop.cli.server import deploy
    d = tempfile.mkdtemp(prefix="mop-test-deploy-check-")
    inventory, key = os.path.join(d, "inventory.yaml"), os.path.join(d, "id")
    for f in (inventory, key, key + ".pub"):
        open(f, "w").close()
    # Один человек в файле операторов: без людей deploy отказывает (#219).
    from mop.server import identity
    people = os.path.join(d, "operators")
    with open(people, "w") as f:
        f.write(identity.format_line(identity.Identity("anton", "admin", ("*",)),
                                     identity.hash_password("x")) + "\n")
    calls, collected, checked = [], [], []
    listing = [INVENTORY_CLEAN]
    # Окружение -- снимком до предохранителя: после проверки оно целиком
    # прежнее, без MOP_SERVER_LAN предохранителя и без INVENTORY здесь.
    env = dict(os.environ)
    try:
        with offline(), \
                patched(shutil, which=lambda cmd: f"/usr/bin/{cmd}"), \
                patched(config, require=lambda *a: None), \
                patched(subprocess, call=lambda argv, **kw: calls.append(argv) or 0,
                        run=lambda argv, **kw: subprocess.CompletedProcess(
                            argv, 0, json.dumps(listing[0]), "")), \
                patched(playvars, playbook_vars=lambda: {}), \
                patched(deploy, missing_extras=lambda setting, root: [],
                        link=lambda name, target: None, manifests=lambda origins: {},
                        check=lambda: checked.append(1)), \
                patched(bus, ask_cluster=lambda *a, **kw: {"ok": True, "projects": []}), \
                patched(projects, for_deploy=lambda answer, local: ([], None), read=lambda: []), \
                patched(creds, collect=lambda *a, **kw: collected.append(1) or [],
                        operator=lambda dest: "anton"):
            os.environ.update({"INVENTORY": inventory, "MOP_GIT_KEY": key,
                               "MOP_OPERATORS_FILE": people})
            for argv, dry in (([], False), (["--check"], True)):
                calls.clear(), collected.clear(), checked.clear()
                out, err, code = run_command(deploy.main, argv)
                plays = [cmd for cmd in calls if cmd and cmd[0] == "ansible-playbook"]
                flags = {"--check", "--diff"} & {a for cmd in plays for a in cmd}
                c.check(f"deploy {argv}: code {code!r}, plays {plays!r}, err {err!r}",
                        not (code or not plays or flags != ({"--check", "--diff"} if dry else set())))
                # #178: хосты инвентаря едут плейбуку -- их файлом кладёт роль
                # cluster, и по нему forget отказывает узлу, который deploy
                # поставил бы снова.
                sent = [json.loads(cmd[i + 1]) for cmd in plays for i, a in enumerate(cmd)
                        if a == "--extra-vars"]
                hosts = [v["mop_inventory_hosts"] for v in sent if "mop_inventory_hosts" in v]
                c.expect(f"deploy {argv} must send the inventory's hosts to the playbook (#178)",
                         hosts, [["explicit", "hyper", "localhost", "plain", "typo"]])
                c.check("deploy --check must not collect server credentials (it writes)",
                        not (dry and collected))
                c.check("deploy without --check must still collect server credentials",
                        not (not dry and not collected))
            # #335: гейт спрашивает пайплайн коммита на ветке раскатки --
            # ветке origin по умолчанию, а не последний на любой ветке (там
            # 27.09 оказался идущий пайплайн эпика на том же коммите).
            from mop.common import gitlab
            asked, real_get = [], config.get

            def pipeline(sha, ref=None):
                asked.append((sha, ref))
                return {"id": 1, "status": "success", "web_url": "u"}
            with patched(config, get=lambda name, default=None: "1"
                         if name == "MOP_DEPLOY_NEEDS_GREEN" else real_get(name, default)), \
                    patched(gitlab, has_credentials=lambda: True, pipeline=pipeline,
                            jobs=lambda pid: []), \
                    patched(deploy, head_sha=lambda root: "5b92b440c952",
                            ci_state=lambda root: ("feature", "master", [])):
                calls.clear(), collected.clear()
                out, err, code = run_command(deploy.main, [])
                c.expect(f"#335 deploy gate asks the default branch's pipeline (err {err!r})",
                         (code, asked), (0, [("5b92b440c952", "master")]))
            # Прочие аргументы — по-прежнему отказ, и до плейбука.
            for argv in (["pool"], ["git@h:g/p.git"], ["--check", "extra"], ["--diff"]):
                calls.clear()
                out, err, code = run_command(deploy.main, argv)
                c.check(f"deploy {argv} must be refused before any playbook: code {code!r}",
                        not (not code or calls))
            # #190: перекрытая на хосте настройка, из которой сервер строит спеку.
            listing[0] = {**INVENTORY_CLEAN, "_meta": {"hostvars": {
                **INVENTORY_CLEAN["_meta"]["hostvars"], "plain": {"mop_home": "/srv/elsewhere"}}}}
            for argv in ([], ["--check"]):
                calls.clear(), collected.clear()
                out, err, code = run_command(deploy.main, argv)
                plays = [cmd for cmd in calls if cmd and cmd[0] == "ansible-playbook"]
                lines = [lib.plain(l) for l in err.strip().splitlines()]
                c.check(f"deploy {argv} over a host override of MOP_HOME: code {code!r}, "
                        f"plays {plays!r}, err {err!r}",
                        not (code != 1 or plays or len(lines) != 1
                             or not lines[0].startswith("plain: mop_home=")))
            # #186: опечатка в драйвере хоста — отказ до плейбука, строка на хост.
            listing[0] = INVENTORY_TYPO
            for argv in ([], ["--check"]):
                calls.clear(), collected.clear()
                out, err, code = run_command(deploy.main, argv)
                plays = [cmd for cmd in calls if cmd and cmd[0] == "ansible-playbook"]
                lines = [lib.plain(l) for l in err.strip().splitlines()]
                c.check(f"deploy {argv} over a driver typo: code {code!r}, "
                        f"plays {plays!r}, err {err!r}",
                        not (code != 1 or plays or collected or len(lines) != 1
                             or not lines[0].startswith("typo: unknown driver 'pvee'")))
    finally:
        os.environ.clear()
        os.environ.update(env)


def check_agent_on_node_172(c):
    """#172: юнит агента переезжает с `python3 -m mop.agent` на `mop agent`,
    а на узле .env нет -- только node.env. Считалось, что диспетчер откажет
    там на REQUIRED; не отказывает: require зовёт только lib.cluster, а
    командлет агента его не берёт. Проверка зелёная с первого прогона и
    закрепляет нынешнее поведение, а не чинит: отказ здесь уронил бы агента
    на каждом узле, как только юнит переедет. Живьём то же на hyper:
    `cd / && ~/mop/bin/mop agent --check` -- exit 0, subscribed.

    Узел здесь -- временный дом с одним node.env, .env нет, каталог `/`.
    Дальше отказа на настройках команда дойти обязана: до агента -- его
    отказ на кредах шины, либо, без nats-py, отказ командлета про библиотеку.
    STATUS: FIXED — see #172"""
    import subprocess
    import tempfile
    home = tempfile.mkdtemp(prefix="mop-node-172-")
    os.makedirs(os.path.join(home, ".config", "mop"))
    with open(os.path.join(home, ".config", "mop", "node.env"), "w") as f:
        f.write("MOP_DRIVER=host\nMOP_USER=mopuser\n")
    env = hermetic.child_env({"HOME": home,
                              "MOP_ENV_FILE": os.path.join(home, "no-such.env"),
                              "MOP_NODE": "hyper"})
    r = subprocess.run(["bash", os.path.join(ROOT, "bin", "mop"), "agent", "--check"],
                       cwd="/", env=env, capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    reached = "no bus credentials" in out or "bus library needed" in out
    c.check("mop agent --check on a node with node.env and no .env must reach the agent",
            not ("required settings not filled" in out or not reached),
            f"rc {r.returncode}, {out.strip()!r}")


def check_bus_import_169(c):
    """HYPOTHESIS (#169): mop/common/bus.py при импорте без nats-py зовёт sys.exit --
    библиотека кончает процесс сама, и тот, кто её импортировал, не может ни
    перехватить отказ, ни сказать его своими словами (#150: `-m mop.agent`
    печатал текст bus вместо своего).
    SOLUTION: bus бросает ImportError с тем же текстом; диспетчер импортирует
    командлет внутри cli.run, и run делает из ImportError одну строку в
    stderr и код 1.
    STATUS: FIXED — see #169"""
    missing_library(c, "nats", "mop.common.bus", "bus library required: pip install --user "
                           "--break-system-packages nats-py")


def check_nomad_import_187(c):
    """HYPOTHESIS (#187): mop/server/nomad.py при импорте без python-nomad зовёт
    sys.exit -- тот же дефект, что у bus в #169: библиотека кончает процесс
    сама.
    SOLUTION: как в #169 -- ImportError с тем же текстом, одну строку из него
    делает cli.run.
    STATUS: FIXED — see #187"""
    # Команда мастер-шелла python-nomad больше не импортирует (#81, слои
    # #258: nomad -- сервер), поэтому пробуем командой контроллера.
    missing_library(c, "nomad", "mop.server.nomad", "API library required: pip install --user "
                           "--break-system-packages python-nomad",
                           command="mop.cli.driver.build", argv=["--nope"])


def missing_library(c, lib, module, text, command="mop.cli.core.restart", argv=()):
    """Импорт module без библиотеки lib -- ImportError с текстом text, а
    командлет command, которому module нужен, через cli.run -- одна строка в
    stderr и код 1. Модули перезагружаются: проверка идёт последней."""
    import importlib
    import importlib.abc

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
        c.fail("the network guard did not hold")
        undo()
        return
    except NetworkGuard:
        pass
    keep = dict(sys.modules)
    blocker = NoLib()
    sys.meta_path.insert(0, blocker)
    try:
        evict(True)
        try:
            importlib.import_module(module)
            c.fail(f"importing {module} without {lib} must raise ImportError")
        except ImportError as e:
            c.expect(f"{module} without {lib}: the ImportError text", str(e), text)
        except SystemExit as e:
            c.fail(f"{module} without {lib} ends the process: SystemExit({e.code!r})")
        evict(False)
        # Командлет, которому нужна шина, через диспетчер: одна строка, код 1.
        # Без аргументов: если шина вдруг импортируется, командлет остановится
        # на usage, а не пойдёт в сеть.
        try:
            _, err, code = run_command(cli.command(command), argv, via_cli=True)
        except BaseException as e:  # noqa: BLE001 -- ушедшее мимо cli.run и есть дефект
            err, code = "", f"escaped {type(e).__name__}"
        c.check(f"a commandlet without {lib} through cli.run: code {code!r}, stderr {err!r}",
                not (code != 1 or err != text + "\n"))
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


def check_fallback_model_183(c):
    """HYPOTHESIS (#183): puppets.py читал MOP_FALLBACK_MODEL на уровне модуля,
    и всякий, кто импортирует puppets (сервис кластера), считался читающим
    эту настройку, хотя применяет её один treat() -- `mop doctor --fix` на
    машине оператора.
    SOLUTION: treat() читает её при вызове; на уровне модуля чтения нет.
    STATUS: FIXED — see #183"""
    import ast
    from mop.common import puppets
    tree = ast.parse(open(os.path.join(ROOT, "mop", "common", "puppets.py")).read())
    top = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                        ast.ClassDef))]
    c.check("puppets.py reads MOP_FALLBACK_MODEL at import, not in treat()",
            not (any("MOP_FALLBACK_MODEL" in ast.unparse(n) for n in top)))
    typed = []
    # force -- лечение проходит ворота владения (#40).
    with offline(), \
            patched(puppets, switch_model=lambda node, name, model, force=False: typed.append(model)), \
            patched_env(MOP_FALLBACK_MODEL="sonnet-for-183"):
        got = puppets.treat({"action": "model", "name": "pu-mop-1",
                             "alloc": {"NodeName": "n1"}})
    c.check(f"treat(model) must use MOP_FALLBACK_MODEL as it is at the call: "
            f"{got!r}, typed {typed}",
            not (got != "/model sonnet-for-183" or typed != ["sonnet-for-183"]))
    # ── #251: отвергнутый вход не оставляет мусора aiohttp ──────────────
    # HYPOTHESIS: bus.check / ask_once / connect зовут nats.connect(), который
    # при отказе (allow_reconnect=False) бросает до закрытия транспорта;
    # websocket-транспорт nats-py 2.15 держит aiohttp.ClientSession, и при
    # выходе процесса сборщик печатает «Unclosed client session» -- после
    # запроса пароля mop join успех неотличим от ошибки.
    # SOLUTION: одна точка соединения bus._open: клиент nats.NATS(), connect
    # в try, при отказе nc.close() (закрывает транспорт) и исключение дальше.
    # STATUS: FIXED — see #251
    from mop.common import bus as _bus
    closed = []

    class FakeNATS:
        def __init__(self):
            self.is_closed = False

        async def connect(self, **kw):
            raise OSError("refused by the check")

        async def close(self):
            closed.append(True)

    def refuse(*a, **k):
        raise OSError("nats.connect must not be used: the client must be closable")
    with patched(sys.modules.get("nats"), NATS=FakeNATS, connect=refuse):
        conn = {"url": "wss://192.0.2.1:443/nats", "user": "u", "password": "p"}
        for what, call in (("check", lambda: _bus.check(conn)),
                           ("ask_once", lambda: _bus.ask_once(conn, "s", "ping", timeout=1))):
            closed.clear()
            try:
                call()
                c.fail(f"#251 {what}: a refused connect must raise")
            except Exception as e:
                c.check(f"#251 {what}: must raise a RuntimeError, got {type(e).__name__}: {e}",
                        not (not isinstance(e, RuntimeError)))
            c.expect(f"#251 {what}: the client must be closed once on refusal", closed, [True])
    # ── #252: сообщения наружу без номеров тикетов ──────────────────────
    # HYPOTHESIS: строки, которые видит человек в терминале или модель через
    # MCP, несли «(#82)», «before #211», «(#61)»: довод, которому место в
    # комментарии рядом с кодом, а не в сообщении. SOLUTION: разбор AST всего
    # mop/ -- строковые константы в аргументах print / sys.exit / lib.fail /
    # lib.usage / конструкторов исключений не содержат `#<число>`.
    # STATUS: FIXED — see #252
    import ast as _ast2
    import re as _re
    ticket = _re.compile(r"#\d{2,}")
    SPEAK = {"print", "exit", "fail", "usage", "ok", "section"}

    def _consts(node):
        if isinstance(node, _ast2.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, _ast2.JoinedStr):
            for v in node.values:
                yield from _consts(v)
        elif isinstance(node, _ast2.BinOp):
            yield from _consts(node.left)
            yield from _consts(node.right)

    def _speaks(call):
        f = call.func
        name = f.id if isinstance(f, _ast2.Name) else f.attr if isinstance(f, _ast2.Attribute) else ""
        return name in SPEAK or name.endswith(("Error", "Exception", "Refused", "Missing"))

    root = os.path.dirname(cli.PACKAGE)
    spoken = []
    for d, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(d, f)
            with open(path) as fh:
                tree = _ast2.parse(fh.read())
            for node in _ast2.walk(tree):
                if isinstance(node, _ast2.Call) and _speaks(node):
                    for a in node.args:
                        for text in _consts(a):
                            if ticket.search(text):
                                spoken.append(f"{os.path.relpath(path, root)}:{node.lineno}: {text.strip()[:60]!r}")
    c.check("#252 messages must not cite tickets", not (spoken), "\n  " + "\n  ".join(spoken))
    # #255: докстринг командлета -- usage, строка help и описание инструмента
    # MCP, то есть тоже сообщение наружу. `epic #12` в примере -- формат
    # номера, а не ссылка.
    # STATUS: FIXED — see #255
    told = []
    for d, _, files in os.walk(cli.PACKAGE):
        for f in files:
            if f.endswith(".py") and (not f.startswith("_") or f == "__init__.py"):
                path = os.path.join(d, f)
                for i, line in enumerate(cli.docstring(path).splitlines(), 1):
                    if ticket.search(_re.sub(r"epic #\d+", "", line)):
                        told.append(f"{os.path.relpath(path, cli.PACKAGE)}:{i}: {line.strip()[:60]!r}")
    c.check("#255 usage and help must not cite tickets", not (told), "\n  " + "\n  ".join(told))

    # ── #254: bug и ci -- в пространстве dev, без MCP ────────────────────
    # HYPOTHESIS: трекер и CI проекта -- команды разработчика, а лежат среди
    # команд пула и торчат в MCP инструментами ci_*. SOLUTION: mop/cli/dev/
    # {bug,ci}, группа dev в PRIVATE, прежние имена -- через LEGACY один релиз.
    # STATUS: FIXED — see #254
    tree = cli.verbs()
    # bug и ci -- в dev; сам dev растёт (#271 -- test), поэтому «содержит».
    c.check(f"#254 dev must hold bug and ci: {sorted(tree.get('dev') or {})}",
            not (not {"bug", "ci"} <= set(tree.get("dev") or {})))
    top = cli.catalog(cli.scan())
    c.check("#254 bug and ci must not stay top-level commands", not ("bug" in top or "ci" in top))
    c.check(f"#254 LEGACY must map bug and ci into dev: {cli.LEGACY}",
            not (cli.LEGACY.get("bug") != ("dev", "bug") or cli.LEGACY.get("ci") != ("dev", "ci")))
    for name, words, path in cli.tool_commands(cli.scan(), tree, private=()):
        c.check(f"#254 {' '.join(words)} must not declare MCP",
                not (words[0] == "dev" and cli.declared(path) is not None))
    c.check("#254 no dev tool may reach MCP",
            not (any(n.startswith("dev") or n.startswith("ci_") for n, _, _ in
                     cli.tool_commands(cli.scan(), tree))))
    got = cli.resolve(cli.unalias(["bug", "list"]), top, tree)
    c.expect("#254 `mop dev bug list` must still resolve through LEGACY", got,
             ("mop.cli.dev.bug.list", []))

    check_server_namespace_259(c)
    check_named_263(c)
    check_body_file_270(c)


def check_server_namespace_259(c):
    """HYPOTHESIS (#259): команды контроллера -- deploy, config, setup
    контроллера, user, cluster, bootstrap, web, callout, pve-facts -- лежат
    среди команд пула и оператора, и что из них делается только на сервере,
    по имени не видно.
    SOLUTION: пространство mop server; `mop setup` наверху -- только машина
    оператора. Прежние имена -- через LEGACY один релиз: юниты и роли
    зовут их до своего тикета. `mop driver pve-facts` -- двухсловный ключ
    LEGACY: driver как целое не псевдоним, run/list/sweep/build в нём.
    STATUS: FIXED — see #259"""
    want = {"deploy", "config", "setup", "user", "cluster", "bootstrap", "web",
            "callout", "pve-facts"}
    tree = cli.verbs()
    got = set(tree.get("server") or {})
    c.expect("#259 server must hold exactly its verbs", sorted(got), sorted(want))
    top = cli.catalog(cli.scan())
    stay = sorted((want - {"setup"}) & set(top))
    c.check(f"#259 must not stay top-level: {stay}", not (stay))
    c.check("#259 mop setup stays top-level, for the operator's machine", not ("setup" not in top))
    c.expect("#259 driver keeps run/list/sweep/build",
             set(tree.get("driver") or {}), {"run", "list", "sweep", "build"})
    for old in ("deploy", "config", "user", "cluster", "bootstrap", "web", "callout"):
        c.expect(f"#259 LEGACY must map {old} into server", cli.LEGACY.get(old), ("server", old))
    for argv, want_argv in [(["driver", "pve-facts", "--base", "1"],
                             ["server", "pve-facts", "--base", "1"]),
                            (["driver", "run", "x"], ["driver", "run", "x"]),
                            (["cluster", "users"], ["server", "cluster", "users"])]:
        c.expect(f"#259 unalias({argv})", cli.unalias(argv), want_argv)
    for argv, mod in [(["deploy"], ("mop.cli.server.deploy", [])),
                      (["cluster", "users", "--reload"], ("mop.cli.server.cluster.users", ["--reload"])),
                      (["driver", "pve-facts", "--base", "9"], ("mop.cli.server.pve-facts", ["--base", "9"])),
                      (["server", "user", "add", "x"], ("mop.cli.server.user.add", ["x"]))]:
        got = cli.resolve(cli.unalias(argv), top, tree)
        c.expect(f"#259 {argv} must resolve to {mod}", got, mod)
    # ── #271: mop dev test -- прогон tests/ одной командой ──────────────
    # HYPOTHESIS: проверки гоняли шелл-циклом `for t in tests/*.py`, у
    # каждого свой (CI, папеты, мастер), с разным разбором итога. SOLUTION:
    # `mop dev test [имя ...] [--strict]`; план и итог -- чистые функции.
    # STATUS: FIXED — see #271
    try:
        from mop.cli.dev import test as dev_test
    except ImportError:
        dev_test = None
    if c.check("#271 mop.cli.dev.test with plan() and summary() is missing",
               not (dev_test is None or not hasattr(dev_test, "plan") or not hasattr(dev_test, "summary"))):
        listing = ["cli.py", "agent.py", "_lib.py", "README", "hermetic.py", "spec_snapshot.json"]
        for names, want in (([], ["agent.py", "cli.py", "hermetic.py"]),
                            (["cli"], ["cli.py"]),
                            (["tests/agent.py", "cli.py", "agent"], ["agent.py", "cli.py"])):
            got = dev_test.plan(names, listing)
            c.expect(f"#271 plan({names})", got, want)
        for bad in (["nope"], ["_lib"], ["spec_snapshot.json"]):
            try:
                dev_test.plan(bad, listing)
                c.fail(f"#271 plan({bad}) must refuse")
            except ValueError as e:
                c.check(f"#271 plan({bad}) refusal must name the file: {e}",
                        not ("tests/" not in str(e)))
        lines, ok = dev_test.summary([("agent.py", 0, "agent: ok\n", 1.0),
                                      ("cli.py", 1, "FAIL x\ncli: FAILED\n", 2.0)])
        c.check(f"#271 summary of a red run: {ok} {lines}",
                not (ok or lines[0] != "FAILED tests/cli.py (exit 1)" or "FAIL x" not in lines
                     or lines[-1] != "1/2 ok, failed: cli.py"))
        lines, ok = dev_test.summary([("agent.py", 0, "agent: ok\n", 1.0)])
        c.check(f"#271 summary of a green run: {ok} {lines}", not (not ok or lines != ["1/1 ok"]))
        c.check("#271 test must be a verb of the private dev namespace",
                not ("dev" not in cli.PRIVATE or "test" not in (cli.verbs().get("dev") or {})))


def check_named_263(c):
    """HYPOTHESIS (#263): delete, recycle, restart, wipe и update каждый
    повторяют разбор --force, проверку одного имени с usage, перила guard и
    печать owner_note; --dry -- в gc и двух sweep; фабрика argparse -- в
    bug и ci. Пять копий разойдутся на первой же правке одной.
    SOLUTION: lib.named / lib.parse_named, lib.note, lib.dry, одна фабрика
    parser в dev/_common. Вывод, usage и коды выхода -- те же: их держит
    характеризация ниже, снятая с кода до переделки.
    STATUS: FIXED — see #263"""
    import importlib
    from mop.cli import lib
    from mop.common import bus, puppets
    from mop.cli.core import _common as core_common

    # ── помощник: разбор имени и --force ──────────────────────────────────
    doc = "do a thing: mop thing <name> [--force]"
    fn = getattr(lib, "named", None)
    if not c.check("#263 lib.named is missing", fn is not None):
        return
    for argv, want in [(["pu-mop-1"], ("pu-mop-1", False)),
                       (["--force", "pu-mop-1"], ("pu-mop-1", True)),
                       (["pu-mop-1", "--force"], ("pu-mop-1", True))]:
        got = fn(argv, doc)
        c.expect(f"#263 named({argv})", got, want)
    for argv in ([], ["a", "b"], ["--force"]):
        try:
            fn(argv, doc)
            c.fail(f"#263 named({argv}) must refuse through usage")
        except SystemExit as e:
            c.expect(f"#263 named({argv}) must exit with the usage", str(e.code), doc)
    c.expect("#263 parse_named with most=2",
             lib.parse_named(["x", "git@h:o.git", "--force"], doc, most=2), (["x", "git@h:o.git"], True))
    for argv, want in (([], False), (["--dry"], True)):
        c.check(f"#263 dry({argv})", not (lib.dry(argv, doc) is not want))
    try:
        lib.dry(["--wet"], doc)
        c.fail("#263 dry(['--wet']) must refuse through usage")
    except SystemExit:
        pass
    from mop.cli.dev.bug import _common as bug_c
    from mop.cli.dev.ci import _common as ci_c
    c.check("#263 the argparse factory keeps prog and no built-in help",
            not (bug_c.parser("new").prog != "mop dev bug new" or ci_c.parser("log").prog != "mop dev ci log"
                 or bug_c.parser("new").add_help))

    # ── характеризация пяти команд: вывод тот же, что до переделки ────────
    calls = []
    note = {"owner_note": "was olga's: taken with --force"}

    def call_cluster(verb, **kw):
        calls.append((verb, kw.get("force")))
        if verb == "alloc":
            return {"alloc": {"NodeName": "hyper"}}
        if verb == "spec":
            return {"meta": {"origin": "git@h:g/mop.git"}}
        return dict(note)
    def run(module, argv):
        mod = importlib.import_module(f"mop.cli.core.{module}")
        return (*run_command(mod.main, argv, via_cli=True), mod.__doc__.strip())
    with patched_env(MOP_SERVER_LAN="192.0.2.1"), \
            patched(lib, in_project=lambda: None), \
            patched(bus, call_cluster=call_cluster, login=lambda: "anton"), \
            patched(puppets,
                    delete=lambda name, force=False: calls.append(("delete", force)) or
                    {**note, "body": "destroyed", "node": "hyper"},
                    recycle=lambda name, workspace_of=None, force=False:
                    calls.append(("recycle", force)) or dict(note),
                    wipe=lambda node, name, force=False:
                    calls.append(("wipe", force)) or {**note, "target": "/t"},
                    running_alloc=lambda name: {"NodeName": "hyper"}), \
            patched(core_common, push_llm_keys=lambda p: None, workspace_text=lambda o: None):
        line = "pu-mop-1: was olga's: taken with --force\n"
        want = {
            "delete": line + "deleted pu-mop-1 (body gone from hyper)\n",
            "recycle": line, "restart": line,
            "wipe": line + "pu-mop-1: clone reset to HEAD, target wiped (/t)\n",
            "update": line,
        }
        for module, out_want in want.items():
            calls.clear()
            out, err, code, usage = run(module, ["pu-mop-1", "--force"])
            forced = [f for v, f in calls if v in ("delete", "recycle", "wipe", "restart", "update")]
            c.check(f"#263 mop {module} pu-mop-1 --force: {(out, err, code)!r}, force {forced}",
                    not ((out, err, code) != (out_want, "", 0) or forced != [True]))
            for bad in ([], ["a", "b", "c"]) if module == "update" else ([], ["a", "b"]):
                out, err, code, usage = run(module, bad)
                # usage -- SystemExit(докстринг): в процессе он -- код выхода,
                # печатает его интерпретатор на выходе (stderr, код 1).
                c.check(f"#263 mop {module} {bad}: must exit with its usage",
                        not ((out, err, code) != ("", "", usage)), repr((out, err, str(code)[:60])))


def check_body_file_270(c):
    """HYPOTHESIS (#270): `mop dev bug new --body-file -` и любой
    несуществующий путь падают трассировкой FileNotFoundError в read_body:
    `-` читался как имя файла, а отсутствие файла не ловил никто.
    SOLUTION: `-` в --body-file -- stdin, как у текста комментария; файла нет
    или он не читается -- отказ одной строкой с путём.
    STATUS: FIXED — see #270"""
    import io
    import tempfile
    from mop.cli.dev.bug import _common as bug_common
    try:
        with patched(sys, stdin=io.StringIO("тело из stdin\n")):
            got = bug_common.read_body(None, "-")
        c.expect("#270 --body-file - must read stdin", got, "тело из stdin")
    except BaseException as e:  # noqa: BLE001 -- трассировка и есть дефект
        c.fail(f"#270 --body-file - must read stdin, raised {type(e).__name__}: {e}")
    missing = os.path.join(tempfile.mkdtemp(prefix="mop-test-270-"), "no-such.md")
    for path in (missing, tempfile.mkdtemp(prefix="mop-test-270-")):
        try:
            bug_common.read_body(None, path)
            c.fail(f"#270 --body-file {path} must be refused")
        except SystemExit as e:
            text = str(e.code)
            c.check(f"#270 the refusal must be one line naming the path: {text!r}",
                    not (path not in text or "\n" in text.strip() or "Traceback" in text))
        except BaseException as e:  # noqa: BLE001
            c.fail(f"#270 --body-file {path}: a traceback ({type(e).__name__}), "
                   f"not a one-line refusal")
    # Как было: файл -- его текст, аргумент -- он сам.
    ok = os.path.join(tempfile.mkdtemp(prefix="mop-test-270-"), "body.md")
    with open(ok, "w", encoding="utf-8") as f:
        f.write("  тело  \n")
    c.check("#270 a readable file and a text argument must work as before",
            not (bug_common.read_body(None, ok) != "тело" or bug_common.read_body(" x ", None) != "x"))


if __name__ == "__main__":
    sys.exit(main())
