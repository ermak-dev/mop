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
