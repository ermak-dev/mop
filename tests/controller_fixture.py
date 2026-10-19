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

    # HYPOTHESIS: Django DiscoverRunner молча считает пропуск зелёным
    # результатом: непривилегированный CI прогонял root-only UID границу как
    # пропуск и уходил зелёным, ни разу её не проверив (#674).
    # SOLUTION: контракт #229 распространён на Django runner -- под
    # MOP_TESTS_STRICT=1 каждый skipped результат входит в счёт провалов.
    def test_strict_runner_counts_skipped_django_results_as_failures(self):
        from types import SimpleNamespace
        from _controller_fixture import strict_failure_count
        clean = SimpleNamespace(failures=[], errors=[], skipped=[])
        one_skip = SimpleNamespace(failures=[], errors=[], skipped=[object()])
        mixed = SimpleNamespace(failures=[object()], errors=[object()],
                                skipped=[object(), object()])
        self.assertEqual(strict_failure_count(clean, strict=True), 0)
        self.assertEqual(strict_failure_count(one_skip, strict=False), 0)
        self.assertEqual(strict_failure_count(one_skip, strict=True), 1)
        self.assertEqual(strict_failure_count(mixed, strict=True), 4)
        self.assertEqual(strict_failure_count(mixed, strict=False), 2)

    ROOT_GATE_MODULE = '_credential_uid_gate.py'
    # Обязательное множество root-only ID (#674): ровно эти два случая
    # перенесены из общих модулей #669 в узкий root gate, без изменения
    # утверждений. Новый root-only случай расширяет этот набор явно.
    REQUIRED_ROOT_ONLY_IDS = frozenset({
        ('CredentialSocketUids', 'test_distinct_frontend_uid_uses_only_group_protected_socket'),
        ('AddressedCredentialUids', 'test_addressed_delivery_uses_actual_frontend_uid_without_rights_inference'),
        ('InstalledCredentialUids', 'test_fresh_installer_starts_rootless_daemon_with_separate_pg_login_and_frontend_uid'),
    })

    # #742: новая actual UID граница — union, не замена исходного #674 набора.
    MANAGEMENT_ROOT_ONLY_IDS = frozenset({('ManagementContextUids',
        'test_management_context_actual_frontend_uid_has_no_human_or_generic_token_authority')})

    def _test_inventory(self, name):
        import ast
        from pathlib import Path
        source = (Path(__file__).resolve().parent / name).read_text(encoding='utf-8')
        tree = ast.parse(source)
        ids = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name.startswith('test_'):
                        ids.add((node.name, item.name))
        return ids, source, tree

    # HYPOTHESIS: два root-only метода в общих модулях под ci уходили в
    # skipTest-подобный пропуск; перенос должен быть переносом, а не потерей
    # -- union счёта случаев #669 сохраняется, gate держит ровно
    # обязательные ID, не пропускает и запускает ровно их через точечный
    # вход fixture, не весь унаследованный класс.
    def test_uid_gate_holds_exactly_the_moved_root_only_ids(self):
        import ast
        gate_ids, gate_source, tree = self._test_inventory(self.ROOT_GATE_MODULE)
        required = self.REQUIRED_ROOT_ONLY_IDS | self.MANAGEMENT_ROOT_ONLY_IDS
        self.assertEqual(gate_ids, required)
        self.assertNotIn('skipTest', gate_source)
        self.assertIn('os.geteuid() != 0', gate_source)
        # Точечный вход: _credential_uid_fixture.py объявляет метки ровно
        # двух перенесённых случаев -- не модуль и не весь класс.
        entry = Path(__file__).resolve().with_name('_credential_uid_fixture.py')
        fixture_source = entry.read_text(encoding='utf-8')
        labels = ()
        for node in ast.walk(ast.parse(fixture_source)):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'GATE_LABELS'
                            for target in node.targets)):
                labels = ast.literal_eval(node.value)
        self.assertTrue(labels, 'the gate entry must declare its exact labels')
        expected = [f'{self.ROOT_GATE_MODULE.removesuffix(".py")}.{cls}.{method}'
                    for cls, method in required]
        # #669 добавляет application send/take UID, не заменяет ни один из
        # исходных credential IDs и не разрешает wildcard/class discovery.
        private_ids, private_source, _ = self._test_inventory('_endpoint_credential_uid_gate.py')
        self.assertEqual(private_ids, {('EndpointCredentialUids',
            'test_private_reports_cross_real_uid_without_root_or_caller_authority'),
            ('EndpointCredentialUids','test_private_wss_default_root_crosses_actual_frontend_uid')})
        self.assertNotIn('skipTest', private_source)
        expected += [f'_endpoint_credential_uid_gate.{cls}.{method}' for cls, method in private_ids]
        self.assertEqual(sorted(labels), sorted(expected))
        suite_ids = set()
        root_ids = gate_ids | private_ids
        for name in ('_credential_cases.py', '_credential_binding_cases.py', '_endpoint_credential_cases.py'):
            ids, source = self._test_inventory(name)[:2]
            self.assertNotIn('skipTest', source, f'{name} must not skip unprivileged')
            self.assertEqual(ids & root_ids, set(),
                             f'{name} still carries root-only cases')
            suite_ids |= ids
        # #711: перенос, не потеря -- сохраняются точные исходные ID #669,
        # а не общий счётчик, который запрещал добавлять socket checks.
        from _credential_inventory import missing_original
        self.assertEqual(missing_original(suite_ids | root_ids), set())
        self.assertEqual(suite_ids & root_ids, set())

    # #669: entry уже существует; проверка закрепляет обязательный запуск
    # настоящих application cases, отдельно от root-only и pure protocol.
    def test_private_endpoint_credentials_have_a_mandatory_nonroot_entrypoint(self):
        import runpy
        from mop.cli.dev.test import plan
        top = Path(__file__).resolve().parent
        entries = ('endpoint_credentials.py', 'endpoint_credential_protocol.py')
        selected = plan([], [path.name for path in top.iterdir()])
        for name in entries:
            self.assertIn(name, selected)
        self.assertNotIn('_endpoint_credential_cases.py', selected)
        self.assertNotIn('_endpoint_credential_uid_gate.py', selected)
        with patch('_controller_fixture.main', return_value=23) as main:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(top / entries[0]), run_name='__main__')
        main.assert_called_once_with(['_endpoint_credential_cases'])
        self.assertEqual(caught.exception.code, 23)

    # #668: selectable helper не должен остаться вне обязательного CI gate.
    # RED: entry отсутствовал; отдельный check исполняется и в hermetic HOME.
    def test_owned_management_regression_is_selected_by_the_normal_gate(self):
        from mop.cli.dev.test import plan
        entry = Path(__file__).with_name('controller_lifecycle.py')
        self.assertTrue(entry.is_file(), 'owned lifecycle regression needs a mandatory check-file entry')
        self.assertEqual(plan([], [entry.name, '_controller_lifecycle_cases.py']), [entry.name])
        self.assertIn("'_controller_lifecycle_cases.OwnedLifecycle'", entry.read_text())

    # HYPOTHESIS (#711): exact count66 падал после добавления новых случаев;
    # замена на >=66 скрыла бы удаление исходной проверки новой с другим ID.
    # SOLUTION: baseline точных ID из4f60fb0a, проверяется его сохранность.
    # RESULT: RED94!=66; добавления разрешены, каждое исходное удаление видно.
    # STATUS: FIXED — see #711
    def test_uid_inventory_allows_growth_but_detects_every_original_loss(self):
        from _credential_inventory import ORIGINAL_CREDENTIAL_IDS, RENAMED_CREDENTIAL_IDS, missing_original
        self.assertEqual(len(ORIGINAL_CREDENTIAL_IDS), 66)
        added = {('FutureCredentialCases', 'test_new_boundary')}
        self.assertEqual(missing_original(ORIGINAL_CREDENTIAL_IDS | added), set())
        for original in ORIGINAL_CREDENTIAL_IDS:
            with self.subTest(original=original):
                self.assertEqual(missing_original((ORIGINAL_CREDENTIAL_IDS - {original}) | added), {original})
        for original, successor in RENAMED_CREDENTIAL_IDS.items():
            renamed = (ORIGINAL_CREDENTIAL_IDS - {original}) | {successor}
            self.assertEqual(missing_original(renamed), set())
            self.assertEqual(missing_original(renamed - {successor}), {original})

    # HYPOTHESIS: узкий root gate обязан прогоняться обязательно, иначе
    # непривилегированный CI снова зелёный без реальной UID-проверки (#674).
    # SOLUTION: SETUP объявленного CI job гоняет gate под root после
    # создания пользователей и до exec su ci; TESTS job'а остаётся чистым
    # непривилегированным suite -- внутри test-контейнера нет docker-сокета,
    # и командлет fixture там не запустить.
    # RESULT: red до объявления; фактический прогон gate ниже.
    def test_declared_ci_job_runs_the_root_uid_gate_inside_the_owned_fixture(self):
        import yaml
        top = Path(__file__).resolve().parents[1]
        with (top / '.gitlab-ci.yml').open() as stream:
            ci = yaml.safe_load(stream)
        setup = ci['.tests']['variables']['SETUP']['value']
        tests_value = ci['test:py3.12']['variables']['TESTS']
        tests_script = tests_value.get('value') if isinstance(tests_value, dict) else tests_value
        useradd = setup.index('useradd')
        gate_builder = (setup.index('python3 tests/_credential_uid_fixture.py')
                        if 'python3 tests/_credential_uid_fixture.py' in setup else -1)
        su_ci = setup.index('exec su ci')
        self.assertLess(useradd, gate_builder, 'users must exist before the gate runs')
        self.assertLess(gate_builder, su_ci, 'the root gate runs before the unprivileged suite')
        self.assertEqual(setup.count('python3 tests/_credential_uid_fixture.py'), 1,
                         'exactly one declared gate run')
        self.assertEqual(tests_script, 'bin/mop dev test -v',
                         'TESTS stays the pure unprivileged suite')
        self.assertNotIn('--cross-uid', tests_script)

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

    # BUG (#677): launch-файлы проверок -- обычные unittest-модули без Django
    # labels: DiscoverRunner на 'body_launch' отдаёт _FailedTest, а без своего
    # --check запускать их в объявленном образе можно только руками, мимо
    # fixture с её сетью и cleanup.
    # HYPOTHESIS: конечного --check launch (body_launch + api_body_spec одним
    # списком, тем же shlex-контрактом, что остальные checks) достаточно.
    # SOLUTION: отдельный режим в controller; результат -- два имени файла в
    # TESTS, оба обязательны, без shell-source и без wildcard.
    # RESULT: режим отказал до реализации (argparse exit 2), затем fixture
    # несёт оба скрипта; actual run в python:3.12-slim см. отчёт #677.
    def test_launch_gate_selects_both_finite_check_files_in_owned_fixture(self):
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            result, _, commands = self.invoke(self.source(directory), check='launch')
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        script = next(word.removeprefix('TESTS=') for word in runner if word.startswith('TESTS='))
        self.assertEqual(shlex.split(script), ['python3', 'tests/body_launch.py',
                                               '&&', 'python3', 'tests/api_body_spec.py'])

    # #695: упакованный дашборд (реальный wheel + uvicorn) требует PostgreSQL
    # фикстуры; без своего режима его не прогнать локально, только в CI.
    def test_dashboard_gate_runs_the_packaged_controller_check(self):
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            result, _, commands = self.invoke(self.source(directory), check='dashboard')
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        script = next(word.removeprefix('TESTS=') for word in runner if word.startswith('TESTS='))
        self.assertEqual(shlex.split(script), ['python3', 'tests/packaged_dashboard.py'])

    def test_runtime_gate_can_be_selected_in_owned_postgres_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _, commands = self.invoke(self.source(directory), check='controller_runtime')
        self.assertEqual(result, 0)
        runner = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        self.assertIn('TESTS=python3 tests/controller_runtime.py', runner)

    # #697: DiscoverRunner.run_tests возвращает ЦЕЛОЕ число провалов, а не
    # объект результата; strict_failure_count над целым всегда видит ноль,
    # и gate выходит 0 даже при FAILED. Агрегация выхода обязана считать
    # целое и добавлять skipped только по захваченному объекту результата.
    # RED: run_exit_code не существовал, а main маршрутизировал целое в
    # strict_failure_count. RESULT: 21/21 офлайн; живой зонд — намеренно
    # красный лейбл даёт exit 1, зелёный 0.
    # STATUS: FIXED — see #697
    def test_controller_gate_exit_aggregates_the_integer_run_result(self):
        from _controller_fixture import run_exit_code
        import inspect as _inspect
        from pathlib import Path as _P
        import _controller_fixture as _mod
        self.assertEqual(run_exit_code(0, strict=True, skipped=0), 0)
        self.assertEqual(run_exit_code(39, strict=False, skipped=5), 1)
        self.assertEqual(run_exit_code(0, strict=True, skipped=2), 1)
        self.assertEqual(run_exit_code(0, strict=False, skipped=2), 0)
        source = _P(_mod.__file__).read_text(encoding='utf-8')
        self.assertIn('run_exit_code(', source,
                      'the fixture main must route its exit through run_exit_code')
        self.assertNotIn('int(bool(strict_failure_count(suite_result', source,
                         'the integer run_tests result must not reach strict_failure_count')

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

    # HYPOTHESIS (#668): UUID-name недостаточен для DROP: collision/replacement
    # не становятся нашими ресурсами. Native maintenance purpose ограничен
    # точным именем, созданным этим объектом OID/owner/nonce, не произвольным SQL.
    # SOLUTION: два instance hooks creation, native postgres-only cursor с 30 s
    # общим бюджетом; default backend/pool и 5/10 s приложения не меняются.
    # RESULT: behavioral RED/GREEN PG17 в _controller_lifecycle_cases; 29 pure
    # safety checks green, исходные strict skip/cross-UID inventories сохранены.
    # STATUS: FIXED — see #668 (не утверждение об исходном orphan-owner).
    def owned_database(self):
        from types import SimpleNamespace
        from _controller_fixture import OwnedTestDatabase
        name = 'test_mop_' + 'a' * 32
        config = dict(NAME='mop_ci', HOST='fixture-only', PORT='5432',
                      USER='fixture-user', PASSWORD='synthetic-do-not-log',
                      OPTIONS={'pool': {'max_size': 8}, 'sslmode': 'disable',
                               'options': '-c statement_timeout=5000'}, TEST={'NAME': name})
        creation = SimpleNamespace(connection=SimpleNamespace(settings_dict=config,
            close_pool=unittest.mock.Mock()), _get_test_db_name=lambda: name,
            sql_table_creation_suffix=lambda: '')
        return OwnedTestDatabase(creation, name), creation

    def test_owned_management_is_postgres_only_finite_and_does_not_inherit_runtime_policy(self):
        import psycopg
        from unittest.mock import MagicMock
        from _controller_fixture import maintenance_cursor
        owner, creation = self.owned_database()
        native = MagicMock()
        native.__enter__.return_value = native
        with patch.object(psycopg.Connection, 'connect', return_value=native) as connect:
            with maintenance_cursor(creation.connection.settings_dict):
                pass
        options = connect.call_args.kwargs
        self.assertEqual(options['dbname'], 'postgres')
        self.assertEqual(options['connect_timeout'], 5)
        self.assertIn('statement_timeout=30000', options['options'])
        self.assertIn('lock_timeout=2000', options['options'])
        self.assertEqual(options['sslmode'], 'disable')
        self.assertNotIn('pool', options)
        native.close.assert_called_once()

    def test_runner_wires_owned_management_without_replacing_the_runtime_backend(self):
        import hermetic
        from _controller_fixture import main, OwnedTestDatabase
        import _pytest_django
        observed = []
        def run_tests(labels):
            from django.conf import settings
            from django.db import connection
            from contextlib import nullcontext
            from types import SimpleNamespace
            self.assertEqual(labels, ['_controller_cases'])
            fixture = _pytest_django._owned_django_management.__wrapped__(
                SimpleNamespace(unblock=nullcontext))
            try:
                owner = next(fixture)
                creation = connection.creation
                self.assertIsInstance(owner, OwnedTestDatabase)
                self.assertIs(creation._create_test_db.__self__, owner)
                with patch.object(owner, 'destroy') as destroy:
                    creation._destroy_test_db(owner.name, 0)
                    destroy.assert_called_once_with(owner.name, 0)
                self.assertEqual(settings.DATABASES['default']['ENGINE'], 'mop.server.controller.postgresql')
                self.assertEqual(settings.CONTROLLER_DB_TIMEOUT, 5)
                self.assertEqual(settings.CONTROLLER_TRANSACTION_TIMEOUT, 10)
                observed.append(owner.name)
            finally:
                fixture.close()
            return 0
        with patch.dict(hermetic.FIXTURE_ENV, {'MOP_TEST_POSTGRES_HOST': 'fixture-only'}), \
                patch.object(_pytest_django, 'run_labels', side_effect=run_tests):
            self.assertEqual(main(['_controller_cases']), 0)
        self.assertEqual(len(observed), 1)

    def test_owned_management_transport_timeout_fences_and_is_not_unbounded(self):
        import psycopg
        from unittest.mock import MagicMock
        from _controller_fixture import maintenance_cursor
        owner, creation = self.owned_database()
        raw, captured = MagicMock(), []
        def connect(cls, **kwargs):
            captured.append(cls)
            return raw
        with patch.object(psycopg.Connection, 'connect', new=classmethod(connect)), \
                patch('_controller_fixture.time.monotonic', side_effect=[0, 1, 31]), \
                patch.object(psycopg.Connection, 'wait', side_effect=psycopg.errors._WaitTimeout) as wait:
            with maintenance_cursor(creation.connection.settings_dict):
                fake = captured[0](MagicMock())
                with self.assertRaisesRegex(TimeoutError, 'maintenance deadline'):
                    captured[0].wait(fake, iter(()))
                self.assertEqual(wait.call_args.kwargs['timeout'], 29)
                fake.pgconn.finish.assert_called_once()
                fake.pgconn.finish.reset_mock()
                with self.assertRaisesRegex(TimeoutError, 'maintenance deadline'):
                    captured[0].wait(fake, iter(()))
                self.assertEqual(wait.call_count, 1, 'expired budget must not enter another driver wait')
                fake.pgconn.finish.assert_called_once()
        raw.close.assert_called_once()

    def test_owned_management_rejects_collision_without_destroy_or_retry(self):
        from unittest.mock import MagicMock
        owner, creation = self.owned_database()
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError('database already exists')
        with patch('_controller_fixture.maintenance_cursor') as managed:
            managed.return_value.__enter__.return_value = cursor
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                owner.create(0, True, False)
        self.assertEqual(cursor.execute.call_count, 1)
        self.assertTrue(cursor.execute.call_args.args[0].startswith('CREATE DATABASE '))
        self.assertIsNone(owner.identity)

    def test_owned_management_refuses_unowned_or_replaced_database_before_drop(self):
        from unittest.mock import MagicMock
        owner, creation = self.owned_database()
        cursor = MagicMock()
        with patch('_controller_fixture.maintenance_cursor') as managed:
            managed.return_value.__enter__.return_value = cursor
            with self.assertRaisesRegex(ValueError, 'owned|created'):
                owner.destroy(owner.name, 0)
            managed.assert_not_called()
            owner.identity = (111, 222, owner.marker)
            for metadata in ((112, 222, owner.marker), (111, 223, owner.marker),
                             (111, 222, 'foreign-marker'), None):
                cursor.reset_mock()
                cursor.fetchone.return_value = metadata
                with self.subTest(metadata=metadata), self.assertRaisesRegex(RuntimeError, 'ownership'):
                    owner.destroy(owner.name, 0)
                self.assertFalse(any('DROP DATABASE' in str(call) for call in cursor.execute.call_args_list))
        with self.assertRaisesRegex(ValueError, 'owned|exact'):
            owner.destroy('mop_ci', 0)
        owner.creation.connection.settings_dict['TEST']['NAME'] = 'test_mop_' + 'b' * 32
        with self.assertRaisesRegex(ValueError, 'owned|exact'):
            owner.destroy(owner.name, 0)

    def test_owned_cleanup_requires_quiescence_and_propagates_drop_errors(self):
        from unittest.mock import MagicMock
        owner, creation = self.owned_database()
        owner.identity = (111, 222, owner.marker)
        cursor = MagicMock()
        cursor.fetchone.return_value = owner.identity
        cursor.fetchall.return_value = [(123, 'idle', 'Client', 'ClientRead')]
        with patch('_controller_fixture.maintenance_cursor') as managed:
            managed.return_value.__enter__.return_value = cursor
            with self.assertRaisesRegex(RuntimeError, 'quiescent'):
                owner.destroy(owner.name, 0)
            self.assertFalse(any('DROP DATABASE' in str(call) for call in cursor.execute.call_args_list))
            cursor.fetchall.return_value = []
            def fail_drop(query, *args, **kwargs):
                if query.startswith('DROP DATABASE'):
                    raise RuntimeError('owned DROP failed')
            cursor.execute.side_effect = fail_drop
            with self.assertRaisesRegex(RuntimeError, 'owned DROP failed'):
                owner.destroy(owner.name, 0)
        self.assertIsNotNone(owner.identity, 'failed cleanup must not erase ownership evidence')
        creation.connection.close_pool.assert_called()

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

    # HYPOTHESIS: объявленный SETUP обязан строить ровно одну root-only пару
    # UID и гонать ею только перемещённые UID-случаи; расширенный su ci или
    # подменённый gate молча менял бы границу привилегий (#674).
    # SOLUTION: builder только проверяет объявленную пару и отказывает не
    # под root; useradd остаётся в SETUP; корневой прогон получает только
    # узкий gate-модуль, обычный -- полные общие модули без root-only случаев.
    def test_cross_uid_fixture_builds_one_gate_pair_and_runs_only_the_gate_module(self):
        from pathlib import Path as P
        source = (P(__file__).resolve().parent / '_credential_uid_fixture.py').read_text()
        self.assertIn('uid_pair', source)
        self.assertNotIn('useradd', source,
                         'the builder verifies the declared pair; useradd stays in SETUP')
        self.assertIn("os.geteuid() != 0", source)
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            setup_path = root / '.gitlab-ci.yml'
            setup_path.write_text(setup_path.read_text().replace('value: exit 0',
                "value: |\n        python3 tests/_credential_uid_fixture.py\n"
                '        exec su ci -s /bin/bash -c "$TESTS"'))
            result, _, commands = self.invoke(root, arguments=['--postgres', '17',
                '--check', 'credential_service', '--cross-uid'])
        self.assertEqual(result, 0)
        worker = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        script = next(word.removeprefix('TESTS=') for word in worker if word.startswith('TESTS='))
        self.assertEqual(script, 'python3 tests/_credential_uid_fixture.py')
        self.assertIn('exec bash -c', worker[-1])
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            result, _, commands = self.invoke(root, check='credential_service')
        self.assertEqual(result, 0)
        worker = next(command for command in commands if any(word.startswith('TESTS=') for word in command))
        script = next(word.removeprefix('TESTS=') for word in worker if word.startswith('TESTS='))
        self.assertIn('tests/credential_service.py', script)
        self.assertNotIn('_credential_uid_gate', script,
                         'the default unprivileged suite must not import the root gate')

    def test_failed_cleanup_cannot_report_a_successful_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source(directory)
            with self.assertRaisesRegex(RuntimeError, 'cleanup|removed|incomplete'):
                self.invoke(root, cleanup_failure=True)


if __name__ == '__main__':
    with offline():
        unittest.main()
