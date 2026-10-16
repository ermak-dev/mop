"""Изоляция серверных зависимостей и холодного входа #632."""
import ast
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _lib  # noqa: F401,E402 — lifetime временных файлов
import hermetic  # noqa: F401,E402 — старые настройки машины изолированы
from mop.common import deps

# HYPOTHESIS: установка клиента не умеет выбирать серверную группу;
# bootstrap командлета проходит через чтение старой .env.
# Проверки метаданных исполняются без Django и без сетевой установки.
# SOLUTION: отдельный server extra и stdlib dispatch без старой .env.
# RESULT: clean-install gate отдельно проверяет настоящий клиентский venv.
# STATUS: FIXED — see #632
class Packaging(unittest.TestCase):
    def test_server_extra_is_separate_from_client(self):
        server = getattr(deps, 'SERVER', ())
        self.assertTrue(server, 'server dependency group must be declared')
        for name in ('Django', 'djangorestframework', 'channels', 'uvicorn', 'psycopg'):
            self.assertTrue(any(x.lower().startswith(name.lower()) for x in server), name)
        self.assertFalse(set(server) & set(deps.PIP))
        spec = importlib.util.spec_from_file_location('setup_metadata', ROOT / 'setup.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.requirements('server'), list(server))
        self.assertEqual(module.requirements(), list(deps.PIP))

    def test_pool_worker_ownership_uses_the_explicitly_selected_pool_release(self):
        # BUG: psycopg[pool]==3.3.6 не фиксирует отдельно выпускаемый psycopg_pool.
        # HYPOTHESIS: shutdown читает private _workers/_sched_runner, поэтому
        # новая установка должна получать именно проверенный pool, не latest.
        # SOLUTION: отдельный точный server-only requirement для psycopg-pool.
        # RESULT: наблюдался missing-pin red; packaging и реальный shutdown green.
        # STATUS: FIXED — see #632
        self.assertIn('psycopg-pool==3.3.3', deps.SERVER)
        self.assertFalse(any(item.lower().startswith(('psycopg-pool', 'psycopg_pool'))
                             for item in deps.PIP), 'client must not acquire the controller pool')

    def test_isolated_database_fixture_survives_hermetic_children(self):
        from unittest.mock import patch
        import os
        with patch.dict(os.environ, {'MOP_TEST_POSTGRES_HOST': 'isolated-db',
                                     'MOP_SERVER_LAN': 'must-not-survive'}):
            env = hermetic.child_env({}, strict=True)
        self.assertEqual(env.get('MOP_TEST_POSTGRES_HOST'), 'isolated-db')
        self.assertNotIn('MOP_SERVER_LAN', env)

    def test_global_server_selector_is_refused_without_old_context(self):
        from mop import cli
        from unittest.mock import patch
        with patch('mop.common.context.here', side_effect=AssertionError('old context read')), \
                patch.object(cli, 'environment', side_effect=AssertionError('old environment read')), \
                patch.object(cli, 'command') as command:
            self.assertEqual(cli.main(['--server', 'old.invalid', 'server', 'controller', 'check']), 1)
            command.assert_not_called()

    def test_hermetic_failure_keeps_the_actual_child_failure(self):
        from unittest.mock import Mock, patch
        class Collector:
            def __init__(self):
                self.messages = []
            def check(self, message, condition):
                self.messages.append(message)
        failure = 'AssertionError: exact failing invariant\n' + '\n'.join('trailer' for _ in range(12))
        # Каждый Popen — отдельный ребёнок, в том числе при concurrent drain (#673).
        def child(*args, **kwargs):
            process = Mock(returncode=1)
            process.communicate.return_value = (failure, None)
            return process
        collector = Collector()
        with patch.object(hermetic, 'hostile_home', return_value=('/fixture', {})), \
                patch.object(hermetic, 'hostile_repo', return_value='/fixture'), \
                patch.object(hermetic, 'files', return_value=['/fixture/tests/controller.py']), \
                patch.object(hermetic.subprocess, 'Popen', side_effect=child):
            hermetic.check_runs(collector)
        self.assertEqual(len(collector.messages), 2)
        self.assertTrue(all('exact failing invariant' in message for message in collector.messages))

    # HYPOTHESIS: setuptools берёт только .py; рабочий source Admin скрывает
    # отсутствие HTML templates после стандартной установки пакета.
    # SOLUTION: controller объявляет HTML templates в package-data.
    # RESULT: missing-pattern behavioral red, затем 7/7 packaging green.
    # STATUS: FIXED — see #638
    def test_admin_templates_are_declared_package_data(self):
        import tomllib
        config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        patterns = config['tool']['setuptools']['package-data'].get('mop.server.controller', [])
        self.assertIn('templates/admin/mop/*.html', patterns)
        self.assertTrue((ROOT / 'mop/server/controller/templates/admin/mop/form.html').is_file())

    def test_controller_dispatch_does_not_initialize_old_environment(self):
        from mop import cli
        from unittest.mock import patch
        self.assertTrue(cli.client_command('mop.cli.server.controller'))
        with patch.object(cli, 'environment', side_effect=AssertionError('old environment read')), \
                patch.object(cli, 'command', return_value=lambda argv: 0):
            with self.assertRaises(SystemExit) as result:
                cli.main(['server', 'controller', 'check'])
            self.assertEqual(result.exception.code, 0)

if __name__ == '__main__':
    unittest.main()
