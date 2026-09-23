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

Переход: `deploy` пока на bash (bin/deploy, bin/common) и запускается отсюда
как исключение, до #76.
"""
import ast
import importlib
import os
import subprocess
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
# Переходное: команды, которые ещё живут в bin/ на bash (#76).
BASH = ("deploy",)


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
    """{группа: {глагол}} — модули внутри подпакетов-групп."""
    out = {}
    for section, _, is_pkg in scan(package):
        if is_pkg:
            path = os.path.join(package, section)
            out[section] = {m[:-3] for m in os.listdir(path)
                            if m.endswith(".py") and not m.startswith("_")}
    return out


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
    rest = list(argv[1:])
    if rest and rest[0] in group_verbs.get(name, ()):
        return f"{mod}.{rest[0]}", rest[1:]
    return mod, rest


def describe(path):
    """Первая строка докстринга модуля, без импорта; пусто, если нет."""
    try:
        with open(path) as f:
            doc = ast.get_docstring(ast.parse(f.read()))
    except (OSError, SyntaxError):
        return ""
    return (doc or "").strip().splitlines()[0].strip() if doc else ""


def _path_of(section, name, is_pkg):
    return os.path.join(PACKAGE, section, "__init__.py" if is_pkg else f"{name}.py")


def usage(found=None):
    """Список команд по секциям — ответ на `mop` без аргументов."""
    found = scan() if found is None else found
    lines = ["mop — pool of claude puppets on top of Nomad, message bus on top of NATS",
             "", "  mop <command> [arguments]"]
    groups = [(s, n, p) for s, n, p in found if p]
    for section in SECTIONS:
        rows = [(n, describe(_path_of(section, n, False)))
                for s, n, p in found if s == section]
        if section == "pool":
            rows += [(b, describe_bash(os.path.join(BIN, b))) for b in BASH]
            rows.sort()
        if section == "service" and groups:
            lines += ["", "  groups: mop <group> <verb>, mop <group> alone for the verbs"]
            lines += [f"    {s:<10} {describe(_path_of(s, '', True))}" for s, _, _ in groups]
        lines += ["", f"  {TITLES[section]}:"]
        lines += [f"    {n:<10} {d}" for n, d in rows]
    lines += ["", "  mop help     this list"]
    return "\n".join(lines)


def describe_bash(path):
    """Описание bash-командлета: первая строка комментария после shebang."""
    try:
        with open(path) as f:
            for line in f.readlines()[1:4]:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return ""


# ─── запуск ──────────────────────────────────────────────────────────────
def main(argv):
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(usage())
        return 0
    if argv[0] in BASH:
        # Шелловый командлет получает функции из common тем же source, что и
        # раньше; сам файл живёт в bin/ до переезда на python (#76).
        cmd = f'source "$0/common"; source "$0/{argv[0]}"'
        os.execvp("bash", ["bash", "-e", "-c", cmd, BIN, *argv[1:]])
    try:
        found = resolve(argv, catalog(scan()), verbs())
    except RuntimeError as e:
        sys.exit(f"mop: {e}")
    if found is None:
        print(f"mop: no such subcommand «{argv[0]}»", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 1
    modname, rest = found
    environment()
    return run(importlib.import_module(modname).main, rest)


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
    except (ConnectionError, RuntimeError, LookupError) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
