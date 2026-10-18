#!/usr/bin/env python3
"""HYPOTHESIS: HOST не имеет bounded private transport approved phase bytes.
SPEC: typed Admission + exact initial delivery -> private derived input ->
root-only immutable copy -> strict body reader; hash не machine authority.
SOLUTION: отдельный transport port, без native bindings и privileged exec.
RESULT: 14 RED для отсутствующего transport, затем RED bounded retention и
safe diagnostics (probe, delivery и duplicate JSON); 19 pure/filesystem checks GREEN.
Actual Python 3.12 UID probe: 19 checks GREEN после отдельных RED large reader,
mutable receipt, failed cleanup, stale release и world-readable foreign quarantine.
BUG/HYPOTHESIS: доступный body directory inode не может быть private root lock.
SOLUTION: fixed owner-private lock inode, pinned lifetime, per-call open description.
RESULT: private lock contract RED, затем 21 checks GREEN; max_files=1 сохраняет
один data slot, unknown entries и replacement не исключаются из safety checks.
Runner/backend не проверены.
STATUS: FIXED — see #746
"""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
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
from _host_inputs_data import selected
from mop.node.http_execution import Refused


def api():
    assert importlib.util.find_spec('mop.node.host_inputs') is not None, \
        'bounded private phase-input transport is missing (#746)'
    return importlib.import_module('mop.node.host_inputs')


