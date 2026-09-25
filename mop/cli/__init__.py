"""Диспетчер командлетов: `mop <команда> [аргументы]` -> модуль mop.cli.*.

Командлеты жили исполняемыми файлами в bin/ (#74): наружу нужен один `mop`,
а каталог целиком в PATH класть нельзя — `list`, `code`, `node`, `stat`
перекрыли бы системные. Исполняемыми они были лишь номинально: запускал их
только диспетчер, и он же подставлял PYTHONPATH, без которого они не находили
пакет. Модуль в пакете снимает и это, и `import lib` через sys.path:
команда — это `mop.cli.<секция>.<имя>`, группа — подпакет.

Два вида каталогов, и различает их список SECTIONS — единственное место,
где это записано:
  секция   плоские команды, имя каталога в вызове не участвует:
           mop/cli/core/list.py  ->  mop list
  группа   подпакет, имя каталога и есть команда, глагол — модуль в нём:
           mop/cli/driver/build.py  ->  mop driver build
           mop/cli/driver/__init__.py отвечает без глагола (и, пока группу
           не разрезали по глаголам, — за все её глаголы сразу, #77)

Контракт команды: модуль с докстрингом (первая строка — описание в help,
весь текст — usage) и `main(argv) -> int`. Печатает только этот пакет;
остальной `mop/` возвращает данные и молчит — граница та же, что была
между bin/ и mop/, только записана каталогом внутри пакета.

Описание для help читается из файла разбором AST, без импорта: импорт тянул
бы шину и падал бы на машине без nats-py ради одной строки списка.

"""
import ast
import importlib
import os
import sys

PACKAGE = os.path.dirname(os.path.realpath(__file__))
PROJECT = os.path.dirname(os.path.dirname(PACKAGE))
BIN = os.path.join(PROJECT, "bin")

# Секции: плоские имена. Порядок — порядок в help: ежедневное первым,
# служебное последним.
SECTIONS = ("core", "pool", "service")
TITLES = {
    "core": "every day, from a master shell",
    "pool": "the pool and its machines, for the operator",
    "service": "run by systemd and the job spec, not by hand",
}


# ─── чистое: каталог, разбор, описание ───────────────────────────────────
def scan(package=PACKAGE):
    """Обход пакета -> [(каталог, имя, подпакет?)]: модули секций и
    подпакеты-группы. Служебное (lib, _*) не команды."""
    found = []
    for entry in sorted(os.listdir(package)):
        path = os.path.join(package, entry)
        if entry.startswith("_") or entry == "lib.py" or not os.path.isdir(path):
            continue
        if not os.path.exists(os.path.join(path, "__init__.py")):
            continue
        if entry in SECTIONS:
            for m in sorted(os.listdir(path)):
                if m.endswith(".py") and not m.startswith("_"):
                    found.append((entry, m[:-3], False))
        else:
            found.append((entry, "", True))
    return found


def verbs(package=PACKAGE):
    """{группа: дерево} -- глаголы групп (#253). Модуль -- {}, подпакет
    внутри группы -- подгруппа со своим деревом: `mop dev bug new`,
    `mop server user add`. Служебное (_*) -- не глаголы."""
    return {section: _tree(os.path.join(package, section))
            for section, _, is_pkg in scan(package) if is_pkg}


def _tree(path):
    out = {}
    for entry in sorted(os.listdir(path)):
        if entry.startswith("_"):
            continue
        full = os.path.join(path, entry)
        if entry.endswith(".py"):
            out[entry[:-3]] = {}
        elif os.path.isdir(full) and os.path.exists(os.path.join(full, "__init__.py")):
            out[entry] = _tree(full)
    return out


# Пространства команд, которых нет в MCP (#253): внутренние команды
# разработчика (трекер, CI) модель зовёт из шелла мастера, инструментами
# они не нужны.
PRIVATE = ("dev",)

# Прежние имена команд -> новые слова (#253): переезд команды в пространство
# не ломает юниты, спеку и запущенных мастеров до раскатки и перезапуска.
# Молча: это не отказ и не совет, а переход на один релиз, после которого
# таблица пустеет отдельным тикетом.
LEGACY = {"bug": ("dev", "bug"), "ci": ("dev", "ci")}   # #254


def unalias(argv, table=None):
    """argv с прежним именем команды -> argv с новыми словами. Чистая функция."""
    table = LEGACY if table is None else table
    if argv and argv[0] in table:
        return list(table[argv[0]]) + list(argv[1:])
    return list(argv)


