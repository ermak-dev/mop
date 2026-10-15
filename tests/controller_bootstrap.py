"""Защищённый bootstrap нового контроллера, без Django и старых файлов."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mop.server.controller.bootstrap import load

# HYPOTHESIS: простой JSON reader принимает публичные credentials и лишние
# настройки; bootstrap должен отвергать их до первого подключения к БД.
# Инфраструктурный reader существует: red здесь — принятие опасного ввода,
# а не отсутствие Django или импортируемого модуля.
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.root = self.directory / 'root.key'
        self.root.write_bytes(b'k' * 64)
        self.root.chmod(0o600)
        self.path = self.directory / 'bootstrap.json'
        self.data = {
            'installation_id': 'foundation-test', 'role': 'maintenance',
            'root_key': str(self.root),
            'database': {'ENGINE': 'django.db.backends.postgresql',
                         'NAME': 'controller_test', 'HOST': 'db.example.invalid',
                         'PORT': '5432', 'USER': 'migration', 'PASSWORD': 'private-value',
                         'OPTIONS': {'sslmode': 'verify-full', 'sslrootcert': '/trust/ca.pem'}}}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def test_private_explicit_remote_database(self):
        result = load(self.path)
        self.assertEqual(result['database']['HOST'], 'db.example.invalid')
        self.assertNotIn('private-value', repr(result))

    def test_world_readable_bootstrap_refused(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            load(self.path)

    def test_symlink_refused(self):
        link = self.directory / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(RuntimeError, 'regular|symlink'):
            load(link)

    def test_public_parent_refused(self):
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            load(self.path)

    def test_unknown_keys_do_not_become_general_settings(self):
        self.data['MOP_NOMAD_TOKEN'] = 'private-value'
        self.write()
        with self.assertRaisesRegex(RuntimeError, 'keys'):
            load(self.path)

    def test_public_root_and_unsupported_backend_refused(self):
        self.root.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            load(self.path)
        self.root.chmod(0o600)
        self.data['database']['ENGINE'] = 'django.db.backends.sqlite3'
        self.write()
        with self.assertRaisesRegex(RuntimeError, 'PostgreSQL'):
            load(self.path)

    def test_runtime_does_not_receive_root_key(self):
        self.data['role'] = 'web'
        self.write()
        with self.assertRaisesRegex(RuntimeError, 'root'):
            load(self.path)

    def test_no_fallback_for_missing_file(self):
        with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
            load(self.directory / 'absent')

if __name__ == '__main__':
    unittest.main()
