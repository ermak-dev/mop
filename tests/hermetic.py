#!/usr/bin/env python3
"""Проверки без машины (#209): python3 tests/hermetic.py

Импортом -- изоляция настроек от машины, на которой идут проверки. Файл
проверок импортирует этот модуль до первого импорта mop:

    import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)

config.get читает окружение, ~/.config/mop/node.env и .env клона. Проверки
видели настройки той машины, где их запустили: на узле wate-wsl, где
node.env с #201 несёт MOP_SSH_PORT=2222, проверки #201 падали на чистом
master, а на hyper и у оператора были зелёными -- #201 так и сел, не
замеченным. Любая проверка, читающая настройку, могла перевернуться от
машины.

Что делает импорт, до того как mop прочтёт хоть что-то:
  * HOME -- пустой временный каталог: node.env, ~/.config/mop, ~/.ssh,
    ~/.gitconfig машины не видны, и пути, которые mop считает от дома при
    импорте, тоже временные. Пакеты пользователя (nats-py в user site)
    остаются: sys.path собран до смены HOME, а дочерним процессам user site
    называет PYTHONUSERBASE;
  * из окружения убраны MOP_*, NOMAD_*, PU_* -- настройки и креды машины;
  * config.ENV_FILE -- несуществующий файл во временном доме: .env клона
    пуст для проверки. Задан и переменной MOP_ENV_FILE (#217), чтобы тот же
    пустой .env видели дочерние процессы -- командлеты через bin/mop.
    Проверки, которым нужен свой .env или node.env, подсовывают его сами,
    как и раньше (tests/config.py);
  * привязка клона к серверу (git config mop.server, #131) -- пустая: у
    клона оператора она своя.
Сеть не трогается: где проверка гоняет командлет, предохранитель --
no_network() из tests/cli.py.

Запуском -- проверка, что изоляция есть и держит:
  * каждый файл tests/, импортирующий mop, импортирует hermetic раньше;
  * все остальные файлы tests/ зелёные дважды: с пустым HOME и с
    враждебным -- node.env с MOP_SSH_PORT=2222 и чужими значениями всех
    настроек, ~/.gitconfig с mop.server, и те же настройки в окружении;
    враждебный прогон идёт из копии репозитория с теми же значениями в .env
    корня (#217, настоящий .env не трогается);
  * дочерний процесс видит тот же .env, что проверка (#217).

HYPOTHESIS: config.get читает настоящие node.env и .env машины, и у
проверок нет общего способа от них отгородиться.
SOLUTION: этот модуль -- импортом до mop в каждом файле tests/.
STATUS: FIXED — see #209
"""
import ast
import os
import site
import subprocess
import sys
import tempfile

import _lib  # noqa: F401,E402 -- первым: временный корень процесса раньше HOME (#338)

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TESTS = os.path.join(ROOT, "tests")
MACHINE = ("MOP_", "NOMAD_", "PU_")
HOME = tempfile.mkdtemp(prefix="mop-test-home-")


# Строгий режим (#229): пропуск проверки -- провал файла. Читается до
# изоляции -- она убирает MOP_* из окружения. Обычный режим -- как было:
# строка SKIPPED, файл идёт дальше; строгий ставит сборка CI (#230), где
# стоят все необязательные библиотеки и пропуску неоткуда взяться.
STRICT = (os.environ.get("MOP_TESTS_STRICT") or "").lower() in ("1", "yes", "true", "on")


def skip(what, why):
    """Проверку `what` не прогнать: `why` (нет библиотеки, нет утилиты).
    Единственное место, где печатается SKIPPED: пропуск, читавшийся зелёным,
    отправил #220 в master красным. Строгий режим -- выход с ненулевым кодом
    и именем пропуска, файл дальше не идёт."""
    if STRICT:
        raise SystemExit(f"FAILED  {what}: skipped ({why}) -- a skip is a failure "
                         f"under MOP_TESTS_STRICT=1")
    print(f"SKIPPED {what}: {why}", flush=True)


