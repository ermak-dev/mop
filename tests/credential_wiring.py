"""Installer → rootless factory → настоящий защищённый Unix socket, без БД."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401

from mop.common import credentials as p
from mop.common.credential_client import CredentialClient


# HYPOTHESIS: production factory всегда None, хотя транспорт уже реализован.
# SPEC: точные installer coordinates, timeout=5, нет root/provider/retained token;
# недоступный/чужой endpoint не объявляется готовой службой и не повторяет I/O.
# SOLUTION: factory использует validated bootstrap и bounded stdlib transport.
# RESULT: RED -- factory None; GREEN -- настоящий Unix socket/SO_PEERCRED,
# forwarded per-call auth, no retry/root read/provider import. Без live установки.
# STATUS: FIXED — see #644.
class CredentialWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='mop-644-factory-')
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        signing = base / 'django.key'
        signing.write_bytes(b's' * 64)
        signing.chmod(0o600)
        boot = base / 'bootstrap.json'
        boot.write_text(json.dumps(dict(installation_id='wiring-fixture', role='web',
            django_key=str(signing), database=dict(ENGINE='django.db.backends.postgresql',
                NAME='not_connected', HOST='db.example.invalid', PORT='5432', USER='web',
                PASSWORD='fixture-db-password', OPTIONS={'sslmode': 'disable'}))))
        boot.chmod(0o600)
        with patch.dict(os.environ, {'MOP_CONTROLLER_BOOTSTRAP': str(boot)}):
            from mop.server.controller.runtime import configure
            configure()
        from mop.server.controller import adminservices
        cls.services = adminservices

    def bootstrap(self, *, role='web', path=None, uid=None):
        return {'role': role, 'credential_service': {
            'path': path or '/var/lib/fresh/credential-socket/service.sock',
            'server_uid': os.geteuid() if uid is None else uid}}

    def test_factory_constructs_exact_bounded_rootless_client_without_reading_custody(self):
        from django.test import override_settings
        from mop.server.controller import bootstrap
        real_import = builtins.__import__
        def guarded(name, globals=None, locals=None, fromlist=(), level=0):
            resolved = importlib.util.resolve_name('.' * level + name, globals['__package__']) if level else name
            names = (resolved, *(resolved + '.' + part for part in fromlist or ()))
            for candidate in names:
                if candidate in {'mop.server.controller.credentials', 'mop.server.controller.endpointroot',
                                  'mop.server.secrets.crypto'} or candidate.startswith('nacl'):
                    raise AssertionError('rootless factory imported a crypto provider')
            return real_import(name, globals, locals, fromlist, level)
        for role in ('web', 'worker'):
            data = self.bootstrap(role=role, uid=1944)
            with self.subTest(role=role), override_settings(BOOTSTRAP=data), \
                 patch.object(bootstrap, 'private_bytes', side_effect=AssertionError('root read')), \
                 patch.object(bootstrap, 'secret_provider', side_effect=AssertionError('provider construction')), \
                 patch('builtins.__import__', side_effect=guarded):
                client = self.services.credential_client()
                self.assertIsInstance(client, CredentialClient, 'production credential factory is unbound')
                self.assertEqual(str(client.path), data['credential_service']['path'])
                self.assertEqual(client.server_uid, 1944)
                self.assertEqual(client.timeout, 5)
                self.assertEqual(set(vars(client)), {'path', 'server_uid', 'timeout'})

    def test_missing_binding_is_unavailable_and_other_roles_cannot_use_it(self):
        from django.test import override_settings
        with override_settings(BOOTSTRAP={'role': 'web'}):
            self.assertIsNone(self.services.credential_client())
        for role in ('maintenance', 'credential', 'gateway'):
            with self.subTest(role=role), override_settings(BOOTSTRAP=self.bootstrap(role=role)):
                with self.assertRaisesRegex(RuntimeError, 'credential service.*role'):
                    self.services.credential_client()

    def test_factory_does_not_cache_a_previous_installation_endpoint(self):
        from django.test import override_settings
        for number in (1944, 1945):
            data = self.bootstrap(uid=number, path=f'/run/credential-{number}/service.sock')
            with override_settings(BOOTSTRAP=data):
                client = self.services.credential_client()
                self.assertIsNotNone(client, 'production credential factory is unbound')
                self.assertEqual(client.server_uid, number)
                self.assertEqual(str(client.path), data['credential_service']['path'])

    def test_generated_factory_uses_real_protected_socket_and_current_auth(self):
        from django.test import override_settings
        self.assertNotEqual(os.geteuid(), 0, 'rootless socket proof needs an ordinary user')
        with tempfile.TemporaryDirectory(prefix='mop644-sock-') as directory:
            path = str(Path(directory) / 'service.sock')
            seen, errors = [], []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                os.chmod(path, 0o600)
                server.listen(1)
                server.settimeout(5)
                def respond():
                    try:
                        with server.accept()[0] as peer:
                            peer.settimeout(5)
                            def receive(size):
                                value = bytearray()
                                while len(value) < size:
                                    chunk = peer.recv(size - len(value))
                                    if not chunk:
                                        raise AssertionError('truncated credential frame')
                                    value.extend(chunk)
                                return bytes(value)
                            seen.append(p.loads(receive(p.frame_size(receive(4)))))
                            peer.sendall(p.frame({'ok': False, 'error': {
                                'code': 'refused', 'message': 'fixture scope refused'}}))
                    except BaseException as error:
                        errors.append(error)
                thread = threading.Thread(target=respond)
                thread.start()
                try:
                    with override_settings(BOOTSTRAP=self.bootstrap(path=path)):
                        client = self.services.credential_client()
                        self.assertIsNotNone(client, 'production credential factory is unbound')
                        with self.assertRaisesRegex(p.CredentialRefused, 'fixture scope refused'):
                            client.issue_addressed(p.Credential('token', 'a' * 40), target_id=uuid4())
                finally:
                    thread.join(timeout=6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0]['auth'], {'kind': 'token', 'value': 'a' * 40})
                self.assertEqual(seen[0]['command'], 'issue_addressed')

    def test_missing_socket_or_wrong_service_uid_fails_closed(self):
        from django.test import override_settings
        with tempfile.TemporaryDirectory(prefix='mop644-sock-') as directory:
            path = str(Path(directory) / 'service.sock')
            for uid in (os.geteuid(), os.geteuid() + 1):
                with self.subTest(uid=uid), override_settings(BOOTSTRAP=self.bootstrap(path=path, uid=uid)):
                    client = self.services.credential_client()
                    self.assertIsNotNone(client, 'production credential factory is unbound')
                    with self.assertRaises(p.CredentialError):
                        client.issue_addressed(p.Credential('token', 'a' * 40), target_id=uuid4())


if __name__ == '__main__':
    unittest.main()