def files():
    api()
    assert importlib.util.find_spec('mop.node.host_input_files') is not None, \
        'pinned private phase-input store is missing (#746)'
    return importlib.import_module('mop.node.host_input_files')


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.m = api()
        self.a, self.s, self.value = selected()

    def pack(self, a=None, s=None, value=None):
        return self.m.pack(a or self.a, s or self.s, self.value if value is None else value)

    def parse(self, raw, descriptor=None, admission=None):
        return self.m.Envelope.parse(raw, descriptor or self.pack().descriptor,
            admission or self.a, backend_epoch=self.s.epoch, node_id=self.s.node_id,
            executor_epoch=self.s.executor_epoch)

    def test_exact_selected_payload_no_ambient_environment_or_client_serialization(self):
        with patch.dict(os.environ, NODE_BEARER='ambient-secret-not-approved'):
            envelope = self.pack()
        self.assertNotIn(b'ambient-secret-not-approved', envelope.raw)
        self.assertEqual(envelope.payload, self.m.canonical_json(self.value))
        self.assertEqual(envelope.descriptor.sha256, hashlib.sha256(envelope.raw).hexdigest())
        self.assertEqual(envelope.descriptor.scope, self.s)
        self.assertEqual(envelope.descriptor.attempt_id, self.a.attempt_id)
        self.assertNotIn('approved-phase-secret', repr(envelope))
        self.assertNotIn(self.value['consumers'][0]['value'], repr(envelope))
        self.assertNotIn('payload', envelope.descriptor.wire())
        self.assertEqual(self.m.Descriptor.parse(envelope.descriptor.wire()), envelope.descriptor)
        self.assertLessEqual(len(envelope.descriptor.filename), 255)
        self.assertNotIn('/', envelope.descriptor.filename)
        self.assertIn(self.a.attempt_id, envelope.descriptor.filename)

    def test_node_writer_requires_typed_admission_strict_delivery_and_finite_purpose(self):
        for a in (None, self.a.work, self.a.__dict__, replace(self.a, revision=True),
                  replace(self.a, request_id='arbitrary-path'), replace(self.a, lease_until=self.a.work.not_after+timedelta(seconds=1))):
            with self.subTest(a=type(a)), self.assertRaises(Refused):
                self.m.pack(a, self.s, self.value)
        for mutate in (lambda v: v.update(env={'TOKEN': 'unapproved'}),
                       lambda v: v.update(phase='producer_credentials'),
                       lambda v: v['work'].update(generation='2'),
                       lambda v: v['consumers'][0].update(value='not base64!'),
                       lambda v: v['consumers'][0].update(destination='/root/secret')):
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.assertRaises(Refused):
                self.pack(value=value)
        for action in ('confirm_ready', 'send_message', 'scheduler_register'):
            a = replace(self.a, work=replace(self.a.work, action=action))
            value = {**self.value, 'work': a.work.wire(), 'phase': action}
            with self.assertRaises(Refused):
                self.pack(a=a, value=value)

    def test_safe_delivery_refusal_reason_survives_without_reflecting_raw_values(self):
        value = {**self.value, 'consumers': []}
        with self.assertRaisesRegex(Refused, 'phase consumer delivery is incomplete'):
            self.pack(value=value)

    def test_scope_work_attempt_and_trusted_backend_are_all_pinned(self):
        envelope = self.pack()
        for name in self.s.__dataclass_fields__:
            with self.subTest(name=name), self.assertRaises(Refused):
                self.pack(s=replace(self.s, **{name: str(uuid4())}))
        for a in (replace(self.a, attempt_id=str(uuid4())), replace(self.a, revision=2),
                  replace(self.a, request_id=str(uuid4())),
                  replace(self.a, lease_until=self.a.lease_until-timedelta(seconds=1))):
            with self.assertRaises(Refused):
                self.parse(envelope.raw, admission=a)
        for key in ('backend_epoch', 'node_id', 'executor_epoch'):
            expected = dict(backend_epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch)
            expected[key] = str(uuid4())
            with self.assertRaises(Refused):
                self.m.Envelope.parse(envelope.raw, envelope.descriptor, self.a, **expected)
        for descriptor in (replace(envelope.descriptor, attempt_id=str(uuid4())),
                           replace(envelope.descriptor, sha256='0'*64)):
            with self.assertRaises(Refused):
                self.parse(envelope.raw, descriptor)

    def test_expired_lease_and_work_refused_even_with_same_bytes(self):
        envelope = self.pack()
        expired = datetime.now(timezone.utc)-timedelta(seconds=1)
        for a in (replace(self.a, lease_until=expired), replace(self.a, work=replace(self.a.work, not_after=expired))):
            with self.assertRaises(Refused):
                self.pack(a=a)
            with self.assertRaises(Refused):
                self.parse(envelope.raw, admission=a)

    def test_root_outer_checks_do_not_parse_or_execute_inner_payload(self):
        envelope = self.pack()
        outer = json.loads(envelope.raw)
        self.assertEqual(type(outer['payload']), str, 'root must not decode nested delivery/YAML')
        with patch('mop.common.initialcreate.parse_delivery', side_effect=AssertionError('root parsed project data')):
            parsed = self.parse(envelope.raw, envelope.descriptor)
        self.assertEqual(parsed.payload, envelope.payload)
        outer['payload'] = 'this is deliberately not JSON or YAML'
        outer['payload_digest'] = hashlib.sha256(outer['payload'].encode()).hexdigest()
        raw = self.m.canonical_json(outer)
        descriptor = replace(envelope.descriptor, sha256=hashlib.sha256(raw).hexdigest())
        parsed = self.parse(raw, descriptor)
        with self.assertRaises(Refused):
            self.m.parse_body(parsed)

    def test_finite_outer_json_and_descriptor_never_reflect_untrusted_secret(self):
        envelope = self.pack()
        outer = json.loads(envelope.raw)
        for changes in ({'backend_authority': 'forged'}, {'purpose': 'post_active'},
                        {'schema_version': True}, {'payload_digest': '0'*64},
                        {'payload': {'env': 'raw-secret-sentinel'}}):
            raw = self.m.canonical_json({**outer, **changes})
            descriptor = replace(envelope.descriptor, sha256=hashlib.sha256(raw).hexdigest())
            with self.assertRaises(Refused) as caught:
                self.parse(raw, descriptor)
            self.assertNotIn('raw-secret-sentinel', str(caught.exception))
        for raw in (b'[]', b'\xff', b'{"a":1,"a":1}', b'{"a":NaN}', b'['*2000):
            descriptor = replace(envelope.descriptor, sha256=hashlib.sha256(raw).hexdigest())
            with self.assertRaises(Refused):
                self.parse(raw, descriptor)
        for changes in ({'path': '/private/secret'}, {'sha256': '../secret'}, {'attempt_id': '../secret'}):
            with self.assertRaises(Refused):
                self.m.Descriptor.parse({**envelope.descriptor.wire(), **changes})

    def test_outer_duplicate_field_refusal_keeps_safe_parser_reason(self):
        envelope = self.pack()
        raw = b'{"schema_version":1,"schema_version":1}'
        descriptor = replace(envelope.descriptor, sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(Refused, 'duplicate HOST JSON field'):
            self.parse(raw, descriptor)

    def test_body_reparses_actual_delivery_and_preserves_exact_bytes(self):
        envelope = self.pack()
        body = self.m.parse_body(self.parse(envelope.raw))
        self.assertEqual(body.value, self.value)
        self.assertEqual(body.payload, envelope.payload)
        self.assertNotIn(self.value['consumers'][0]['value'], repr(body))
        outer = json.loads(envelope.raw)
        bad = {**self.value, 'env': {'TOKEN': 'raw-secret-sentinel'}}
        outer['payload'] = self.m.canonical_json(bad).decode()
        outer['payload_digest'] = hashlib.sha256(outer['payload'].encode()).hexdigest()
        raw = self.m.canonical_json(outer)
        parsed = self.parse(raw, replace(envelope.descriptor, sha256=hashlib.sha256(raw).hexdigest()))
        with self.assertRaises(Refused) as caught:
            self.m.parse_body(parsed)
        self.assertNotIn('raw-secret-sentinel', str(caught.exception))


LOCK = '.phase-input.lock'


def data_entries(directory):
    return [entry for entry in directory.iterdir() if entry.name != LOCK]


class NodeWriterTests(unittest.TestCase):
    def setUp(self):
        self.m, self.f = api(), files()
        self.tmp = tempfile.TemporaryDirectory(prefix='mop-phase-input-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'ingress'
        self.path.mkdir(mode=0o700)
        self.a, self.s, self.value = selected()
        self.writer = self.f.NodeWriter(self.path, node_uid=os.geteuid())
        self.addCleanup(self.writer.close)

    def write(self, a=None, s=None, value=None):
        return self.writer.write(a or self.a, s or self.s, self.value if value is None else value)

    def test_node_creates_private_exclusive_derived_file_and_same_repeat_is_read_only(self):
        descriptor = self.write()
        path = self.path/descriptor.filename
        before = path.stat()
        self.assertEqual(before.st_mode & 0o777, 0o600)
        self.assertEqual(before.st_uid, os.geteuid())
        self.assertEqual(path.read_bytes(), self.m.pack(self.a, self.s, self.value).raw)
        self.assertEqual(self.write(), descriptor)
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_ino, after.st_mtime_ns, after.st_ctime_ns))
        changed = {**self.value, 'git_name': 'Different'}
        with self.assertRaises(Refused):
            self.write(value=changed)
        other = self.f.NodeWriter(self.path, node_uid=os.geteuid())
        self.addCleanup(other.close)
        with self.assertRaisesRegex(Refused, 'unknown|exists|owned'):
            other.write(self.a, self.s, self.value)

    def test_same_bytes_on_replacement_inode_never_adopted_or_overwritten(self):
        descriptor = self.write()
        path = self.path/descriptor.filename
        raw = path.read_bytes()
        path.rename(self.path/'retained-old')
        path.write_bytes(raw)
        path.chmod(0o600)
        inode = path.stat().st_ino
        with self.assertRaises(Refused):
            self.write()
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.read_bytes(), raw)

    def test_symlink_hardlink_fifo_loose_mode_and_private_ancestor_refused(self):
        descriptor = self.m.pack(self.a, self.s, self.value).descriptor
        path = self.path/descriptor.filename
        foreign = self.path/'foreign'
        foreign.write_bytes(b'raw-secret-sentinel')
        for kind in ('symlink', 'hardlink', 'fifo', 'mode'):
            if kind == 'symlink': path.symlink_to(foreign)
            elif kind == 'hardlink': os.link(foreign, path)
            elif kind == 'fifo': os.mkfifo(path, 0o600)
            else:
                path.write_bytes(self.m.pack(self.a, self.s, self.value).raw)
                path.chmod(0o644)
            with self.subTest(kind=kind), self.assertRaises(Refused):
                self.write()
            path.unlink()
        alias = self.path.parent/'alias'
        alias.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(Refused):
            self.f.NodeWriter(alias, node_uid=os.geteuid())
        self.path.chmod(0o755)
        with self.assertRaises(Refused):
            self.write()

    def test_pinned_parent_replacement_and_in_place_read_race_refused(self):
        descriptor = self.write()
        self.path.rename(self.path.parent/'old')
        self.path.mkdir(mode=0o700)
        with self.assertRaises(Refused):
            self.write()
        self.path.rmdir()
        (self.path.parent/'old').rename(self.path)
        real_read = os.read
        changed = False
        def racing(fd, count):
            nonlocal changed
            raw = real_read(fd, count)
            if not changed:
                changed = True
                with open(self.path/descriptor.filename, 'ab') as output:
                    output.write(b' ')
            return raw
        with patch('os.read', side_effect=racing), self.assertRaises(Refused):
            self.write()

    def test_file_byte_caps_and_interprocess_lock_are_not_bypassed(self):
        raw = self.m.pack(self.a, self.s, self.value).raw
        limited = self.f.NodeWriter(self.path, node_uid=os.geteuid(),
            limits=self.m.Limits(max_files=1, max_bytes=len(raw), max_file_bytes=len(raw)))
        self.addCleanup(limited.close)
        limited.write(self.a, self.s, self.value)
        a, s, value = selected()
        with self.assertRaisesRegex(Refused, 'capacity|bound'):
            limited.write(a, s, value)
        too_small = self.f.NodeWriter(self.path, node_uid=os.geteuid(),
            limits=self.m.Limits(max_files=2, max_bytes=len(raw), max_file_bytes=len(raw)-1))
        self.addCleanup(too_small.close)
        with self.assertRaisesRegex(Refused, 'bound|limit'):
            too_small.write(a, s, value)
        import fcntl
        locked = os.open(self.path/LOCK, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, locked)
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(Refused, 'busy|concurrent'):
            self.write()
        self.assertEqual(len(data_entries(self.path)), 1)

    def test_node_lock_uses_private_pinned_inode_not_readable_directory(self):
        # BUG/HYPOTHESIS: flock на readable directory не является private lock.
        import fcntl
        descriptor = self.write()
        path = self.path/LOCK
        before = path.stat()
        self.assertEqual((before.st_uid, before.st_mode & 0o777, before.st_nlink, before.st_size),
                         (os.geteuid(), 0o600, 1, 0))
        directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.write(), descriptor)
        finally:
            os.close(directory)
        # Удалённый inode остаётся pinned; новый inode даже с теми же mode/UID — отказ.
        path.unlink()
        path.touch(mode=0o600)
        self.assertNotEqual(path.stat().st_ino, before.st_ino)
        with self.assertRaisesRegex(Refused, 'lock.*(identity|changed|mismatch)'):
            self.write()
        self.assertEqual(data_entries(self.path), [self.path/descriptor.filename])

    def test_node_lock_refuses_symlink_hardlink_fifo_loose_mode_and_content(self):
        path = self.path/LOCK
        self.assertTrue(path.exists(), 'dedicated private node lock missing')
        saved = self.path/'saved-lock'
        path.rename(saved)
        for kind in ('symlink', 'hardlink', 'fifo', 'mode', 'contents'):
            if kind == 'symlink': path.symlink_to(saved)
            elif kind == 'hardlink': os.link(saved, path)
            elif kind == 'fifo': os.mkfifo(path, 0o600)
            else:
                path.touch(mode=0o600)
                if kind == 'mode': path.chmod(0o640)
                else: path.write_bytes(b'not-lock-metadata')
            with self.subTest(kind=kind), self.assertRaises(Refused):
                with self.f.NodeWriter(self.path, node_uid=os.geteuid()):
                    pass
            path.unlink()

    def test_retained_ownership_is_bounded_and_forget_never_deletes_files(self):
        limited = self.f.NodeWriter(self.path, node_uid=os.geteuid(),
            limits=self.m.Limits(max_files=1, max_bytes=262144, max_file_bytes=262144))
        self.addCleanup(limited.close)
        descriptor = limited.write(self.a, self.s, self.value)
        path = self.path/descriptor.filename
        with self.assertRaises(Refused):
            limited.forget(descriptor)
        path.unlink()
        a, s, value = selected()
        with self.assertRaisesRegex(Refused, 'retained|capacity'):
            limited.write(a, s, value)
        limited.forget(descriptor)
        limited.write(a, s, value)
        self.assertEqual(len(limited.retained), 1)

    def test_limits_refuse_unbounded_or_bool_configuration(self):
        for changes in ({'max_files': True}, {'max_files': 257}, {'max_bytes': 16777217},
                        {'max_file_bytes': 1048577}, {'max_file_bytes': 0}, {'max_bytes': 1}):
            with self.subTest(changes=changes), self.assertRaises(Refused):
                self.m.Limits(**changes)

    def test_no_trusted_root_store_or_body_reader_in_node_uid(self):
        params = dict(node_uid=os.geteuid(), body_uid=os.geteuid()+1, body_gid=os.getgid()+1,
            backend_epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch)
        with self.assertRaises(Refused):
            self.f.SnapshotStore(self.path, self.path.parent/'snapshots', **params)
        with self.assertRaises(Refused):
            self.f.BodyReader(self.path, body_uid=os.geteuid()+1, body_gid=os.getgid()+1,
                backend_epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch)


