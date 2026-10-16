"""Клиент credential socket не зависит от Django/серверного пакета.

HYPOTHESIS: rootless frontend вместо use cases импортирует provider либо
позволяет передать context/ciphertext и произвольную команду.
SOLUTION: stdlib-only typed adapter и конечный wire schema; один absolute deadline.
RESULT: red — missing client, legacy bootstrap reached, send сбрасывал deadline;
после исправления все пять checks green. Недоверенные типы auth дают безопасный отказ.
STATUS: FIXED — see #637.

HYPOTHESIS #669: transport в client-слое недоступен server; rootless callers
не могут invoke конечные bundle/preparation/grant use cases, provenance течёт в repr.
SOLUTION: common stdlib transport, конечные typed inputs/receipts и private repr.
RESULT: наблюдались missing common transport, unsupported commands, accepted null
project и private provenance в repr; после исправления десять checks green.
STATUS: FIXED — see #669 (transport contract, не full production composition).
"""
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest

import hermetic  # noqa: F401
from _lib import offline


class ClientBoundary(unittest.TestCase):
    # HYPOTHESIS: server не может вызывать transport из client-слоя.
    # SOLUTION: stdlib transport на common-границе; unreleased старый импорт удалён.
    # RESULT: common transport отсутствовал; после переноса шесть checks green.
    # STATUS: FIXED — see #669.
    def test_transport_is_available_to_all_layers_without_client_import(self):
        path = Path(__file__).resolve().parents[1] / 'mop/common/credential_client.py'
        self.assertTrue(path.is_file(), 'server-compatible common credential transport is missing')
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or '').startswith(('mop.client', 'mop.server', 'django', 'nacl')))
            elif isinstance(node, ast.Import):
                self.assertFalse(any(a.name.startswith(('mop.client', 'mop.server', 'django', 'nacl')) for a in node.names))

    def test_stdlib_only_client_does_not_import_server_or_framework(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('mop/common/credential_client.py', 'mop/common/credentials.py'):
            path = root / name
            self.assertTrue(path.exists(), 'rootless credential client protocol is missing')
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith(('django', 'mop.server', 'nacl', 'channels')))
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name.startswith(('django', 'mop.server', 'nacl', 'channels')) for a in node.names))

    def test_publish_bundle_protocol_accepts_only_exact_domain_inputs(self):
        from mop.common import credentials as p
        from uuid import uuid4
        request = {'version': 1, 'auth': {'kind': 'token', 'value': 'a' * 40},
            'command': 'publish_bundle', 'args': {'project_id': str(uuid4()),
                'manifest': p.encoded(b'{}'), 'prompt': p.encoded(b'private prompt'), 'provenance': 'fixture'}}
        auth, command, args = p.parse_request(request)
        self.assertEqual(command.value, 'publish_bundle')
        self.assertEqual(args['prompt'], b'private prompt')
        for name, value in (('owner_id', 1), ('ciphertext', 'bad'), ('context', 'bad')):
            with self.subTest(name=name), self.assertRaises(p.CredentialError):
                p.parse_request({**request, 'args': {**request['args'], name: value}})

    def test_preparation_and_grant_commands_have_finite_owner_owned_inputs(self):
        from mop.common import credentials as p
        from uuid import uuid4
        identity = str(uuid4())
        shapes = {
            'publish_preparation': {'project_id': identity, 'tasks': p.encoded(b'tasks'),
                'variables': p.encoded(b'vars'), 'provenance': 'fixture', 'commit': 'a' * 40, 'secret_refs': []},
            'read_preparation': {'revision_id': identity},
            'approve_preparation': {'revision_id': identity},
            'approve_bundle': {'bundle_id': identity},
            'reserve_grant': {'project_id': identity, 'bundle_id': identity, 'target_id': identity,
                'incarnation': identity, 'request_id': identity, 'expires_at': '2026-10-16T12:00:00+00:00'},
            'provision_grant': {'grant_id': identity, 'expected_revision': 1}}
        for command, args in shapes.items():
            request = {'version': 1, 'auth': {'kind': 'token', 'value': 'a' * 40}, 'command': command, 'args': args}
            with self.subTest(command=command):
                _, parsed, _ = p.parse_request(request)
                self.assertEqual(parsed.value, command)
                for name in ('owner_id', 'actor_id', 'ciphertext', 'context'):
                    with self.assertRaises(p.CredentialError):
                        p.parse_request({**request, 'args': {**args, name: identity}})

    def test_publish_requires_concrete_project_and_bounded_reference_list(self):
        from mop.common import credentials as p
        from uuid import uuid4
        request = {'version': 1, 'auth': {'kind': 'token', 'value': 'a' * 40}, 'command': 'publish_bundle',
            'args': {'project_id': None, 'manifest': p.encoded(b'{}'), 'prompt': p.encoded(b'p'), 'provenance': 'fixture'}}
        with self.assertRaises(p.CredentialError):
            p.parse_request(request)
        request = {**request, 'command': 'publish_preparation', 'args': {'project_id': str(uuid4()),
            'tasks': p.encoded(b''), 'variables': p.encoded(b''), 'provenance': 'fixture',
            'commit': 'a' * 40, 'secret_refs': []}}
        for refs in ('not a list', [str(uuid4())] * 129, [None], [str(uuid4())] * 2):
            with self.subTest(refs=repr(refs)[:50]), self.assertRaises(p.CredentialError):
                p.parse_request({**request, 'args': {**request['args'], 'secret_refs': refs}})

    # HYPOTHESIS: изолированные subject suites скрывают одинаковые DB constraint names.
    # SOLUTION: execution receipt получает собственное имя в модели и initial schema.
    # RESULT: actual PG16 models.E032 для receipt_known_outcome до механического fix;
    # после переименования schema check пропускает реальные behavioral cases.
    # STATUS: FIXED — see #669.
    def test_composed_domain_constraint_names_are_globally_unique(self):
        root = Path(__file__).resolve().parents[1]
        names = {}
        for path in (root / 'mop/server').glob('*/models.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in ('CheckConstraint', 'UniqueConstraint'):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == 'name' and isinstance(keyword.value, ast.Constant):
                        name = keyword.value.value
                        if type(name) is str and '%' not in name:
                            self.assertFalse(name in names, f'composed constraint collision: {name}')
                            names[name] = path

    def test_private_bundle_provenance_and_payload_never_enter_receipt_repr(self):
        from mop.common import credentials as p
        from dataclasses import FrozenInstanceError
        from uuid import uuid4
        value = p.BundleBytes(uuid4(), 'worker', 'a' * 64, b'private manifest',
            b'private prompt', 'private provenance')
        for marker in ('private manifest', 'private prompt', 'private provenance'):
            self.assertNotIn(marker, repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.digest = 'b' * 64

    def test_private_auth_repr_and_no_crypto_or_raw_rpc_api(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.common.credentials'), 'typed socket protocol is missing')
        from mop.common.credentials import Credential, CredentialError
        from mop.common.credential_client import CredentialClient
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
        from mop.common import credential_client as client
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
        self.assertIsNotNone(importlib.util.find_spec('mop.common.credential_client'), 'credential client is missing')
        from mop.common.credential_client import CredentialClient
        from mop.common.credentials import CredentialError
        for path in ('relative.sock', '/tmp/../bad.sock', ''):
            with self.assertRaises(CredentialError):
                CredentialClient(path, server_uid=0)


if __name__ == '__main__':
    with offline():
        unittest.main()
