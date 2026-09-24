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

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TESTS = os.path.join(ROOT, "tests")
MACHINE = ("MOP_", "NOMAD_", "PU_")
HOME = tempfile.mkdtemp(prefix="mop-test-home-")


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
    from mop import config, context
    config.ENV_FILE = os.environ["MOP_ENV_FILE"]
    config.NODE_ENV_FILE = os.path.join(HOME, ".config", "mop", "node.env")
    config.forget()
    context.clone_binding = lambda: {}


_isolate()


# ─── запуском: изоляция есть и держит ────────────────────────────────────
def files():
    return sorted(os.path.join(TESTS, f) for f in os.listdir(TESTS)
                  if f.endswith(".py") and f != "hermetic.py")


def mop_imports(tree):
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "mop"
            or isinstance(n, ast.Import) and any(a.name.split(".")[0] == "mop" for a in n.names)]


def check_imports():
    """Каждый файл с mop импортирует hermetic на верхнем уровне и раньше."""
    out = []
    for f in files():
        tree = ast.parse(open(f).read())
        uses = mop_imports(tree)
        if not uses:
            continue
        guard = [n.lineno for n in tree.body if isinstance(n, ast.Import)
                 and any(a.name == "hermetic" for a in n.names)]
        if not guard or guard[0] > min(uses):
            out.append(f"{os.path.basename(f)}: import hermetic before the first mop import "
                       f"(line {min(uses)})")
    return out


def hostile_home():
    """Дом, где всё не так: node.env и ~/.gitconfig с чужими значениями."""
    from mop import config
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
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".env"))
    with open(os.path.join(root, ".env"), "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    return root


def check_children():
    """Изоляция доходит до дочернего процесса (#217): командлет, запущенный
    проверкой через bin/mop, видит тот же пустой .env, что и она."""
    out = []
    got = subprocess.run(
        [sys.executable, "-c", "from mop import config; print(config.ENV_FILE)"],
        env={**os.environ, "PYTHONPATH": ROOT}, capture_output=True, text=True, cwd=ROOT)
    from mop import config
    if got.stdout.strip() != config.ENV_FILE:
        out.append(f"a child reads .env {got.stdout.strip() or got.stderr.strip()!r}, "
                   f"the check reads {config.ENV_FILE!r}")
    if config.ENV_FILE.startswith(ROOT + os.sep):
        out.append(f"the check reads the repository's own .env: {config.ENV_FILE}")
    from mop import puppets
    if puppets.LOCAL_KEYS_FILE != config.ENV_FILE:
        out.append(f"LLM keys come from {puppets.LOCAL_KEYS_FILE}, not the .env the "
                   f"check sees ({config.ENV_FILE})")
    return out


def check_runs():
    """Все файлы зелёные с пустым HOME и с враждебным -- во втором случае из
    копии репозитория с враждебным .env (#217)."""
    out = []
    # PYTHONUSERBASE здесь уже задан импортом: без него смена HOME прятала
    # бы nats-py самого файла проверок, а не машину.
    base = {k: v for k, v in os.environ.items() if not k.startswith(MACHINE)}
    hostile, values = hostile_home()
    repo = hostile_repo(values)
    homes = {"clean": (tempfile.mkdtemp(prefix="mop-test-clean-"), {}, ROOT),
             "hostile": (hostile, values, repo)}
    runs = []
    for label, (home, env, root) in homes.items():
        for f in files():
            f = os.path.join(root, "tests", os.path.basename(f))
            runs.append((label, f, subprocess.Popen(
                [sys.executable, f], env={**base, **env, "HOME": home},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=root)))
    for label, f, p in runs:
        text = p.communicate()[0]
        if p.returncode != 0:
            tail = "\n    ".join(text.strip().splitlines()[-8:])
            out.append(f"{os.path.basename(f)} with a {label} HOME: exit {p.returncode}\n    {tail}")
    return out


def main():
    failed = []
    for check in (check_imports, check_children, check_runs):
        failed += [f"FAIL {check.__name__}: {line}" for line in check()]
    if failed:
        print("\n".join(failed))
    print("hermetic: FAILED" if failed else "hermetic: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
