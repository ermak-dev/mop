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


# HYPOTHESIS (#692): ранний import hermetic съедал strict/fixture inputs,
# runpy оставлял import root другого клона, refusal прятал ImportError,
# а ambient PYTEST_ADDOPTS/PYTEST_PLUGINS мог подменить выбранный plan.
# SOLUTION: bridge читает strict до изоляции и сохраняет script semantics;
# commandlet возвращает исходную причину отсутствующей зависимости;
# общий context изолирует pytest inputs и восстанавливает их после session.
# RESULT: RED — inputs=None, неверный root, потерянный reason и ambient
# pytest inputs внутри session; GREEN — те же probes, восстановление env,
# strict unittest skip и общий tests_lib на Python 3.12.
# STATUS: FIXED — see #692

def check_pytest_bootstrap_692(c):
    import json
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        probe = os.path.join(directory, 'check.py')
        with open(probe, 'w') as stream:
            stream.write('import json, os, sys\n'
                         'values = {key: os.environ.get(key) for key in '
                         '("MOP_TESTS_STRICT", "MOP_TEST_POSTGRES_HOST")}\n'
                         'values["script_directory"] = sys.path[0]\nprint(json.dumps(values))\n')
        env = hermetic.child_env({'MOP_TESTS_STRICT': '1',
                                 'MOP_TEST_POSTGRES_HOST': 'isolated-test-coordinate'})
        result = subprocess.run([sys.executable, os.path.join(TESTS, '_pytest_legacy.py'), probe],
                                env=env, capture_output=True, text=True)
    c.expect('#692 bridge runs the same script entrypoint', result.returncode, 0)
    observed = json.loads(result.stdout)
    c.expect('#692 bridge uses the selected check directory, not the helper clone',
             observed.pop('script_directory'), directory)
    c.expect('#692 bridge preserves strict and fixture inputs until the check isolates itself',
             observed, {'MOP_TESTS_STRICT': '1',
                                        'MOP_TEST_POSTGRES_HOST': 'isolated-test-coordinate'})


def check_pytest_strict_skips_692(c):
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        probe = os.path.join(directory, 'skip_probe.py')
        with open(probe, 'w') as stream:
            stream.write('import unittest\nclass Probe(unittest.TestCase):\n'
                         '    @unittest.skip("owned skip reason")\n'
                         '    def test_boundary(self): pass\n'
                         'if __name__ == "__main__": unittest.main()\n')
        for strict in (False, True):
            result = subprocess.run([sys.executable, os.path.join(TESTS, '_pytest_legacy.py'), probe],
                                    env=hermetic.child_env({}, strict=strict),
                                    capture_output=True, text=True)
            c.expect(f'#692 unittest skip fails exactly in strict={strict}',
                     result.returncode, int(strict))
            if strict:
                c.check('#692 strict failure preserves the skipped test identity and reason',
                        'Probe.test_boundary' in result.stderr and
                        'owned skip reason' in result.stderr and
                        'strict mode rejects skipped test' in result.stderr, result.stderr)


def check_pytest_import_reason_692(c):
    from unittest.mock import patch
    from mop.cli.dev import test as command
    import _pytest_runtime
    reason = 'owned missing pytest dependency reason'
    with patch.object(command, 'root', return_value=hermetic.ROOT), \
            patch.object(_pytest_runtime, 'load_pytest', side_effect=ModuleNotFoundError(reason)):
        try:
            command.main(['tests_lib'])
        except RuntimeError as error:
            observed = str(error)
        else:
            observed = ''
    c.check('#692 missing test extra preserves the actual dependency import reason',
            reason in observed, observed)


def check_pytest_environment_692(c):
    from types import SimpleNamespace
    from unittest.mock import patch
    from mop.cli.dev import test as command
    import _pytest_runtime
    ambient = {'PYTEST_ADDOPTS': '-k only_one_check', 'PYTEST_PLUGINS': 'ambient_fixture_plugin',
               'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '0'}
    observed = []
    pytest = SimpleNamespace(main=lambda args:
        observed.append({key: os.environ.get(key) for key in ambient}) or 0)
    with patch.dict(os.environ, ambient), \
            patch.object(command, 'root', return_value=hermetic.ROOT), \
            patch.object(_pytest_runtime, 'load_pytest', return_value=pytest):
        result = command.main(['tests_lib'])
        restored = {key: os.environ.get(key) for key in ambient}
    c.expect('#692 ambient pytest options and plugins cannot shrink or replace the selected plan',
             (result, observed), (0, [{'PYTEST_ADDOPTS': None, 'PYTEST_PLUGINS': None,
                                     'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}]))
    c.expect('#692 pytest environment belongs to the invocation and is restored afterwards',
             restored, ambient)


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
    check_pytest_bootstrap_692(c)
    check_pytest_strict_skips_692(c)
    check_pytest_import_reason_692(c)
    check_pytest_environment_692(c)
    return c.report("tests_lib")


if __name__ == "__main__":
    sys.exit(main())
