"""Диагностика отказа видна до следующего файла, не после всего CI suite."""
import io
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
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
    # SOLUTION (#692): настоящий pytest session с изолированными items;
    # наблюдатель видит полный отказ до запуска следующего Popen.
    # Старые три ID и свойства #672 сохранены; меняется только формат итога.
    # RESULT (#692): RED — warning из повторного import plugin портил quiet;
    # GREEN — тот же точный итог, flush до следующего файла и все три ID.
    # Hook-модулям без assert явно не нужен rewrite, warnings не фильтруются.
    # STATUS: FIXED — see #672, #692
    def invoke(self, names, execute, output, verbose=False):
        import _pytest_plugin
        class Process:
            def __init__(self, result):
                self.returncode, self.output = result.returncode, result.stdout
                self.handle = SimpleNamespace(row={'pid': 1, 'start': 'synthetic',
                    'group': 1, 'namespace': 'synthetic'})

            def communicate(self):
                return self.output, None

            def close(self):
                pass

        def launch(script, **kwargs):
            return Process(execute(['tests/' + Path(script).name], **kwargs))

        with tempfile.TemporaryDirectory(prefix='mop-output-') as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\npython_files = ["*.py"]\n')
            for name in names:
                (root / 'tests' / (name + '.py')).write_text('# isolated fixture\n')
            with patch.object(runner, 'root', return_value=str(root)), \
                    patch.object(_pytest_plugin, 'OwnedProcess', side_effect=launch):
                _, _, result = run_command(runner.main,
                    [*names, *(['-v'] if verbose else [])], out=output)
        return result

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

                result = self.invoke(['first', 'second'], execute, output, verbose)
                self.assertEqual(result, 1)
                self.assertEqual(calls, ['tests/first.py', 'tests/second.py'])
                self.assertEqual(output.getvalue().count(reason), 1, 'failure diagnostic was printed twice')
                self.assertNotIn('private successful child output', output.getvalue())
                self.assertIn('1 failed, 1 passed', output.getvalue())

    def test_successful_quiet_run_keeps_only_the_existing_summary(self):
        output = Output()
        result = self.invoke(['first', 'second'], lambda argv, **kwargs:
            subprocess.CompletedProcess(argv, 0, 'child body\n'), output)
        self.assertEqual(result, 0)
        self.assertRegex(output.getvalue(), r'\A\.\.\s+\[100%\]\n2 passed in [0-9.]+s\n\Z')
        self.assertNotIn('child body', output.getvalue())
        self.assertNotIn('FAILED', output.getvalue())

    def test_every_failure_remains_visible_and_all_files_are_run(self):
        output, calls = Output(), []

        def execute(argv, **kwargs):
            calls.append(argv[-1])
            index = len(calls)
            if index > 1:
                self.assertIn('diagnostic-' + str(index - 1), output.getvalue())
            return subprocess.CompletedProcess(argv, 1, 'diagnostic-' + str(index) + '\n')

        result = self.invoke(['first', 'second', 'third'], execute, output)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 3)
        for index in (1, 2, 3):
            self.assertEqual(output.getvalue().count('diagnostic-' + str(index)), 1)
        self.assertIn('3 failed', output.getvalue())


if __name__ == '__main__':
    with offline():
        unittest.main()
