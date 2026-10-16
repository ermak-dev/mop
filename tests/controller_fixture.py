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
    POSTGRES_IMAGE: postgres:17-bookworm
    POSTGRES_MAJOR: "17"
    PYTHON_IMAGE: python:3.12-slim-trixie
    NODE_IMAGE: node:22-bookworm
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

    def invoke(self, root, *, cleanup_failure=False, arguments=None, labels=(), check='controller'):
        original_run, original_which = subprocess.run, shutil.which
        snapshots, commands = [], []

        def run(argv, **kwargs):
            if argv[0] != '/fixture/docker':
                return original_run(argv, **kwargs)
            commands.append(argv)
            if argv[1:3] in (['container', 'ls'], ['network', 'ls']):
                name = argv[argv.index('--filter') + 1].split('=', 1)[1]
                return subprocess.CompletedProcess(argv, 0, stdout=name + '\n', stderr='')
            if any(word.startswith('TESTS=') for word in argv):
                mount = argv[argv.index('-v') + 1]
                source = Path(mount.rsplit(':', 1)[0])
                snapshots.append({str(path.relative_to(source)) for path in source.rglob('*') if path.is_file()})
            code = 1 if cleanup_failure and argv[1:3] == ['rm', '-f'] else 0
            return subprocess.CompletedProcess(argv, code, stdout=b'', stderr=b'')

        with patch.object(test_command, 'root', return_value=str(root)), \
                patch.object(controller.shutil, 'which', side_effect=lambda name:
                             '/fixture/docker' if name == 'docker' else original_which(name)), \
                patch.object(controller.subprocess, 'run', side_effect=run):
            result = controller.main(arguments or ['--postgres', '17', '--check', check,
                *[word for label in labels for word in ('--label', label)]])
        return result, snapshots, commands

    # RED: PG17 выбирал Python 3.13, а отдельный PG16 fixture оставался доступен.
    # SOLUTION: один объявленный CI job и та же 3.12/17 пара в локальной fixture.
    # RESULT: 14 fixture checks и 6 strict файлов green; actual --check client
    # --web: 4 TLS + 5 native PG/HTTPS green, из трёх frontend-гейтов build
    # green, types и полный Vitest выполнены до конца и честно красны (legacy
    # TS errors и journal-формат дат) -- fixture отказал кодом 1, все гейты
    # отчитались. STATUS: FIXED (контракт fixture) — see #678
    def test_fixture_uses_the_single_python312_postgres17_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            result, _, commands = self.invoke(root, arguments=['--postgres', '17'])
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        database = next(command for command in commands if 'POSTGRES_DB=mop_ci' in command)
        self.assertIn('python:3.12-slim-trixie', runner)
        self.assertIn('POSTGRES_MAJOR=17', runner)
        self.assertIn('postgres:17-bookworm', database)
        self.assertNotIn('python:3.13-slim-trixie', runner)

    def test_web_gate_uses_the_same_python_container_and_cleans_up_its_toolchain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            result, _, commands = self.invoke(root, arguments=['--postgres', '17', '--check', 'client', '--web'])
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        self.assertIn('python:3.12-slim-trixie', runner)
        self.assertTrue(any(word.endswith(':/opt/mop-node:ro') for word in runner))
        script = next(word.removeprefix('TESTS=') for word in runner if word.startswith('TESTS='))
        self.assertIn('tests/_web_dist_fixture.py', script)
        self.assertIn('_control_client_cases', script)
        created = next(command[command.index('--name')+1] for command in commands
                       if command[1] == 'create' and 'node:22-bookworm' in command)
        self.assertTrue(any(command[1:3] == ['rm', '-f'] and created in command for command in commands))

    # #678: web gate -- не один build --check, а все три frontend-гейта
    # (build, types, полный Vitest) в процессе, через общий _lib.run_command,
    # а не subprocess другого командлета (#160). Каждый отказ виден, любой
    # из них красит fixture, без fail-fast.
    def test_web_fixture_invokes_the_actual_build_check_not_the_group_help(self):
        import runpy
        from unittest.mock import call
        with patch('mop.cli.dev.web.build.main', return_value=0) as build, \
                patch('mop.cli.dev.web.test.main', return_value=0) as web_test:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(Path(__file__).with_name('_web_dist_fixture.py')), run_name='__main__')
        build.assert_called_once_with(['--check'])
        self.assertEqual(web_test.call_args_list, [call(['--types']), call([])])
        self.assertEqual(caught.exception.code, 0)

    def test_web_fixture_keeps_running_after_a_failed_gate_and_exits_nonzero(self):
        import runpy
        with patch('mop.cli.dev.web.build.main', return_value=1), \
                patch('mop.cli.dev.web.test.main', return_value=0) as web_test:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(Path(__file__).with_name('_web_dist_fixture.py')), run_name='__main__')
        self.assertEqual(web_test.call_args_list[0].args, (['--types'],))
        self.assertEqual(len(web_test.call_args_list), 2)
        self.assertEqual(caught.exception.code, 1)

    def test_web_gate_cannot_expand_the_root_only_uid_fixture(self):
        with self.assertRaises(SystemExit) as caught:
            controller.main(['--check', 'credential_service', '--cross-uid', '--web'])
        self.assertEqual(caught.exception.code, 2)

    def test_other_postgres_matrix_is_rejected_before_any_fixture_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            with self.assertRaises(SystemExit) as caught:
                self.invoke(root, arguments=['--postgres', '16'])
        self.assertEqual(caught.exception.code, 2)

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

    # HYPOTHESIS: обычный ci UID не способен проверить SO_PEERCRED двух UID.
    # SOLUTION: только явный credential_service fixture допускает root в owned container.
    # RESULT: отсутствующий --cross-uid дал red; policy checks и actual PG16 UID probe green.
    # STATUS: FIXED — see #669.
    def test_cross_uid_is_explicit_owned_container_mode_not_default_or_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            setup_path = root / '.gitlab-ci.yml'
            setup_path.write_text(setup_path.read_text().replace('value: exit 0',
                "value: |\n        prepare-declared-fixture\n        exec su ci -s /bin/bash -c \"$TESTS\""))
            result, _, commands = self.invoke(root, arguments=['--postgres', '17',
                '--check', 'credential_service', '--cross-uid'])
            self.assertEqual(result, 0)
            worker = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
            self.assertIn('prepare-declared-fixture', worker[-1])
            self.assertIn('exec bash -c "$TESTS"', worker[-1])
            self.assertNotIn('exec su ci', worker[-1])
            _, _, commands = self.invoke(root)
            worker = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
            self.assertIn('exec su ci', worker[-1])

    def test_cross_uid_cannot_run_arbitrary_suite_as_root(self):
        with self.assertRaises(SystemExit) as caught:
            controller.main(['--check', 'hermetic', '--cross-uid'])
        self.assertEqual(caught.exception.code, 2)

    def test_narrow_labels_are_passed_as_arguments_not_shell_source(self):
        import shlex
        labels = ('_access_cases.AccessContracts.test_currentness', 'not-a-label; exit 0')
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            result, _, commands = self.invoke(root, labels=labels)
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        script = next(word.removeprefix('TESTS=') for word in runner if word.startswith('TESTS='))
        self.assertEqual(shlex.split(script), ['python3', 'tests/controller.py', *labels])
    def test_runtime_gate_can_be_selected_in_owned_postgres_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, commands = self.invoke(self.source(directory), check='controller_runtime')
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        self.assertIn('TESTS=python3 tests/controller_runtime.py', runner)

    def test_drop_diagnostic_reads_only_owned_database_and_known_pid_metadata(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import sys
        from _controller_fixture import _drop_backend_state
        observer = MagicMock()
        observer.__enter__.return_value = observer
        observer.execute.return_value.fetchall.return_value = [(123, 'postgres', 'active', 'IPC', 'CheckpointDone')]
        connect = MagicMock(return_value=observer)
        config = dict(NAME='test_mop_' + 'a' * 32, HOST='fixture-only', PORT='5432',
                      USER='fixture-only', PASSWORD='synthetic-do-not-log')
        with patch.dict(sys.modules, {'psycopg': SimpleNamespace(connect=connect)}):
            rows = _drop_backend_state(config, 123)
        self.assertEqual(rows, [(123, 'postgres', 'active', 'IPC', 'CheckpointDone')])
        query, params = observer.execute.call_args.args
        self.assertEqual(params, (123, config['NAME']))
        self.assertEqual(query, 'SELECT pid, datname, state, wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s OR datname = %s')
        self.assertNotIn('synthetic-do-not-log', repr(rows))
        self.assertIn('default_transaction_read_only=on', connect.call_args.kwargs['options'])
        self.assertIn('statement_timeout=1000', connect.call_args.kwargs['options'])
        self.assertEqual(connect.call_args.kwargs['connect_timeout'], 5)

    def test_drop_diagnostic_refuses_nonfixture_database_or_unknown_pid_before_connect(self):
        # HYPOTHESIS: диагностический helper не должен расширять SQL-scope
        # при неверном NAME/PID. Это защита probe, не исправление причины #668.
        # SOLUTION: fixture UUID-name и положительный int PID проверяются до connect.
        # RESULT: шесть meaningful red (SQL выполнялся), затем guard green.
        # STATUS: защита probe проверена; причина #668 ещё не подтверждена.
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import sys
        from _controller_fixture import _drop_backend_state
        connect = MagicMock()
        valid = 'test_mop_' + 'a' * 32
        with patch.dict(sys.modules, {'psycopg': SimpleNamespace(connect=connect)}):
            for name, pid in [('mop_ci', 123), ('test_mop_', 123), (valid, None),
                              (valid, 0), (valid, True), (valid, '123')]:
                with self.subTest(database=name, pid=pid):
                    with self.assertRaisesRegex(ValueError, 'owned test database|known backend PID'):
                        _drop_backend_state(dict(NAME=name, HOST='fixture-only', PORT='5432',
                                                 USER='fixture-only', PASSWORD='synthetic'), pid)
        connect.assert_not_called()

    def test_client_fixture_runs_clean_wheel_tls_and_real_postgres_contracts(self):
        # HYPOTHESIS: один control_tls не доказывает Django admission contracts.
        # SOLUTION: последовательный script с &&, обе проверки обязательны.
        # RESULT: missing PostgreSQL command red, затем четыре fixture checks green.
        # STATUS: FIXED — see #642.
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            result, _, commands = self.invoke(root, check='client')
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        script = next(word.removeprefix('TESTS=') for word in runner if word.startswith('TESTS='))
        self.assertEqual(script.split(' && '), [
            shlex.join(['python3', 'tests/control_tls.py']),
            shlex.join(['python3', 'tests/controller.py', '_control_client_cases'])])

    def test_failed_cleanup_cannot_report_a_successful_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            with self.assertRaisesRegex(RuntimeError, 'cleanup|removed|incomplete'):
                self.invoke(root, cleanup_failure=True)


if __name__ == '__main__':
    with offline():
        unittest.main()
