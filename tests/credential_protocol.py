"""Клиент credential socket не зависит от Django/серверного пакета.

HYPOTHESIS: rootless frontend вместо use cases импортирует provider либо
позволяет передать context/ciphertext и произвольную команду.
SOLUTION: stdlib-only typed adapter и конечный wire schema; один absolute deadline.
RESULT: red — missing client, legacy bootstrap reached, send сбрасывал deadline;
после исправления все пять checks green. Недоверенные типы auth дают безопасный отказ.
STATUS: FIXED — see #637.
"""
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest

import hermetic  # noqa: F401
from _lib import offline


class ClientBoundary(unittest.TestCase):
    def test_stdlib_only_client_does_not_import_server_or_framework(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('mop/client/credentials.py', 'mop/common/credentials.py'):
            path = root / name
            self.assertTrue(path.exists(), 'rootless credential client protocol is missing')
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith(('django', 'mop.server', 'nacl', 'channels')))
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name.startswith(('django', 'mop.server', 'nacl', 'channels')) for a in node.names))

    def test_private_auth_repr_and_no_crypto_or_raw_rpc_api(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.common.credentials'), 'typed socket protocol is missing')
        from mop.common.credentials import Credential, CredentialError
        from mop.client.credentials import CredentialClient
        auth = Credential('token', 'a' * 40)
        self.assertNotIn('a' * 40, repr(auth))
        for name in ('decrypt', 'encrypt', 'call', 'request', 'export_root', 'crud'):
            self.assertFalse(hasattr(CredentialClient, name), name)
        for kind, value in (('bearer', 'a' * 40), ('token', 'invalid'), ('session', ''), ('token', 1), ([], 'a' * 40)):
            with self.assertRaises(CredentialError):
                Credential(kind, value)

    def test_credential_command_bypasses_legacy_environment(self):
        from mop import cli
        from unittest.mock import patch
        called = []
        with patch.object(cli, 'environment', side_effect=AssertionError('legacy bootstrap reached')), \
                patch.object(cli, 'command', return_value=lambda argv: called.append(argv) or 0):
            with self.assertRaises(SystemExit) as stopped:
                cli.main(['server', 'credentials', '--help'])
            self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(called, [['--help']])

    def test_client_send_and_receive_share_one_absolute_deadline(self):
        import os
        import stat
        from types import SimpleNamespace
        from unittest.mock import patch
        from mop.common import credentials as protocol
        from mop.client import credentials as client
        clock = [0]
        response = bytearray(protocol.frame({'ok': True, 'result': []}))
        class Socket:
            def __enter__(inner): return inner
            def __exit__(inner, *args): return False
            def settimeout(inner, value): inner.timeout = value
            def connect(inner, path): clock[0] = 4
            def sendall(inner, value):
                self.assertLessEqual(inner.timeout, 1, 'send reset the absolute request deadline')
            def recv(inner, size):
                part = bytes(response[:size])
                del response[:size]
                return part
        with tempfile.TemporaryDirectory() as directory:
            fd = os.open(directory, os.O_RDONLY)
            with patch.object(protocol, 'directory_fd', return_value=fd), \
                    patch.object(protocol, 'peer_uid', return_value=os.geteuid()), \
                    patch.object(client.os, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.geteuid())), \
                    patch.object(client.socket, 'socket', return_value=Socket()), \
                    patch.object(client.time, 'monotonic', side_effect=lambda: clock[0]):
                adapter = client.CredentialClient(directory + '/service.sock', server_uid=os.geteuid())
                self.assertEqual(adapter.issue_addressed(protocol.Credential('token', 'a' * 40)), ())

    def test_socket_parent_and_symlink_are_not_ambient_discovery(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.client.credentials'), 'credential client is missing')
        from mop.client.credentials import CredentialClient
        from mop.common.credentials import CredentialError
        for path in ('relative.sock', '/tmp/../bad.sock', ''):
            with self.assertRaises(CredentialError):
                CredentialClient(path, server_uid=0)


if __name__ == '__main__':
    with offline():
        unittest.main()
