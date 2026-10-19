"""Контракты перехода на pytest без потери прежних проверок (#692).

HYPOTHESIS: командлет сам исполняет каждый файл и не отдаёт pytest ни
collection, ни результаты; Django entrypoints повторяют DiscoverRunner.
SPEC: один pytest session на выбранные файлы, исходные имена checks и
Django test ID сохранены; strict отказывает на любом пропуске.
SOLUTION: pytest владеет verdict/collection; legacy main guards изолированы,
Django cases используют native collection и общую owned pytest-django fixture.
RESULT: RED — обход pytest, потерянные system checks и зелёный cleanup failure;
GREEN — 24 focused cases и исходные 5 UID checks на Python 3.12/PostgreSQL 17,
полный исходный Django ID union проверен collect-only; SQL budgets неизменны.
Переходный этап: legacy assertion bodies и HOME matrix scheduler сохранены.
STATUS: FIXED — see #692
"""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hermetic
from _lib import run_command
from _controller_fixture import main
from mop.cli.dev import test as command

ROOT = Path(__file__).resolve().parents[1]


class PytestCommand(unittest.TestCase):
    def invoke(self, args, code=0):
        calls = []
        pytest = SimpleNamespace(main=lambda argv: calls.append(argv) or code)
        with patch.dict(sys.modules, pytest=pytest), \
                patch.object(command, 'root', return_value=str(ROOT)), \
                patch.object(command.os, 'listdir', return_value=['first.py', 'second.py', '_helper.py']), \
                patch.object(command.subprocess, 'run', side_effect=AssertionError(
                    'per-file execution still bypasses pytest')):
            out, err, result = run_command(command.main, args)
        self.assertEqual(len(calls), 1, 'the entire selection must share one pytest session')
        return calls[0], result, out + err

    def test_one_pytest_session_receives_exact_selected_check_names(self):
        argv, result, _ = self.invoke(['first', 'tests/second.py', 'first'])
        self.assertEqual(result, 0)
        self.assertEqual(argv[-2:], [str(ROOT / 'tests/first.py'), str(ROOT / 'tests/second.py')])
        self.assertIn('_pytest_plugin', argv)

    def test_strict_and_verbose_are_pytest_options_not_child_runner_state(self):
        argv, result, _ = self.invoke(['first', '--strict', '-v'], code=1)
        self.assertEqual(result, 1)
        self.assertIn('--mop-strict', argv)
        self.assertIn('-v', argv)

    def test_empty_selection_keeps_the_complete_existing_check_set(self):
        argv, _, _ = self.invoke([])
        self.assertEqual(argv[-2:], [str(ROOT / 'tests/first.py'), str(ROOT / 'tests/second.py')])

    def test_pytest_collection_failure_cannot_be_reported_as_success(self):
        _, result, _ = self.invoke(['first'], code=5)
        self.assertNotEqual(result, 0)

    def test_pytest_loads_the_packaging_dependency_before_the_check_namespace(self):
        import importlib.util
        # Имя существующего check packaging.py нельзя отдавать зависимости
        # pytest packaging.version: это уже дало настоящий import failure PG17.
        previous = sys.modules.get('packaging')
        with patch.dict(sys.modules):
            for name in tuple(sys.modules):
                if name == 'packaging' or name.startswith('packaging.'):
                    del sys.modules[name]
            calls = []
            def session(argv):
                module = sys.modules.get('packaging')
                self.assertIsNotNone(module, 'pytest dependencies must load before tests/ enters sys.path')
                self.assertTrue(hasattr(module, '__path__'), 'the check packaging.py shadowed the dependency')
                calls.append(argv)
                return 0
            with patch.dict(sys.modules, pytest=SimpleNamespace(main=session)), \
                    patch.object(command, 'root', return_value=str(ROOT)):
                _, _, result = run_command(command.main, ['pytest_harness'])
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 1)
        self.assertIs(sys.modules.get('packaging'), previous)

    def test_real_pytest_plan_runs_existing_entrypoints_without_losing_their_main_guards(self):
        # Узкий plan: никаких длинных controller/hermetic повторных прогонов.
        names = ['tests_lib', 'test_output', 'controller_fixture', 'hermetic_runs', 'ci', 'cli', 'packaging']
        with patch.object(command, 'root', return_value=str(ROOT)):
            out, err, result = run_command(command.main, [*names, '--strict'])
        self.assertEqual(result, 0, out + err)
        self.assertIn(f'{len(names)} passed', out)

    def test_real_wrapper_stays_hermetic_in_clean_and_hostile_home(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='mop-pytest-clean-') as clean:
            hostile, values = hermetic.hostile_home()
            copy = hermetic.hostile_repo(values)
            for root, home, environment in ((str(ROOT), clean, {}), (copy, hostile, values)):
                with self.subTest(hostile=root == copy), \
                        patch.dict(os.environ, {**environment, 'HOME': home}), \
                        patch.object(command, 'root', return_value=root):
                    out, err, result = run_command(command.main, ['config', 'tests_lib', '--strict'])
                    self.assertEqual(result, 0, out + err)
                    self.assertIn('2 passed', out)

    # HYPOTHESIS: configured label-run скрывает старый unittest main guard.
    # RED: прямой запуск отдавал dotted label в unittest.loader._FailedTest.
    # SOLUTION: тот же shared bootstrap, что у остальных Django entrypoints.
    # RESULT: GREEN — native inventory проходит без унаследованных settings.
    # STATUS: FIXED — see #692
    def test_direct_entrypoint_bootstraps_the_native_inventory_without_inherited_settings(self):
        import subprocess
        label = 'pytest_harness.NativeInventory.test_every_existing_django_entrypoint_collects_its_original_id_union_without_running_it'
        env = hermetic.child_env({})
        env.pop('DJANGO_SETTINGS_MODULE', None)
        env.pop('MOP_CONTROLLER_BOOTSTRAP', None)
        result = subprocess.run([sys.executable, str(ROOT / 'tests/pytest_harness.py'), label],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('::test_every_existing_django_entrypoint_collects_its_original_id_union_without_running_it PASSED', result.stdout)
        self.assertIn('1 passed', result.stdout)

    def test_django_entrypoints_no_longer_construct_their_own_test_runner(self):
        source = (ROOT / 'tests/_controller_fixture.py').read_text()
        self.assertNotIn('class DiagnosticRunner', source,
                         'Django entries must share the pytest-django fixture')
        self.assertNotIn('runner.run_tests(', source,
                         'native pytest collection must own Django execution')

    def test_pytest_dependencies_are_declared_separately_from_runtime(self):
        scope = {}
        exec((ROOT / 'mop/common/deps.py').read_text(), scope)
        declared = scope.get('TEST', ())
        self.assertTrue(any(value.startswith('pytest==') for value in declared),
                        'pytest must be a pinned declared test dependency')
        self.assertTrue(any(value.startswith('pytest-django==') for value in declared),
                        'pytest-django must be a pinned declared test dependency')
        self.assertFalse(any(value.startswith(('pytest==', 'pytest-django=='))
                             for value in scope['PIP'] + scope['SERVER']))


class NativeInventory(unittest.TestCase):
    def inventory(self):
        from types import ModuleType
        module = ModuleType('_pytest_692_inventory')
        module.Example = type('Example', (unittest.TestCase,), {
            '__module__': module.__name__,
            'test_first': lambda self: None,
            'test_second': lambda self: None,
        })
        return module

    def test_shared_database_fixture_retains_django_system_checks_after_database_setup(self):
        import inspect
        from contextlib import nullcontext
        import _pytest_django
        fixture = _pytest_django.django_db_setup.__wrapped__
        upstream = object()
        arguments = {'_owned_django_management': None, 'django_db_setup': upstream}
        if 'django_db_blocker' in inspect.signature(fixture).parameters:
            arguments['django_db_blocker'] = SimpleNamespace(unblock=nullcontext)
        with patch('django.core.management.call_command') as checks:
            self.assertIs(fixture(**arguments), upstream)
        checks.assert_called_once_with('check', verbosity=0, databases=['default'])

    def test_every_existing_django_entrypoint_collects_its_original_id_union_without_running_it(self):
        import ast
        import _pytest_django
        labels = []
        for path in sorted((ROOT / 'tests').glob('*.py')):
            if path.name.startswith('_'):
                continue
            tree = ast.parse(path.read_text())
            if not any(isinstance(node, ast.ImportFrom) and node.module == '_controller_fixture'
                       and any(alias.name == 'main' for alias in node.names) for node in tree.body):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'main' and node.args):
                    continue
                for value in ast.walk(node.args[0]):
                    if isinstance(value, ast.List):
                        labels.extend(ast.literal_eval(value))
        labels = list(dict.fromkeys(labels))
        self.assertIn('_controller_cases', labels)
        self.assertIn('_credential_cases', labels)
        expected = _pytest_django.cases(labels)
        collected = []

        class Inventory:
            def pytest_collection_finish(self, session):
                collected.extend(_pytest_django.item_id(item) for item in session.items)

        original = _pytest_django.pytest.main
        with patch.object(_pytest_django.pytest, 'main', side_effect=lambda args:
                original([*args, '--collect-only'], plugins=[Inventory()])):
            out, err, result = run_command(_pytest_django.run_labels, labels)
        self.assertEqual(result, 0, out + err)
        self.assertEqual(collected, list(expected), 'the full original Django ID union must survive collection')

    def test_overlapping_labels_keep_the_exact_original_unittest_id_union(self):
        from _pytest_django import cases
        module = self.inventory()
        with patch.dict(sys.modules, {module.__name__: module}):
            selected = cases([module.__name__, module.__name__ + '.Example',
                              module.__name__ + '.Example.test_first'])
        self.assertEqual(list(selected), [module.__name__ + '.Example.test_first',
                                         module.__name__ + '.Example.test_second'])

    def test_native_collection_rejects_a_lost_id_instead_of_deselecting_it(self):
        import _pytest_django
        from unittest.mock import Mock
        module = self.inventory()
        with patch.dict(sys.modules, {module.__name__: module}):
            expected = _pytest_django.cases([module.__name__])
        config = SimpleNamespace(_mop_cases=expected, hook=Mock())
        items = [SimpleNamespace(cls=module.Example, name='test_first')]
        with self.assertRaisesRegex(_pytest_django.pytest.UsageError,
                                    'Django collection lost test IDs: .*Example.test_second'):
            _pytest_django.pytest_collection_modifyitems(None, config, items)

    def test_native_collection_orders_and_deduplicates_the_same_exact_ids(self):
        import _pytest_django
        from unittest.mock import Mock
        module = self.inventory()
        with patch.dict(sys.modules, {module.__name__: module}):
            expected = _pytest_django.cases([module.__name__])
        config = SimpleNamespace(_mop_cases=expected, hook=Mock())
        first = SimpleNamespace(cls=module.Example, name='test_first')
        second = SimpleNamespace(cls=module.Example, name='test_second')
        duplicate = SimpleNamespace(cls=module.Example, name='test_first')
        extra = SimpleNamespace(cls=module.Example, name='test_unrequested')
        items = [second, first, duplicate, extra]
        _pytest_django.pytest_collection_modifyitems(None, config, items)
        self.assertEqual(items, [first, second])
        config.hook.pytest_deselected.assert_called_once_with(items=[duplicate, extra])

    def test_strict_native_test_and_collection_skips_become_failures_with_reasons(self):
        import _pytest_plugin
        for strict in (False, True):
            with self.subTest(strict=strict), patch.object(hermetic, 'STRICT', strict):
                report = SimpleNamespace(skipped=True, outcome='skipped', nodeid='owned::boundary',
                                         longreprtext='owned skip reason', longrepr='owned skip reason')
                hook = _pytest_plugin.pytest_runtest_makereport(
                    SimpleNamespace(nodeid=report.nodeid), None)
                next(hook)
                with self.assertRaises(StopIteration):
                    hook.send(SimpleNamespace(get_result=lambda: report))
                self.assertEqual(report.outcome, 'failed' if strict else 'skipped')
                self.assertIn('owned skip reason', report.longrepr)
                report.outcome = 'skipped'
                _pytest_plugin.pytest_collectreport(report)
                self.assertEqual(report.outcome, 'failed' if strict else 'skipped')
                self.assertIn('owned skip reason', report.longrepr)


