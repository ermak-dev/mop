"""Изолированный вход прежнего check; pytest владеет его процессом и TMPDIR."""
import os
import runpy
import sys
import unittest


if __name__ == '__main__' and sys.argv[1] != '--body':
    from _pytest_process import supervise
    owner_fd = int(sys.argv[2]) if sys.argv[1] == '--owner-fd' else None
    raise SystemExit(supervise([sys.executable, __file__, '--body', sys.argv[-1]], owner_fd))


if __name__ == '__main__':
    # Не импортируем hermetic раньше check: сам hermetic.py исполняется как
    # __main__ и обязан прочитать strict/fixture inputs до собственной изоляции.
    if (os.environ.get('MOP_TESTS_STRICT') or '').lower() in ('1', 'yes', 'true', 'on'):
        original = unittest.TestResult.addSkip

        def strict_skip(result, test, reason):
            original(result, test, reason)
            result.addFailure(test, (AssertionError,
                AssertionError(f'strict mode rejects skipped test {test.id()}: {reason}'), None))

        unittest.TestResult.addSkip = strict_skip
    script = sys.argv[2]
    # Тот же import root, что у python selected/tests/check.py: адаптер
    # может быть импортирован другой session из соседней working copy.
    sys.path[0] = os.path.dirname(os.path.abspath(script))
    sys.argv = [script]
    runpy.run_path(script, run_name='__main__')
