"""Защищённый bootstrap нового контроллера, без Django и старых файлов."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _lib  # noqa: F401,E402 — lifetime временных файлов
import hermetic  # noqa: F401,E402 — настройки установки не являются входом
from mop.server.controller.bootstrap import load

# HYPOTHESIS: простой JSON reader принимает публичные credentials и лишние
# настройки; bootstrap должен отвергать их до первого подключения к БД.
# Инфраструктурный reader существует: red здесь — принятие опасного ввода,
# а не отсутствие Django или импортируемого модуля.
# SOLUTION: finite schema, private owned regular files и явные trust roots.
# RESULT: небезопасный/неполный bootstrap отказывает до подключения к БД.
# STATUS: FIXED — see #632
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.root = self.directory / 'root.key'
        self.root.write_bytes(b'k' * 64)
        self.root.chmod(0o600)
        self.ca = self.directory / 'fixture-ca.pem'
        self.ca.write_text('fixture-only-not-a-certificate')
        self.ca.chmod(0o644)
        self.path = self.directory / 'bootstrap.json'
        self.data = {
            'installation_id': 'foundation-test', 'role': 'maintenance',
            'root_key': str(self.root),
            'database': {'ENGINE': 'django.db.backends.postgresql',
                         'NAME': 'controller_test', 'HOST': 'db.example.invalid',
                         'PORT': '5432', 'USER': 'migration', 'PASSWORD': 'private-value',
                         'OPTIONS': {'sslmode': 'verify-full', 'sslrootcert': str(self.ca)}}}
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

    # HYPOTHESIS: credential runtime нельзя запустить отдельным restricted uid.
    # STATUS: FIXED — see #637; red — bootstrap process role is invalid.
    def test_credential_role_requires_protected_root_and_django_trust(self):
        self.data['role'] = 'credential'
        self.data['django_key'] = str(self.root)
        self.write()
        self.assertEqual(load(self.path)['role'], 'credential')
        for missing in ('root_key', 'django_key'):
            old = self.data.pop(missing)
            self.write()
            with self.assertRaises(RuntimeError):
                load(self.path)
            self.data[missing] = old

    def test_port_and_tls_values_are_validated_before_connect(self):
        self.data['database']['PORT'] = 'not-a-port'
        self.write()
        with self.assertRaisesRegex(RuntimeError, 'port'):
            load(self.path)
        self.data['database']['PORT'] = '5432'
        self.data['database']['OPTIONS']['sslrootcert'] = ['private-value']
        self.write()
        with self.assertRaisesRegex(RuntimeError, 'TLS'):
            load(self.path)

    def test_malformed_types_fail_with_safe_reason(self):
        original = dict(self.data)
        for key, value in (('role', ['private-value']), ('root_key', 42),
                           ('django_key', ['private-value'])):
            with self.subTest(key=key):
                self.data = dict(original, **{key: value})
                self.write()
                with self.assertRaises(RuntimeError) as error:
                    load(self.path)
                self.assertNotIn('private-value', str(error.exception))

    def test_database_policy_does_not_use_django_unbounded_defaults(self):
        import importlib
        from unittest.mock import patch
        self.data['django_key'] = str(self.root)
        self.write()
        with patch.dict(os.environ, {'MOP_CONTROLLER_BOOTSTRAP': str(self.path)}):
            sys.modules.pop('mop.server.controller.settings', None)
            settings = importlib.import_module('mop.server.controller.settings')
            db = settings.DATABASES['default']
            self.assertEqual(db.get('CONN_MAX_AGE'), 0)
            self.assertIs(db.get('ATOMIC_REQUESTS'), False)
            self.assertEqual(db['OPTIONS']['connect_timeout'], 5)
            self.assertLessEqual(db['OPTIONS']['pool']['max_size'], 8)
        sys.modules.pop('mop.server.controller.settings', None)

    def test_binary_django_signing_key_is_a_safe_bootstrap_refusal(self):
        key = self.directory / 'binary.key'
        key.write_bytes(b'\xff' * 64)
        key.chmod(0o600)
        self.data['django_key'] = str(key)
        self.write()
        with self.assertRaisesRegex(RuntimeError, 'Django signing'):
            load(self.path)

    # HYPOTHESIS: rootless endpoint не входит в finite bootstrap schema.
    # SPEC: только web/worker, canonical socket path + точный non-root UID;
    # никаких bearer/root/DB credentials в endpoint и никаких файловых чтений.
    # SOLUTION: общий finite validator у loader/renderer/factory; без socket I/O.
    # RESULT: RED отвергал valid binding; GREEN проверяет роли, UID, path и safe errors.
    # STATUS: FIXED — see #644.
    def test_web_and_worker_accept_only_nonsecret_protected_service_coordinates(self):
        self.data.pop('root_key')
        endpoint = {'path': str(self.directory / 'service.sock'), 'server_uid': 1944}
        for role in ('web', 'worker'):
            with self.subTest(role=role):
                self.data.update(role=role, credential_service=endpoint)
                self.write()
                self.assertEqual(load(self.path)['credential_service'], endpoint)

    def test_credential_endpoint_rejects_extra_fields_types_paths_and_privileged_uid(self):
        self.data.pop('root_key')
        self.data['role'] = 'worker'
        valid = {'path': '/var/lib/fresh/credential-socket/service.sock', 'server_uid': 1944}
        invalid = [None, [], {}, {**valid, 'token': 'private-value'},
                   {**valid, 'database': {}}, {**valid, 'root_key': 'private-value'}]
        invalid += [{**valid, 'server_uid': uid} for uid in (None, True, 0, -1, '1944', 2 ** 32 - 1)]
        invalid += [{**valid, 'path': path} for path in
                    (None, [], 'relative.sock', '/run/../service.sock', '/run//service.sock',
                     '/run/./service.sock', '/run/service.sock/', '/', '/run/' + 's' * 100,
                     '/run/\x00sock', '/run/' + chr(0xd800))]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                self.data['credential_service'] = endpoint
                self.write()
                with self.assertRaisesRegex(RuntimeError, 'credential service') as caught:
                    load(self.path)
                self.assertNotIn('private-value', str(caught.exception))

    def test_other_roles_cannot_acquire_a_credential_client_by_bootstrap(self):
        self.data['django_key'] = str(self.root)
        self.data['credential_service'] = {'path': '/run/credential/service.sock', 'server_uid': 1944}
        for role in ('gateway', 'maintenance', 'credential'):
            with self.subTest(role=role):
                self.data['role'] = role
                if role == 'gateway':
                    self.data.pop('root_key', None)
                else:
                    self.data['root_key'] = str(self.root)
                self.write()
                with self.assertRaisesRegex(RuntimeError, 'credential service.*role'):
                    load(self.path)

    def test_no_fallback_for_missing_file(self):
        with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
            load(self.directory / 'absent')

if __name__ == '__main__':
    unittest.main()
