"""Рабочий cwd не подменяет пакет выбранного лаунчера (#660).

HYPOTHESIS: python -m ставит cwd перед явно заданным PYTHONPATH.
SOLUTION: safe path оставляет cwd для Git/workspace, не для импорта mop.
STATUS: FIXED — see #660.
"""
import os
import subprocess
import tempfile
from pathlib import Path

import hermetic
from _lib import Checks
from mop.cli import lib


def main():
    c = Checks()
    root = Path(__file__).resolve().parent.parent
    launcher = root / 'bin/mop'
    with tempfile.TemporaryDirectory(prefix='mop-launcher-path-660-') as tmp:
        cwd = Path(tmp, 'other-working-copy')
        package = cwd / 'mop'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('raise SystemExit("fixture-cwd-package-selected")\n')
        env = dict(os.environ)
        env.pop('PYTHONSAFEPATH', None)
        for key in ('PYTHONPATH', 'MOP_SOUL_DIRS', 'MOP_SERVER_ENV_FILE'):
            env.pop(key, None)
        r = subprocess.run([str(launcher), 'help'], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=30)
        c.check('#660 launcher executes its own package from a shadowing cwd',
                r.returncode == 0 and 'fixture-cwd-package-selected' not in r.stdout + r.stderr
                and 'manage the pool' in r.stdout, (r.returncode, r.stdout, r.stderr))
        env['PYTHONPATH'] = str(root)
        r = subprocess.run([*lib.self_argv(bin_dir=str(Path(tmp, 'no-launcher'))), 'help'],
                           cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
        c.check('#660 installed-package subprocess also rejects cwd shadowing',
                r.returncode == 0 and 'fixture-cwd-package-selected' not in r.stdout + r.stderr
                and 'manage the pool' in r.stdout, (r.returncode, r.stdout, r.stderr))
        bindir = Path(tmp, 'bin')
        bindir.mkdir()
        python = bindir / 'python3'
        python.write_text('#!/bin/sh\nprintf "%s\\0" "$PWD" "$@"\n')
        python.chmod(0o700)
        env['PATH'] = str(bindir) + os.pathsep + env['PATH']
        r = subprocess.run([str(launcher), 'help', 'literal with spaces'], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=30)
        fields = r.stdout.rstrip('\x00').split('\x00')
        c.check('#660 launcher keeps cwd and literal arguments',
                r.returncode == 0 and fields[0] == str(cwd)
                and fields[-2:] == ['help', 'literal with spaces'], fields)
    return c.report('launcherpath')


if __name__ == '__main__':
    raise SystemExit(main())