def child_env(extra, strict=None):
    """Окружение дочернего прогона проверок: без настроек машины (MOP_* и
    прочие), с extra и со строгим режимом этого прогона -- иначе изоляция,
    убравшая MOP_*, сделала бы детей мягкими."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(MACHINE)}
    env.update(extra)
    if STRICT if strict is None else strict:
        env["MOP_TESTS_STRICT"] = "1"
    return env


def _isolate():
    # Дочерние процессы проверок (командлеты, python -c) иначе искали бы
    # пакеты пользователя во временном доме и не находили nats-py.
    os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())
    os.environ["HOME"] = HOME
    for k in [k for k in os.environ if k.startswith(MACHINE)]:
        del os.environ[k]
    # .env -- переменной окружения, а не только атрибутом (#217): её
    # наследуют дочерние процессы проверки (bin/mop -> командлет).
    os.environ["MOP_ENV_FILE"] = os.path.join(HOME, "no-such.env")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from mop.common import config, context
    config.ENV_FILE = os.environ["MOP_ENV_FILE"]
    config.NODE_ENV_FILE = os.path.join(HOME, ".config", "mop", "node.env")
    config.forget()
    context.clone_binding = lambda: {}


_isolate()


# ─── запуском: изоляция есть и держит ────────────────────────────────────
def files():
    """Файлы проверок: как у `mop dev test`, помощники (_*.py) -- не проверки
    (#269), и сам hermetic -- тоже."""
    return sorted(os.path.join(TESTS, f) for f in os.listdir(TESTS)
                  if f.endswith(".py") and not f.startswith("_") and f != "hermetic.py")


def mop_imports(tree):
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "mop"
            or isinstance(n, ast.Import) and any(a.name.split(".")[0] == "mop" for a in n.names)]


def check_imports(c):
    """Каждый файл с mop импортирует hermetic на верхнем уровне и раньше."""
    for f in files():
        tree = ast.parse(open(f).read())
        uses = mop_imports(tree)
        if not uses:
            continue
        guard = [n.lineno for n in tree.body if isinstance(n, ast.Import)
                 and any(a.name == "hermetic" for a in n.names)]
        c.check(f"check_imports: {os.path.basename(f)}: import hermetic before the first "
                f"mop import (line {min(uses)})", guard and guard[0] <= min(uses))


def hostile_home():
    """Дом, где всё не так: node.env и ~/.gitconfig с чужими значениями."""
    from mop.common import config
    home = tempfile.mkdtemp(prefix="mop-test-hostile-")
    os.makedirs(os.path.join(home, ".config", "mop"))
    values = {k: "hostile-209" for k in config.SETTINGS}
    values["MOP_SSH_PORT"] = "2222"
    with open(os.path.join(home, ".config", "mop", "node.env"), "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    with open(os.path.join(home, ".gitconfig"), "w") as f:
        f.write("[mop]\n\tserver = 192.0.2.99\n\tuser = hostile\n")
    return home, values


def hostile_repo(values):
    """Копия репозитория с враждебным .env в корне (#217): на контроллере
    .env задаёт MOP_HOME, и командлет в дочернем процессе проверки читал его.
    Копия, а не настоящий корень: настоящий .env не трогается никогда."""
    import shutil
    root = tempfile.mkdtemp(prefix="mop-test-repo-")
    shutil.copytree(ROOT, root, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".env",
                                                  # 370 МБ пакетов страницы (#338):
                                                  # проверкам ни к чему
                                                  "node_modules"))
    with open(os.path.join(root, ".env"), "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    return root


def check_children(c):
    """Изоляция доходит до дочернего процесса (#217): командлет, запущенный
    проверкой через bin/mop, видит тот же пустой .env, что и она."""
    got = subprocess.run(
        [sys.executable, "-c", "from mop.common import config; print(config.ENV_FILE)"],
        env={**os.environ, "PYTHONPATH": ROOT}, capture_output=True, text=True, cwd=ROOT)
    from mop.common import config
    c.check(f"check_children: a child reads .env {got.stdout.strip() or got.stderr.strip()!r}, "
            f"the check reads {config.ENV_FILE!r}", got.stdout.strip() == config.ENV_FILE)
    c.check(f"check_children: the check reads the repository's own .env: {config.ENV_FILE}",
            not config.ENV_FILE.startswith(ROOT + os.sep))
    from mop.common import puppets
    c.check(f"check_children: LLM keys come from {puppets.LOCAL_KEYS_FILE}, not the .env "
            f"the check sees ({config.ENV_FILE})", puppets.LOCAL_KEYS_FILE == config.ENV_FILE)


def check_skip_strict(c):
    """Пропуск проверки (#229): обычно -- строка SKIPPED и файл идёт дальше,
    с MOP_TESTS_STRICT=1 -- файл падает с ненулевым кодом и именем пропуска.
    Пропуск, читавшийся зелёным, однажды уже отправил #220 в master красным."""
    code = ("import sys; sys.path.insert(0, %r); import hermetic; "
            "hermetic.skip('the widget check', 'no widget library'); print('after the skip')"
            % TESTS)
    base = {k: v for k, v in os.environ.items() if not k.startswith(MACHINE)}
    for strict in ("", "1"):
        env = dict(base, **({"MOP_TESTS_STRICT": strict} if strict else {}))
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                           text=True, cwd=ROOT)
        lines = (r.stdout + r.stderr).splitlines()
        went_on = "after the skip" in r.stdout.splitlines()
        if not strict:
            c.check(f"check_skip_strict: normal mode: a skip is a SKIPPED line and the file "
                    f"goes on: exit {r.returncode}, {lines}",
                    r.returncode == 0 and "SKIPPED the widget check: no widget library" in lines
                    and went_on)
        else:
            c.check(f"check_skip_strict: MOP_TESTS_STRICT=1: a skip must fail the file, "
                    f"naming it: exit {r.returncode}, {lines}",
                    not (r.returncode == 0 or went_on or not any(
                        "the widget check" in l and "MOP_TESTS_STRICT" in l for l in lines)))


