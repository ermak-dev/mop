"""Изоляция серверных зависимостей и холодного входа #632."""
import ast
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mop.common import deps

# HYPOTHESIS: установка клиента не умеет выбирать серверную группу;
# bootstrap командлета проходит через чтение старой .env.
# Проверки метаданных исполняются без Django и без сетевой установки.
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

    def test_controller_dispatch_does_not_initialize_old_environment(self):
        from mop import cli
        self.assertTrue(cli.client_command('mop.cli.server.controller'))

if __name__ == '__main__':
    unittest.main()
