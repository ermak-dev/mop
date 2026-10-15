"""Пакет клиента в чистом venv, не в окружении серверных тестов."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
import _lib  # noqa: F401,E402 — lifetime временных файлов
import hermetic  # noqa: E402 — отдельная CI fixture, не настройки установки

class ClientInstall(unittest.TestCase):
    def test_client_wheel_does_not_install_server_dependencies(self):
        if not hermetic.FIXTURE_ENV.get('MOP_TEST_POSTGRES_HOST'):
            hermetic.skip('clean client install', 'isolated CI fixture is required')
            self.skipTest('isolated install gate is not configured')
        with tempfile.TemporaryDirectory(prefix='mop-package-') as directory:
            venv = Path(directory) / 'venv'
            subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
            python = str(venv / 'bin/python')
            subprocess.run([python, '-m', 'pip', 'install', '--disable-pip-version-check',
                            str(ROOT)], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            code = '''
import importlib.util
import importlib.metadata
import mop.cli
for module in ('django', 'channels', 'rest_framework', 'psycopg'):
    assert importlib.util.find_spec(module) is None, module
assert mop.cli.console
assert 'server' in importlib.metadata.metadata('mop').get_all('Provides-Extra')
'''
            subprocess.run([python, '-I', '-c', code], cwd=directory, check=True)
            result = subprocess.run([str(venv / 'bin/mop'), 'help'], cwd=directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == '__main__':
    unittest.main()
