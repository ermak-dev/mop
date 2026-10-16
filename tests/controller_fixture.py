"""Изолированный Docker fixture не получает ignored inputs и не скрывает cleanup.

HYPOTHESIS: copytree исключает только .env, но передаёт .env.backup и
другие gitignored private files; тихий docker rm делает leak зелёным.
SOLUTION: source manifest из Git и обязательный наблюдаемый cleanup outcome.
RESULT: setup .gitignore исправлен отдельно; затем наблюдались два
behavioral FAIL: private file в snapshot и пропущенный cleanup error.
После исправления checks зелёные, новый fixture прошёл PG17 integration.
STATUS: FIXED — see #632 (fixture boundary, не весь эпик).
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import offline

from mop.cli.dev import controller
from mop.cli.dev import test as test_command


class FixtureSafety(unittest.TestCase):
    def source(self, directory):
        root = Path(directory)
        (root / '.mop').mkdir()
        (root / '.gitignore').write_text('.env*\noperator-private.txt\n/inventory.yaml\n/sandbox.yaml\n')
        (root / '.gitlab-ci.yml').write_text('''
.tests:
  variables:
    SETUP:
      value: exit 0
test:py3.12:
  variables:
    POSTGRES_IMAGE: postgres:16-bookworm
    PYTHON_IMAGE: python:3.12-slim-trixie
controller:pg17:
  variables:
    POSTGRES_IMAGE: postgres:17-bookworm
    PYTHON_IMAGE: python:3.13-slim-trixie
''')
        for name, text in {'public.py': 'public source', 'new_case.py': 'new untracked source',
                           '.mop/sandbox.yaml': 'project input', '.env.backup': 'synthetic private marker',
                           'operator-private.txt': 'synthetic private marker',
                           'inventory.yaml': 'installation-only', 'sandbox.yaml': 'installation-only'}.items():
            (root / name).write_text(text)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'add', '.gitignore', '.gitlab-ci.yml',
                        'public.py', '.mop/sandbox.yaml'], check=True)
        return root

    def invoke(self, root, *, cleanup_failure=False, check='controller'):
        original_run, original_which = subprocess.run, shutil.which
        snapshots, commands = [], []

        def run(argv, **kwargs):
            if argv[0] != '/fixture/docker':
                return original_run(argv, **kwargs)
            commands.append(argv)
            if argv[1:3] in (['container', 'ls'], ['network', 'ls']):
                name = argv[argv.index('--filter') + 1].split('=', 1)[1]
                return subprocess.CompletedProcess(argv, 0, stdout=name + '\n', stderr='')
            if 'bash' in argv:
                mount = argv[argv.index('-v') + 1]
                source = Path(mount.rsplit(':', 1)[0])
                snapshots.append({str(path.relative_to(source)) for path in source.rglob('*') if path.is_file()})
            code = 1 if cleanup_failure and argv[1:3] == ['rm', '-f'] else 0
            return subprocess.CompletedProcess(argv, code, stdout=b'', stderr=b'')

        with patch.object(test_command, 'root', return_value=str(root)), \
                patch.object(controller.shutil, 'which', side_effect=lambda name:
                             '/fixture/docker' if name == 'docker' else original_which(name)), \
                patch.object(controller.subprocess, 'run', side_effect=run):
            result = controller.main(['--postgres', '16', '--check', check])
        return result, snapshots, commands

    def test_snapshot_contains_tracked_and_new_source_but_no_ignored_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            result, snapshots, commands = self.invoke(root)
        self.assertEqual(result, 0)
        self.assertEqual(len(snapshots), 1)
        files = snapshots[0]
        self.assertIn('public.py', files)
        self.assertIn('new_case.py', files)
        self.assertIn('.mop/sandbox.yaml', files)
        for private in ('.env.backup', 'operator-private.txt', 'inventory.yaml', 'sandbox.yaml'):
            self.assertNotIn(private, files)
        self.assertFalse(any(path.startswith('.git/') for path in files))
        self.assertTrue(any(command[1:3] == ['rm', '-f'] for command in commands))

    def test_runtime_gate_can_be_selected_in_owned_postgres_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, commands = self.invoke(self.source(directory), check='controller_runtime')
        self.assertEqual(result, 0)
        runner = next(command for command in commands if 'bash' in command)
        self.assertIn('TESTS=python3 tests/controller_runtime.py', runner)

    def test_failed_cleanup_cannot_report_a_successful_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            with self.assertRaisesRegex(RuntimeError, 'cleanup|removed|incomplete'):
                self.invoke(root, cleanup_failure=True)


if __name__ == '__main__':
    with offline():
        unittest.main()
