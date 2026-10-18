"""Потребитель private transport export #640, без пула и реальных credentials.

HYPOTHESIS: fresh node не имеет строгого transport reader и early HTTP entry;
старый agent открывает context/.env/NATS ещё до проверки явного bootstrap.
SPEC: exact #722, private bounded no-follow files, только проверенный TLS;
ни policy/epoch snapshot, ни старые stores не становятся authority.
SOLUTION: bounded dirfd reader, immutable redacted transport и ранний agent entry.
RESULT: RED нашёл missing consumer/legacy import; дополнительный RED показал
ошибочную смену input из-за atime. Reader и isolated dispatcher checks зелёные.
STATUS: FIXED — see #640
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401


TOKEN = 'a' * 40
NODE = str(uuid4())


class BootstrapConsumer(unittest.TestCase):
    def api(self):
        name = 'mop.node.bootstrap_transport'
        self.assertIsNotNone(importlib.util.find_spec(name), 'finite node bootstrap consumer is missing')
        return __import__(name, fromlist=['bootstrap_transport'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / 'bootstrap.json'
        self.ca = self.root / 'ca.pem'
        self.ca.write_bytes(b'owned CA bytes')
        self.ca.chmod(0o600)
        self.document = dict(schema_version=1, installation_id='fixture',
            origin='https://controller.example.invalid:8443', ca_file=str(self.ca), node_id=NODE, token=TOKEN)
        self.write()

    def write(self, value=None):
        self.file.write_text(json.dumps(self.document if value is None else value))
        self.file.chmod(0o600)

    def test_exact_export_contract_and_private_credential_repr(self):
        api = self.api()
        value = api.load(self.file)
        self.assertEqual(value.installation_id, 'fixture')
        self.assertEqual(value.node_id, NODE)
        self.assertEqual(value.origin, self.document['origin'])
        self.assertEqual(value.ca_file, self.ca)
        self.assertEqual(value.token, TOKEN)
        self.assertNotIn(TOKEN, repr(value))
        self.assertNotIn(TOKEN, str(value))
        self.assertFalse(hasattr(value, 'epoch'))
        self.assertFalse(hasattr(value, 'policy'))

    def test_exact_schema_types_uuid_tls_origin_and_token(self):
        api = self.api()
        invalid = [dict(schema_version=True), dict(schema_version=2), dict(installation_id='x/y'),
            dict(installation_id='x' * 65), dict(node_id=str(uuid4()).upper()),
            dict(node_id='00000000-0000-0000-0000-000000000000'), dict(node_id=None),
            dict(token='b' * 39), dict(token='B' * 40), dict(token=TOKEN+'\n'),
            dict(epoch=str(uuid4())), dict(policy={}), dict(actions=[])]
        invalid += [dict(origin=x) for x in ('http://host.invalid', 'https://@host.invalid',
            'https://user:secret@host.invalid', 'https://host.invalid/', 'https://host.invalid/path',
            'https://host.invalid?', 'https://host.invalid#', 'https://host.invalid:0',
            'https://host.invalid:', 'https://host.invalid:99999', 'https://host.invalid\\secret',
            'https://host.invalid\nsecret', [], None)]
        invalid += [dict(ca_file=x) for x in ('relative', '/tmp/../secret', '/tmp//secret',
            '/tmp/./secret', '/tmp/secret/', '/', '/tmp/secret\x00')]
        for change in invalid:
            with self.subTest(change=change):
                self.write({**self.document, **change})
                with self.assertRaises(api.BootstrapError) as caught:
                    api.load(self.file)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn('secret', str(caught.exception))
        for field in self.document:
            self.write({k: v for k, v in self.document.items() if k != field})
            with self.assertRaises(api.BootstrapError):
                api.load(self.file)

    def test_duplicate_nonfinite_invalid_utf8_and_bounded_json(self):
        api = self.api()
        for raw in (b'{}', b'[]', b'null', b'\xff', b'{"schema_version":NaN}',
                json.dumps(self.document).replace('"schema_version": 1',
                    '"schema_version": 1, "schema_version": 1').encode(),
                b' ' * (api.MAX_BOOTSTRAP_BYTES + 1), b'[' * 2000 + b']' * 2000):
            self.file.write_bytes(raw)
            with self.subTest(raw=raw[:40]), self.assertRaises(api.BootstrapError):
                api.load(self.file)

    def test_nonprivate_symlink_fifo_directory_hardlink_and_wrong_owner_refused(self):
        api = self.api()
        self.file.chmod(0o644)
        with self.assertRaises(api.BootstrapError):
            api.load(self.file)
        self.file.chmod(0o600)
        alias = self.root / 'alias'
        alias.symlink_to(self.file)
        with self.assertRaises(api.BootstrapError):
            api.load(alias)
        alias.unlink()
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(api.BootstrapError):
            api.load(alias / self.file.name)
        alias.unlink()
        os.link(self.file, alias)
        with self.assertRaises(api.BootstrapError):
            api.load(self.file)
        alias.unlink()
        for path in (self.root, self.root / 'fifo'):
            if path.name == 'fifo':
                os.mkfifo(path, 0o600)
            with self.assertRaises(api.BootstrapError):
                api.load(path)
        with patch('os.geteuid', return_value=os.geteuid()+1):
            with self.assertRaises(api.BootstrapError):
                api.load(self.file)
        self.root.chmod(0o755)
        with self.assertRaises(api.BootstrapError):
            api.load(self.file)

    def test_writable_nonsticky_ancestor_cannot_replace_private_bootstrap_directory(self):
        api = self.api()
        public = self.root / 'shared'
        public.mkdir(mode=0o777)
        public.chmod(0o777)
        private = public / 'private'
        private.mkdir(mode=0o700)
        file = private / 'bootstrap.json'
        file.write_text(json.dumps(self.document))
        file.chmod(0o600)
        with self.assertRaises(api.BootstrapError):
            api.load(file)
        # Sticky directory защищает owned child от чужого rename, как /tmp.
        public.chmod(0o1777)
        self.assertEqual(api.load(file).node_id, NODE)

    def test_ca_is_also_owned_private_bounded_and_non_symlink(self):
        api = self.api()
        self.ca.chmod(0o644)
        with self.assertRaises(api.BootstrapError):
            api.load(self.file)
        self.ca.chmod(0o600)
        self.ca.write_bytes(b' ' * (api.MAX_CA_BYTES + 1))
        with self.assertRaises(api.BootstrapError):
            api.load(self.file)
        self.ca.unlink()
        self.ca.symlink_to(self.file)
        with self.assertRaises(api.BootstrapError):
            api.load(self.file)

    def test_reader_ignores_atime_but_rejects_content_changes(self):
        api = self.api()
        original = os.read
        touched = False
        # atime может меняться только из-за нашего чтения; моделируем kernel,
        # не os.utime, которое меняет также ctime и правильно отвергается.
        fstat = os.fstat
        def shifted_atime(fd):
            from types import SimpleNamespace
            info = fstat(fd)
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            if touched:
                fields['st_atime'] += 1000
                fields['st_atime_ns'] += 10**12
            return SimpleNamespace(**fields)
        def read_marker(fd, size):
            nonlocal touched
            data = original(fd, size)
            touched = True
            return data
        with patch('os.fstat', side_effect=shifted_atime), patch('os.read', side_effect=read_marker):
            self.assertEqual(api.load(self.file).node_id, NODE)
        touched = False
        def read_changed(fd, size):
            nonlocal touched
            data = original(fd, size)
            if not touched:
                touched = True
                self.file.write_text('changed')
            return data
        with patch('os.read', side_effect=read_changed), self.assertRaises(api.BootstrapError):
            api.load(self.file)

    def test_missing_bootstrap_never_reads_old_stores(self):
        api = self.api()
        self.file.unlink()
        for name in ('.env', 'bus.json', 'ready.json', 'node.env', 'connection.json'):
            (self.root / name).write_text(json.dumps(self.document))
        with self.assertRaises(api.BootstrapError) as caught:
            api.load(self.file)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_early_agent_dispatch_never_imports_old_context_or_nats(self):
        script = '''
import importlib.abc, sys
class RejectOld(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in ('nats', 'mop.node.agent', 'mop.common.context', 'mop.common.config',
                        'mop.common.bus', 'mop.client.control', 'mop.cli._control'):
            raise AssertionError('legacy import: '+fullname)
sys.meta_path.insert(0, RejectOld())
from mop.cli import main
sys.exit(main(sys.argv[1:]))
'''
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MOP_', 'PU_', 'NOMAD_'))}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        for args in (['agent', '--bootstrap', str(self.root/'missing'), '--check'],
                     ['agent'], ['agent', '--bootstrap', str(self.file)]):
            result = subprocess.run([sys.executable, '-c', script, *args], env=env,
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('legacy import', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertNotIn(TOKEN, result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