class ProcessOwnership(unittest.TestCase):
    # Здесь контракт pytest fixture; настоящие группы/detach/reap проверяет
    # pytest_ownership, а не fake PID, который раньше принимали за эксперимент.
    def fixture(self, process):
        import _pytest_plugin
        request = SimpleNamespace(node=SimpleNamespace(path=ROOT / 'tests/state.py'))
        with patch.object(_pytest_plugin, 'OwnedProcess', return_value=process) as start:
            fixture = _pytest_plugin.owned_legacy_process.__wrapped__(request)
            self.assertIs(next(fixture), process)
        return fixture, Path(start.call_args.kwargs['env']['TMPDIR'])

    def test_successful_child_cannot_leave_an_owned_process_group_or_temp_root(self):
        from unittest.mock import Mock
        process = Mock()
        fixture, directory = self.fixture(process)
        process.close.side_effect = lambda: self.assertTrue(directory.exists(), 'HOME removed before join')
        fixture.close()
        process.close.assert_called_once_with()
        self.assertFalse(directory.exists())

    def test_cancelled_reader_kills_only_its_group_reaps_and_removes_its_temp_root(self):
        from unittest.mock import Mock
        process = Mock()
        fixture, directory = self.fixture(process)
        process.close.side_effect = lambda: self.assertTrue(directory.exists(), 'HOME removed before cancel/join')
        with self.assertRaisesRegex(KeyboardInterrupt, 'owned cancellation'):
            fixture.throw(KeyboardInterrupt('owned cancellation'))
        process.close.assert_called_once_with()
        self.assertFalse(directory.exists())


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:] or ['pytest_harness']))
