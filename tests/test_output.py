"""Диагностика отказа видна до следующего файла, не после всего CI suite."""
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import offline, run_command
from mop.cli.dev import test as runner


class Output(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushed = []

    def flush(self):
        self.flushed.append(self.getvalue())


class FailureOutput(unittest.TestCase):
    # HYPOTHESIS: summary после цикла скрывает traceback, пока работают
    # остальные файлы; per-file status не заменяет причину отказа.
    # RED: три отказа — причина отсутствует перед следующим subprocess.
    # SOLUTION: flush полного отказа после файла; в конце только общий verdict.
    # RESULT: 3/3 behavioral checks и 4/4 strict файлов green; нет fail-fast/дублей.
    # STATUS: FIXED — see #672
    #
    # #675: захват вывода — общий run_command(out=...): наблюдатель flush
    # (#672) получает записи прямо в свой поток, а не в скрытую копию.
    def test_failure_is_flushed_before_the_next_file_in_quiet_and_verbose_modes(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                output, calls = Output(), []
                reason = 'first diagnostic marker'

                def execute(argv, **kwargs):
                    calls.append(argv[-1])
                    if argv[-1] == 'tests/first.py':
                        return subprocess.CompletedProcess(argv, 1, reason + '\n')
                    self.assertIn(reason, output.getvalue(), 'failure is hidden until the whole suite finishes')
                    self.assertTrue(any(reason in value for value in output.flushed), 'diagnostic was not flushed')
                    return subprocess.CompletedProcess(argv, 0, 'private successful child output\n')

                with patch.object(runner, 'root', return_value='/fixture'), \
                        patch.object(runner.os, 'listdir', return_value=['first.py', 'second.py']), \
                        patch.object(runner.subprocess, 'run', side_effect=execute):
                    _, _, result = run_command(runner.main,
                        ['first', 'second', *(['-v'] if verbose else [])], out=output)
                self.assertEqual(result, 1)
                self.assertEqual(calls, ['tests/first.py', 'tests/second.py'])
                self.assertEqual(output.getvalue().count(reason), 1, 'failure diagnostic was printed twice')
                self.assertNotIn('private successful child output', output.getvalue())
                self.assertTrue(output.getvalue().endswith('1/2 ok, failed: first.py\n'))

    def test_successful_quiet_run_keeps_only_the_existing_summary(self):
        output = Output()
        with patch.object(runner, 'root', return_value='/fixture'), \
                patch.object(runner.os, 'listdir', return_value=['first.py', 'second.py']), \
                patch.object(runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'child body\n')):
            _, _, result = run_command(runner.main, ['first', 'second'], out=output)
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), '2/2 ok\n')

    def test_every_failure_remains_visible_and_all_files_are_run(self):
        output, calls = Output(), []
        def execute(argv, **kwargs):
            calls.append(argv[-1])
            index = len(calls)
            if index > 1:
                self.assertIn('diagnostic-' + str(index-1), output.getvalue())
            return subprocess.CompletedProcess(argv, 1, 'diagnostic-' + str(index) + '\n')
        with patch.object(runner, 'root', return_value='/fixture'), \
                patch.object(runner.os, 'listdir', return_value=['first.py', 'second.py', 'third.py']), \
                patch.object(runner.subprocess, 'run', side_effect=execute):
            _, _, result = run_command(runner.main, ['first', 'second', 'third'], out=output)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 3)
        for index in (1, 2, 3):
            self.assertEqual(output.getvalue().count('diagnostic-' + str(index)), 1)
        self.assertTrue(output.getvalue().endswith('0/3 ok, failed: first.py second.py third.py\n'))


if __name__ == '__main__':
    with offline():
        unittest.main()
