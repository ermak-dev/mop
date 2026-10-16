"""Явные web gates внутри owned Python 3.12 fixture с объявленным Node toolchain.

#678: все три frontend-гейта CI (build --check, types, полный Vitest) --
в процессе, общим _lib.run_command, а не subprocess другого командлета
(#160). Без fail-fast: каждый отказ виден с его выводом, любой красит
fixture, но остальные гейты всё равно выполняются.
"""
import sys

import _lib  # noqa: F401 -- общий lifetime временных файлов
import hermetic  # noqa: F401 -- до mop и чтения настроек
from mop.cli.dev.web import build as web_build
from mop.cli.dev.web import test as web_test


def main():
    failed = []
    for label, module, argv in (('build', web_build, ['--check']),
                                ('types', web_test, ['--types']),
                                ('tests', web_test, [])):
        out, err, code = _lib.run_command(module.main, argv)
        if code:
            sys.stderr.write(out + err)
            failed.append(f'{label} (exit {code})')
    if failed:
        sys.stderr.write('web gates failed: ' + ', '.join(failed) + '\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
