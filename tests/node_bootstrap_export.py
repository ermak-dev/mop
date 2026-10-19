"""Чистые границы maintenance export #722, без БД/узла/старых файлов."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from _lib import run_command
import hermetic  # noqa: F401

# HYPOTHESIS: freshNode не имеет finite issuer/export, а старый server путь
# читает .env и не может быть новым источником transport credential.
# SPEC: новый owned private каталог, O_EXCL/no-follow, finite HTTPS transport,
# безопасная диагностика и early dispatch. Ни node parser, ни live delivery.
# SOLUTION: early maintenance dispatch, finite redacted transport и новый
# dirfd/no-follow bundle с точными modes; существующее никогда не переписываем.
# RESULT: RED — issuer/export отсутствовал; дополнительные RED нашли umask=0777
# и пустой userinfo. После исправлений все 9 pure checks зелёные.
# STATUS: FIXED — see #722
class NodeBootstrapFiles(unittest.TestCase):
    def api(self):
        name = 'mop.server.controller.nodebootstrap'
        self.assertIsNotNone(importlib.util.find_spec(name), 'private node bootstrap export is missing')
        return __import__(name, fromlist=['nodebootstrap'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_transport_is_finite_and_redacted(self):
        api = self.api()
        node = uuid4()
        value = api.transport(installation_id='fixture', origin='https://controller.example.invalid:8443',
            ca_file='/etc/mop/ca.pem', node_id=node, token='a' * 40)
        self.assertNotIn('a' * 40, repr(value))
        self.assertNotIn('a' * 40, str(value))
        self.assertEqual(dict(value), dict(schema_version=1, installation_id='fixture',
            origin='https://controller.example.invalid:8443', ca_file='/etc/mop/ca.pem',
            node_id=str(node), token='a' * 40))

    def test_transport_refuses_unsafe_origins_paths_and_raw_values_without_echo(self):
        api = self.api()
        valid = dict(installation_id='fixture', origin='https://controller.example.invalid',
                     ca_file='/etc/mop/ca.pem', node_id=uuid4(), token='a' * 40)
        invalid = [dict(origin=value) for value in (
            'http://controller.invalid', 'https://secret:secret@controller.invalid',
            'https://@controller.invalid', 'https://:@controller.invalid',
            'https://controller.invalid/', 'https://controller.invalid/path',
            'https://controller.invalid?secret', 'https://controller.invalid#secret',
            'https://controller.invalid:99999', 'https://controller.invalid\nsecret', None, [])]
        invalid += [dict(ca_file=value) for value in (
            'secret', '/etc/../secret', '/etc//secret', '/etc/./secret', '/secret/', '/', '/secret\x00')]
        invalid += [dict(token=value) for value in ('secret', None, 'x' * 40, 'a' * 39, 'a' * 40 + '\n')]
        invalid += [dict(installation_id='secret/value'), dict(node_id='secret')]
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(RuntimeError) as caught:
                    api.transport(**{**valid, **change})
                self.assertNotIn('secret', str(caught.exception))

    def test_new_folder_and_files_are_private_even_with_public_umask(self):
        api = self.api()
        path = self.root / 'new' / 'bootstrap.json'
        old = os.umask(0)
        try:
            with api.export_files(path, node_file='/var/lib/mop-node/bootstrap.json') as bundle:
                bundle.write(b'{"token":"private-value"}\n', node_id=uuid4(), enrollment_id=uuid4())
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        for item in (path, path.parent / 'delivery.json'):
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)
            self.assertEqual(item.stat().st_uid, os.geteuid())
        delivery = json.loads((path.parent / 'delivery.json').read_bytes())
        self.assertEqual(set(delivery), {'schema_version', 'node_id', 'controller_file', 'node_file',
                                         'mode', 'directory_mode', 'no_log', 'enrollment_id'})
        self.assertTrue(delivery['no_log'])
        self.assertEqual(delivery['controller_file'], str(path))
        self.assertEqual(delivery['node_file'], '/var/lib/mop-node/bootstrap.json')
        self.assertNotIn('private-value', repr(bundle))

    def test_exact_file_mode_does_not_depend_on_restrictive_umask(self):
        api = self.api()
        path = self.root / 'restricted' / 'bootstrap.json'
        old = os.umask(0o777)
        try:
            with api.export_files(path, node_file='/var/lib/node/bootstrap.json') as bundle:
                bundle.write(b'{}', node_id=uuid4(), enrollment_id=uuid4())
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((path.parent/'delivery.json').stat().st_mode), 0o600)

    def test_existing_directory_file_and_symlink_are_never_overwritten(self):
        api = self.api()
        directory = self.root / 'existing'
        directory.mkdir(mode=0o700)
        file = directory / 'bootstrap.json'
        file.write_bytes(b'untouched')
        targets = [file, self.root / 'alias' / 'bootstrap.json']
        (self.root / 'alias').symlink_to(directory)
        for target in targets:
            with self.assertRaises(RuntimeError):
                with api.export_files(target, node_file='/var/lib/node/bootstrap.json'):
                    self.fail('existing output must be refused before enrollment')
        self.assertEqual(file.read_bytes(), b'untouched')

    def test_parent_must_be_private_owned_and_without_symlinks(self):
        api = self.api()
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        (self.root / 'alias').symlink_to(self.root)
        for parent in (public, self.root / 'alias'):
            with self.assertRaises(RuntimeError):
                with api.export_files(parent / 'new' / 'bootstrap.json', node_file='/var/lib/node/bootstrap.json'):
                    self.fail('untrusted parent accepted')
        self.assertFalse((public / 'new').exists())

    def test_failure_removes_only_new_bundle_and_does_not_expose_raw_os_error(self):
        api = self.api()
        path = self.root / 'new' / 'bootstrap.json'
        with self.assertRaisesRegex(RuntimeError, 'fixture refusal'):
            with api.export_files(path, node_file='/var/lib/node/bootstrap.json') as bundle:
                bundle.write(b'private-value', node_id=uuid4(), enrollment_id=uuid4())
                raise RuntimeError('fixture refusal')
        self.assertFalse(path.parent.exists())
        with self.assertRaises(RuntimeError) as caught:
            with patch('os.mkdir', side_effect=OSError('private-value')):
                with api.export_files(path, node_file='/var/lib/node/bootstrap.json'):
                    self.fail('mkdir failure ignored')
        self.assertNotIn('private-value', str(caught.exception))

    def test_invalid_paths_refused_before_creating_any_directory(self):
        api = self.api()
        for path, destination in ((self.root/'new'/'delivery.json', '/var/lib/node/bootstrap.json'),
                                  (self.root/'new'/'bootstrap.json', 'relative'),
                                  (self.root/'new'/'bootstrap.json', '/var/../secret'),
                                  ('relative/new.json', '/var/lib/node/bootstrap.json')):
            with self.assertRaises(RuntimeError):
                with api.export_files(path, node_file=destination):
                    self.fail('invalid export coordinates accepted')
        self.assertFalse((self.root/'new').exists())

    def test_early_cli_dispatch_has_no_legacy_context_or_credentials_in_argv(self):
        from mop import cli
        from mop.cli.server import controller
        from mop.server.controller import runtime
        self.assertTrue(callable(getattr(runtime, 'enroll_node', None)), 'finite maintenance issuer is missing')
        # HYPOTHESIS: ручной capture нарушает retained tests_lib gate (#675).
        # SOLUTION: общий runner сохраняет dispatch, exit/refusal и secret checks.
        # RESULT: retained tests_lib RED -> GREEN, все 9 export checks GREEN.
        # STATUS: FIXED — see #675
        with patch('mop.common.context.strip_server', side_effect=AssertionError('legacy context read')), \
                patch.object(runtime, 'configure'), patch.object(runtime, 'close_pools') as close, \
                patch.object(runtime, 'enroll_node', return_value={'enrollment_id': str(uuid4())}) as issue:
            output, _, code = run_command(cli.main,
                ['server', 'controller', 'enroll-node', '--node', str(uuid4()),
                 '--origin', 'https://controller.example.invalid', '--ca-file', '/etc/mop/ca.pem',
                 '--file', str(self.root/'new'/'bootstrap.json'), '--node-file', '/var/lib/node/bootstrap.json'])
        self.assertEqual(code, 0)
        issue.assert_called_once()
        close.assert_called_once()
        self.assertNotIn('token', output)
        _, _, code = run_command(controller.main, ['enroll-node', '--token', 'private-value'])
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
