"""mop dev test [name ...] [--strict] [-v]: run the checks in tests/

Every file of tests/ (a helper named _*.py is not a check), or only the
named ones: `mop dev test cli spec`. The checks of the working copy you run
it in; outside one, of the mop that runs it.

  --strict   as MOP_TESTS_STRICT=1 in CI: every skipped check or test fails
  -v         verbose pytest progress, not only on a terminal

Uses the declared test extra. Prints each failure before the next file and
pytest's summary; any test or collection failure exits nonzero.
"""
import os
import subprocess
import sys

from mop.common import config
from mop.cli import lib

TESTS = "tests"


def plan(names, listing):
    """Какие файлы гнать. Чистая функция (#271).

    names -- имена из командной строки (`cli`, `cli.py`, `tests/cli.py`),
    listing -- содержимое каталога. Без имён -- все проверки по алфавиту;
    `_*.py` -- помощники, не проверки. Неизвестное имя -- отказ, а не
    молчаливый пропуск: опечатка иначе читалась бы зелёным прогоном."""
    checks = sorted(f for f in listing if f.endswith(".py") and not f.startswith("_"))
    if not names:
        return checks
    out = []
    for n in names:
        f = os.path.basename(n)
        f = f if f.endswith(".py") else f + ".py"
        if f not in checks:
            raise ValueError(f"no check {TESTS}/{f}")
        if f not in out:
            out.append(f)
    return out


def summary(results):
    """[(файл, код, вывод, секунды)] -> (строки итога, всё ли зелёное).
    Чистая функция. Упавшие -- заголовком и выводом целиком: причина и есть
    то, ради чего прогон смотрят; зелёные -- только в счёте."""
    lines, red = [], []
    for f, code, output, _ in results:
        if code == 0:
            continue
        red.append(f)
        lines.append(f"FAILED {TESTS}/{f} (exit {code})")
        lines += output.rstrip("\n").splitlines()
    ok = len(results) - len(red)
    lines.append(f"{ok}/{len(results)} ok" + (f", failed: {' '.join(red)}" if red else ""))
    return lines, not red


def root():
    """Чью копию гнать: рабочую копию, где зовут, если это mop, иначе ту,
    из которой запущен mop. Иначе мастер в ветке-worktree гонял бы проверки
    общей копии."""
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    top = r.stdout.strip() if r.returncode == 0 else ""
    if top and os.path.isdir(os.path.join(top, TESTS)) and \
            os.path.isdir(os.path.join(top, "mop", "cli")):
        return top
    return config.PROJECT


def main(argv):
    strict = "--strict" in argv
    verbose = "-v" in argv
    names = [a for a in argv if a not in ("--strict", "-v")]
    if any(a.startswith("-") for a in names):
        lib.usage(__doc__)
    top = root()
    try:
        files = plan(names, os.listdir(os.path.join(top, TESTS)))
    except ValueError as e:
        lib.usage(f"{e}\n\n{__doc__}")
    # Плагин живёт рядом с checks, не в runtime-пакете клиента/контроллера.
    # Один session на весь план: discovery, verdict и exit code принадлежат pytest.
    previous = list(sys.path)
    try:
        sys.path.insert(0, os.path.join(top, TESTS))
        try:
            from _pytest_runtime import load_pytest, pytest_environment
            pytest = load_pytest()
        except ImportError as error:
            raise RuntimeError(f'mop dev test requires the declared test extra: {error}') from None
        options = ['-c', os.path.join(top, 'pyproject.toml'), '-p', '_pytest_plugin']
        if strict:
            options.append('--mop-strict')
        options.append('-v' if verbose else '-q')
        with pytest_environment():
            return int(pytest.main(options + [os.path.join(top, TESTS, f) for f in files]))
    finally:
        sys.path[:] = previous