def catalog(found):
    """[(каталог, имя, подпакет?)] -> {имя команды: модуль}. Одно имя в двух
    местах — отказ, а не «кто первый»: разойдясь, две одноимённые команды
    молча выбирались бы порядком обхода."""
    out = {}
    for section, name, is_pkg in found:
        key = section if is_pkg else name
        mod = f"mop.cli.{section}" if is_pkg else f"mop.cli.{section}.{name}"
        if key in out:
            raise RuntimeError(f"command {key} is defined twice: {out[key]} and {mod}")
        out[key] = mod
    return out


def resolve(argv, cat, group_verbs):
    """argv -> (модуль, остаток argv) либо None, если команды нет.

    Глагол группы, у которого есть свой модуль, получает остаток без себя;
    иначе отвечает сама группа и получает argv целиком — так группа живёт
    одним модулем, пока её не разрезали (#77)."""
    if not argv:
        return None
    name = argv[0]
    mod = cat.get(name)
    if mod is None:
        return None
    rest, tree = list(argv[1:]), group_verbs.get(name) or {}
    # Спуск по дереву (#253): подгруппа -- ещё один уровень, модуль -- дно.
    while rest and rest[0] in tree:
        mod, tree, rest = f"{mod}.{rest[0]}", tree[rest[0]] or {}, rest[1:]
    return mod, rest


def docstring(path):
    """Докстринг модуля целиком, без импорта; пусто, если нет. Он же usage
    команды и описание её инструмента в MCP (#160)."""
    try:
        with open(path) as f:
            doc = ast.get_docstring(ast.parse(f.read()))
    except (OSError, SyntaxError):
        return ""
    return (doc or "").strip()


def describe(path):
    """Первая строка докстринга модуля, без импорта; пусто, если нет."""
    doc = docstring(path)
    return doc.splitlines()[0].strip() if doc else ""


# ─── чистое: команда как инструмент MCP (#160) ───────────────────────────
# Командлет объявляет себя инструментом константой `MCP` в своём модуле:
#   MCP = {"annotations": "destructive" | "readonly",
#          "background": True,          # итог -- вестью в сессию, не ответом
#          "args": [{"name", "type": string|integer|boolean,
#                    "flag": "--x" (без него -- позиционный),
#                    "required": bool, "help": "..."}]}
# Таблицы команд нет: объявление живёт рядом с разбором argv, который оно
# описывает, и расходиться им негде.
DECL_KEYS = {"annotations", "background", "args"}
ARG_KEYS = {"name", "type", "flag", "required", "help"}
ARG_TYPES = ("string", "integer", "boolean")
ANNOTATIONS = ("readonly", "destructive")


def declared(path):
    """Объявление `MCP` модуля, без импорта; None -- команда в MCP не видна.
    Кривое объявление -- ValueError: пропавший молча инструмент искали бы
    долго, а отказ на старте `mop mcp` называет файл."""
    with open(path) as f:
        tree = ast.parse(f.read())
    node = next((n for n in tree.body if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "MCP" for t in n.targets)), None)
    if node is None:
        return None
    try:
        decl = ast.literal_eval(node.value)
    except ValueError:
        raise ValueError("MCP must be a literal dict")
    if not isinstance(decl, dict) or set(decl) - DECL_KEYS:
        raise ValueError(f"MCP keys are {', '.join(sorted(DECL_KEYS))}")
    if decl.get("annotations") not in (None, *ANNOTATIONS):
        raise ValueError(f"MCP annotations: one of {', '.join(ANNOTATIONS)}")
    optional_seen = False
    for a in decl.get("args", []):
        if set(a) - ARG_KEYS or not str(a.get("name", "")).isidentifier():
            raise ValueError(f"MCP arg {a!r}: keys are {', '.join(sorted(ARG_KEYS))}, "
                             f"name an identifier")
        if a.get("type") not in ARG_TYPES:
            raise ValueError(f"MCP arg {a['name']}: type one of {', '.join(ARG_TYPES)}")
        if a["type"] != "string" and not a.get("flag"):
            raise ValueError(f"MCP arg {a['name']}: a {a['type']} needs a flag")
        if not a.get("flag"):
            # Необязательный позиционный -- только в хвосте: пропуск в
            # середине сдвинул бы следующие на чужое место.
            if a.get("required") and optional_seen:
                raise ValueError(f"MCP arg {a['name']}: a required positional "
                                 f"after an optional one")
            optional_seen = optional_seen or not a.get("required")
    return decl