class ProbeContractTests(unittest.TestCase):
    def test_probe_failure_reports_finite_test_names_not_raw_secret_exception(self):
        api()
        from mop.node import host_input_probe
        report = getattr(host_input_probe, 'safe_failure', None)
        self.assertTrue(callable(report), 'probe must report failed checks without raw diagnostics')
        message = report(b'FAIL: test_fixed_permission (__main__.Fixture.test_fixed_permission)\n'
                         b'AssertionError: raw-secret-sentinel\nFAILED (failures=1)\n')
        self.assertIn('test_fixed_permission', message)
        self.assertNotIn('raw-secret-sentinel', message)
        self.assertNotIn('raw-secret-sentinel', report(b'raw-secret-sentinel'))

    def test_fixed_source_closure_imports_without_installation_or_other_node_modules(self):
        api()
        assert importlib.util.find_spec('mop.node.host_input_probe') is not None, 'owned actual-UID probe missing'
        from mop.node.host_input_probe import snapshot_sources, SOURCES, fixture_args
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            source, context = Path(directory)/'source', Path(directory)/'context'
            source.mkdir()
            context.mkdir()
            for relative in SOURCES:
                dest = source/relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((root/relative).read_bytes())
            (source/'.env').write_text('never-copy-sentinel')
            (source/'mop/node/unselected.py').write_text('raise AssertionError("unselected")')
            snapshot_sources(source, context)
            copied = {str(p.relative_to(context)) for p in context.rglob('*') if p.is_file()}
            self.assertEqual(copied, set(SOURCES) | {'Dockerfile', 'mop/__init__.py',
                'mop/common/__init__.py', 'mop/node/__init__.py'})
            result = subprocess.run([sys.executable, '-ISB', '-c',
                'import sys;sys.path.insert(0,sys.argv[1]);from mop.node import host_inputs,host_input_files;'
                'assert "mop.common.config" not in sys.modules', str(context)],
                cwd=directory, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        args = fixture_args(str(uuid4()))
        joined = ' '.join(args)
        for forbidden in ('--privileged', '--mount', '--volume', 'host', 'docker.sock'):
            self.assertNotIn(forbidden, joined)
        self.assertIn('--network=none', args)
        self.assertIn('--cap-drop=ALL', args)
        self.assertIn('--user=0:0', args)
        from mop import cli
        verbs = set(cli.verbs()['driver'])
        self.assertIn('phase-input-probe', verbs)
        command = importlib.import_module('mop.cli.driver.phase-input-probe')
        self.assertFalse(hasattr(command, 'MCP'))
        for bad in ('../foreign', '', True):
            with self.assertRaises(Refused):
                fixture_args(bad)


if __name__ == '__main__':
    unittest.main()