def check_own_skips(c):
    """Ни один файл не печатает свой SKIPPED: все пропуски -- через skip()."""
    for f in files():
        own = [n for n, line in enumerate(open(f), 1) if "SKIPPED" in line]
        c.check(f"check_own_skips: {os.path.basename(f)}:{own}: its own skip -- "
                f"use hermetic.skip()", not own)


def check_strict_children(c):
    """Строгий режим доходит до дочерних прогонов (#229): изоляция убирает
    MOP_* из окружения, и без явной передачи дети проверяли бы мягко."""
    child_env_ = globals().get("child_env")
    if not c.check("check_strict_children: hermetic has no child_env(): the children's "
                   "environment is built ad hoc", child_env_ is not None):
        return
    env = child_env_({}, strict=True)
    c.check(f"check_strict_children: children of a strict run must be strict: "
            f"{env.get('MOP_TESTS_STRICT')!r}", env.get("MOP_TESTS_STRICT") == "1")
    c.check("check_strict_children: children of a normal run must not be strict",
            "MOP_TESTS_STRICT" not in child_env_({}, strict=False))


def check_runs(c):
    """Все файлы зелёные с пустым HOME и с враждебным -- во втором случае из
    копии репозитория с враждебным .env (#217)."""
    # PYTHONUSERBASE здесь уже задан импортом: без него смена HOME прятала
    # бы nats-py самого файла проверок, а не машину.
    hostile, values = hostile_home()
    repo = hostile_repo(values)
    homes = {"clean": (tempfile.mkdtemp(prefix="mop-test-clean-"), {}, ROOT),
             "hostile": (hostile, values, repo)}
    runs = []
    for label, (home, env, root) in homes.items():
        for f in files():
            f = os.path.join(root, "tests", os.path.basename(f))
            runs.append((label, f, subprocess.Popen(
                [sys.executable, f], env=child_env({**env, "HOME": home}),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=root)))
    for label, f, p in runs:
        text = p.communicate()[0]
        tail = "\n    ".join(text.strip().splitlines()[-8:])
        c.check(f"check_runs: {os.path.basename(f)} with a {label} HOME: exit {p.returncode}"
                f"\n    {tail}", p.returncode == 0)


def main():
    # Лениво: при импорте hermetic -- изоляция, и больше ничего (#269).
    from _lib import Checks
    c = Checks()
    for check in (check_imports, check_children, check_skip_strict, check_own_skips,
                  check_strict_children, check_runs):
        check(c)
    return c.report("hermetic")


if __name__ == "__main__":
    sys.exit(main())