def tool_commands(found, group_verbs, private=PRIVATE):
    """[(каталог, имя, подпакет?)] -> [(имя инструмента, слова команды,
    путь модуля)]. Группа -- сама и каждый её глагол, вглубь (#253):
    `node_drain`, `server_user_add`. Пространства из private пропускаются."""
    out = []
    for section, name, is_pkg in found:
        if not is_pkg:
            out.append((name, [name], _path_of(section, name, False)))
        elif section not in private:
            out += _tools_of([section], group_verbs.get(section) or {}, True)
    return out


def _tools_of(words, tree, group):
    """Инструменты группы или глагола и всего под ним. group -- это пакет
    (сама группа или подгруппа), а не модуль."""
    base = os.path.join(PACKAGE, *words)
    path = os.path.join(base, "__init__.py") if group else base + ".py"
    out = [("_".join(words), list(words), path)]
    for verb in sorted(tree):
        sub = tree[verb] or {}
        out += _tools_of(words + [verb], sub,
                         bool(sub) or os.path.isdir(os.path.join(base, verb)))
    return out


def tool_argv(args, values):
    """Значения инструмента -> argv командлета. ValueError -- отказ модели.

    Позиционное значение с дефисом впереди -- отказ: командлет прочёл бы его
    флагом, и одно поле включало бы чужой флаг."""
    argv, flags, skipped = [], [], None
    for a in args:
        v = values.get(a["name"])
        if a.get("flag"):
            if a["type"] == "boolean":
                flags += [a["flag"]] if v else []
            elif v not in (None, ""):
                flags += [a["flag"], str(v)]
            continue
        if v in (None, ""):
            if a.get("required"):
                raise ValueError(f"{a['name']} is required")
            skipped = skipped or a["name"]
            continue
        if skipped:
            raise ValueError(f"{a['name']} needs {skipped} too: positionals go in order")
        if str(v).startswith("-"):
            raise ValueError(f"{a['name']}: {v!r} would read as a flag")
        argv.append(str(v))
    return argv + flags


def _path_of(section, name, is_pkg):
    return os.path.join(PACKAGE, section, "__init__.py" if is_pkg else f"{name}.py")


def usage(found=None):
    """Список команд по секциям — ответ на `mop` без аргументов."""
    found = scan() if found is None else found
    lines = ["mop — pool of claude puppets on top of Nomad, message bus on top of NATS",
             "", "  mop [--server ADDRESS] <command> [arguments]",
             "", "  the server and the login come from the working copy (git config",
             "  mop.server, mop.user), then MOP_SERVER_LAN / MOP_BUS_USER, then",
             "  --server; with none of them, from .env. Your integration branch,",
             "  if not the repository's default: git config mop.branch, or MOP_BRANCH"]
    groups = [(s, n, p) for s, n, p in found if p]
    for section in SECTIONS:
        rows = [(n, describe(_path_of(section, n, False)))
                for s, n, p in found if s == section]
        if section == "service" and groups:
            lines += ["", "  groups: mop <group> <verb>, mop <group> alone for the verbs"]
            lines += [f"    {s:<10} {describe(_path_of(s, '', True))}" for s, _, _ in groups]
        lines += ["", f"  {TITLES[section]}:"]
        lines += [f"    {n:<10} {d}" for n, d in rows]
    lines += ["", "  mop help     this list"]
    return "\n".join(lines)


