#!/usr/bin/env python3
"""Общее проверок -- в одном месте (#269): python3 tests/tests_lib.py

HYPOTHESIS: 43 файла проверок говорят шестью диалектами одного и того же:
свои замыкания check, два стиля отчёта, пять копий «прогнать командлет в
процессе», no_network в tests/cli.py, который импортируют другие проверки
(проверка тянет проверку), свои заглушки и ручные save/restore.
SOLUTION: tests/_lib.py -- Checks, run_command, no_network, patched и
общие заглушки; файлы проверок стоят на нём и своих копий не держат.
STATUS: FIXED — see #269
"""
import ast
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402

TESTS = os.path.dirname(os.path.realpath(__file__))
HELPERS = ("Checks", "run_command", "no_network", "NetworkGuard", "patched",
           "patched_env", "restored", "offline", "Msg", "canned", "bash", "udp_socket")
# Свои копии общего: определять их в файле проверок нельзя.
OWN = {"no_network", "silent_run", "NetworkGuard", "check", "expect", "fail"}


def check_files():
    return sorted(f for f in os.listdir(TESTS) if f.endswith(".py") and not f.startswith("_"))


def dialects(tree):
    """Следы своего диалекта: -> [строка на след]."""
    out = []
    # Метод класса-заглушки -- не диалект: каталог-заглушка обязан зваться
    # check, раз так зовёт его mop (ldapauth: directory.check).
    methods = {id(m) for k in ast.walk(tree) if isinstance(k, ast.ClassDef)
               for m in k.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in OWN \
                and id(n) not in methods:
            out.append(f"line {n.lineno}: defines its own {n.name}")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name != "main":
            # Настоящее обращение к redirect_stdout, а не строка с этим словом.
            if any(isinstance(m, ast.Attribute) and m.attr == "redirect_stdout"
                   or isinstance(m, ast.Name) and m.id == "redirect_stdout"
                   for m in ast.walk(n)):
                out.append(f"line {n.lineno}: {n.name} runs a command by hand (redirect_stdout)")
        # Счёт провалов руками: failed += 1, bad += 1, failed.append(...).
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) \
                and n.target.id in ("failed", "bad") and isinstance(n.op, ast.Add):
            out.append(f"line {n.lineno}: counts failures by hand ({n.target.id} +=)")
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "append" and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == "failed":
            out.append(f"line {n.lineno}: counts failures by hand (failed.append)")
    return out


def cross_imports(tree, stems):
    """Импорт другой проверки: -> [имя]."""
    got = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in stems:
            got.append(n.module)
        if isinstance(n, ast.Import):
            got += [a.name for a in n.names if a.name in stems]
    return got


def uses_lib(tree):
    return any((isinstance(n, ast.ImportFrom) and n.module == "_lib")
               or (isinstance(n, ast.Import) and any(a.name == "_lib" for a in n.names))
               for n in ast.walk(tree))


# ── #338: проверки не оставляют временных каталогов ──────────────────────
# HYPOTHESIS: tempfile.mkdtemp в проверках (дом hermetic, копия репозитория с
# web/node_modules, каталоги самих проверок) никто не удаляет: прогон на теле
# папета оставлял 276 каталогов и 376 МБ, и tmpfs в 2 ГБ заполнялся за
# несколько прогонов; на mate их набралось 45936.
# SOLUTION: _lib заводит один временный корень на процесс проверки,
# направляет в него tempfile.tempdir и TMPDIR (туда же попадают дети) и
# удаляет его при выходе; hermetic импортирует _lib первым, до своего дома.
# Копия репозитория не несёт web/node_modules.
# STATUS: FIXED — see #338
PROBE = """
import os, subprocess, sys, tempfile
sys.path.insert(0, %r)
import hermetic  # noqa: F401 -- как любой файл проверок
AT_IMPORT = tempfile.mkdtemp(prefix="mop-test-probe-")
def check():
    inner = tempfile.mkdtemp(prefix="mop-test-probe-")
    child = subprocess.run([sys.executable, "-c",
        "import tempfile; print(tempfile.NamedTemporaryFile(prefix='mop-test-child-', delete=False).name)"],
        capture_output=True, text=True, check=True).stdout.strip()
    return inner, child
inner, child = check()
print("\\n".join([hermetic.HOME, AT_IMPORT, inner, child]))
"""


def check_tmp_cleanup_338(c):
    import subprocess
    import tempfile
    watch = os.path.realpath(tempfile.mkdtemp(prefix="mop-test-watch-"))
    src = tempfile.mkdtemp(prefix="mop-test-probe-src-")
    probe = os.path.join(src, "probe.py")
    with open(probe, "w") as f:
        f.write(PROBE % TESTS)
    r = subprocess.run([sys.executable, probe], env=dict(os.environ, TMPDIR=watch),
                       capture_output=True, text=True)
    made = [p for p in r.stdout.splitlines() if p.strip()]
    c.expect("#338 the probe ran: hermetic's home, two mkdtemps, a child's tempfile",
             (r.returncode, len(made)), (0, 4))
    c.check(f"#338 everything the probe made was under its TMPDIR: {made}",
            all(os.path.realpath(p).startswith(watch + os.sep) for p in made))
    c.expect("#338 nothing is left in TMPDIR after the check process exits",
             sorted(os.listdir(watch)), [])
    # Копия репозитория (hermetic.hostile_repo, #217) -- без web/node_modules:
    # с ним каждый прогон писал в /tmp 370 МБ.
    copy = hermetic.hostile_repo({})
    c.check("#338 the repository copy carries no web/node_modules",
            not os.path.exists(os.path.join(copy, "web", "node_modules")))


def main():
    c = Checks()
    lib_path = os.path.join(TESTS, "_lib.py")
    c.check("tests/_lib.py exists", os.path.exists(lib_path))
    if os.path.exists(lib_path):
        import _lib
        missing = [h for h in HELPERS if not hasattr(_lib, h)]
        c.check("tests/_lib.py holds the shared helpers", not missing, missing)
    listed = [os.path.basename(f) for f in hermetic.files()]
    c.check("hermetic skips helpers (_*.py), as mop dev test does",
            not [f for f in listed if f.startswith("_")], listed)
    stems = {f[:-3] for f in check_files()} - {"hermetic"}
    for f in check_files():
        tree = ast.parse(open(os.path.join(TESTS, f)).read())
        found = dialects(tree)
        c.check(f"{f}: no dialect of its own", not found, "; ".join(found[:5])
                + (f" (+{len(found) - 5} more)" if len(found) > 5 else ""))
        other = cross_imports(tree, stems)
        c.check(f"{f}: imports no other check", not other, other)
        c.check(f"{f}: stands on tests/_lib.py", uses_lib(tree))
    check_tmp_cleanup_338(c)
    return c.report("tests_lib")


if __name__ == "__main__":
    sys.exit(main())