# ─── запуск ──────────────────────────────────────────────────────────────
def main(argv):
    # Глобальная опция (#131): сервер для любой команды, с любого места argv.
    from mop import context
    try:
        server, argv = context.strip_server(argv)
    except ValueError as e:
        sys.exit(f"mop: {e}")
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(usage())
        return 0
    argv = unalias(argv)
    try:
        found = resolve(argv, catalog(scan()), verbs())
    except RuntimeError as e:
        sys.exit(f"mop: {e}")
    if found is None:
        print(f"mop: no such subcommand «{argv[0]}»", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 1
    modname, rest = found
    # Контекст -- до environment(): тот читает настройки, и сервер в них
    # должен быть уже этой команды. Слои: клон < окружение < --server.
    with context.use(context.here({"server": server} if server else {})):
        environment()
        return run(command(modname), rest)


FALLBACK_LOCALE = "C.UTF-8"


def locale_usable(name):
    """Есть ли такая локаль на этой машине.

    Проба меняет локаль ПРОЦЕССА, и вернуть надо ровно ту, что была, а не
    «C»: от локали процесса зависит кодировка, которую питон берёт для
    open() без явного encoding. Возврат в «C» превращал её в ascii, и первое
    же чтение русского файла падало UnicodeDecodeError — поймано на
    `mop dev bug new --body-file`."""
    import locale as loc
    keep = loc.setlocale(loc.LC_ALL)
    try:
        loc.setlocale(loc.LC_ALL, name)
        return True
    except (loc.Error, ValueError):
        return False
    finally:
        try:
            loc.setlocale(loc.LC_ALL, keep)
        except (loc.Error, ValueError):
            pass


def run_locale(wanted, usable=locale_usable):
    """Локаль прогонов: локаль УСТАНОВКИ, если она на машине есть.

    Считается от настройки, а не от унаследованного окружения, и это не
    придирка. Окружение как раз и бывает сломано: ssh привозит `LC_*` с
    машины оператора, на сервере такой локали нет, и ansible отказывается
    стартовать — при том что `LANG` и `LC_CTYPE` выглядят исправными, так что
    по ним не понять ничего. Мы ставим `LC_ALL`, а он старше всех категорий
    разом, поэтому чинит и чужие `LC_TIME` с `LC_NUMERIC`.

    Ansible требует UTF-8; запасная — встроенная в glibc `C.UTF-8`, она есть
    везде и генерации не требует."""
    wanted = (wanted or "").strip()
    if not wanted.upper().replace("-", "").replace("_", "").endswith("UTF8"):
        return FALLBACK_LOCALE
    return wanted if usable(wanted) else FALLBACK_LOCALE


def environment():
    """Окружение прогонов ansible: инвентарь установки, свой ansible.cfg,
    роли продукта. Раньше это ставил bin/common для bash-командлетов;
    теперь здесь, потому что их читают и ansible, и библиотека
    (image.bake — INVENTORY), а зовут из любого каталога. setdefault:
    разовое `INVENTORY=… mop deploy` обязано продолжать работать."""
    os.environ.setdefault("INVENTORY", os.path.join(PROJECT, "inventory.yaml"))
    # Свой ansible.cfg ansible ищет относительно текущего каталога; роли —
    # переменной, а не строкой в ansible.cfg: относительные пути там от
    # текущего каталога, а абсолютные были бы литералом конкретной машины.
    os.environ.setdefault("ANSIBLE_CONFIG", os.path.join(PROJECT, "ansible.cfg"))
    os.environ.setdefault("ANSIBLE_ROLES_PATH", os.path.join(PROJECT, "deploy", "roles"))
    from .. import config as _config
    # Замер времени задач — только по просьбе установки (#103): он нужен при
    # разборе раскатки и мешает во всех остальных прогонах.
    if _config.get("MOP_ANSIBLE_PROFILE"):
        os.environ.setdefault("ANSIBLE_CALLBACKS_ENABLED",
                              "ansible.posix.profile_tasks")
    # Локаль прогонов: ansible требует UTF-8 (#92). LC_ALL, а не LANG:
    # он старше всех категорий разом и перебивает чужие LC_*, приехавшие с
    # машины оператора по ssh.
    os.environ["LC_ALL"] = run_locale(_config.get("MOP_LOCALE"))


def command(modname):
    """main командлета, импортируемый уже внутри run (#169): отказ импорта
    (нет nats-py -- ImportError из mop/bus.py) ловится там же, где остальные
    ожидаемые отказы, а не падает трассой до него."""
    return lambda argv: importlib.import_module(modname).main(argv)


def run(fn, argv):
    """Запустить команду, переведя ожидаемые отказы в понятную строку:
    трассировка в ответ на «нет связи с Nomad» — шум, за которым теряется
    единственное, что оператору нужно знать.

    Только stdlib и config: `mop setup` ставит зависимости, и падать до
    него на импорте шины нельзя. BusError — подкласс RuntimeError, ловится
    вместе с ним."""
    from .. import config
    try:
        sys.exit(fn(argv) or 0)
    except config.Missing as e:
        # На новой машине это первое, обо что спотыкаются, и отказ обязан
        # читаться как инструкция, а не как трассировка.
        sys.exit(str(e))
    except (ConnectionError, RuntimeError, LookupError, ImportError) as e:
        # Отказ сервиса (bus.Refused), неработающий папет (LookupError) и
        # недостающая библиотека (ImportError, #169) -- здесь, а не в каждом
        # командлете (#146): одна строка в stderr,
        # ненулевой выход. Печатаем сами, а не sys.exit(строка): так это
        # видно и проверке, а не только интерпретатору на выходе.
        print(e, file=sys.stderr, flush=True)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
