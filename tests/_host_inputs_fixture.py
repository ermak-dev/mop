"""Actual Python 3.12 root/node/body acceptance только внутри owned Docker (#746).

HYPOTHESIS: совпадение synthetic stat/UID не проверяет kernel permission boundary.
SOLUTION: реальные fork/setgroups/setgid/setuid, node-private input и root copy.
RESULT: pending actual probe; это не root backend/phase runner acceptance.
STATUS: pending — see #746
"""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, '/opt/fixture')
sys.path.insert(0, '/opt/fixture/tests')
from _host_inputs_data import selected
from mop.node.http_execution import Refused

NODE, BODY = 21071, 21072


def as_uid(uid, function):
    """Короткий child без exec, authority/socket/environment forwarding."""
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            result = dict(ok=True, uid=os.geteuid(), value=function())
        except BaseException as error:
            result = dict(ok=False, uid=os.geteuid(), error=type(error).__name__, reason=str(error))
        raw = json.dumps(result, separators=(',', ':')).encode()
        os.write(write, raw)
        os.close(write)
        os._exit(0)
    os.close(write)
    raw = bytearray()
    while True:
        chunk = os.read(read, 8192)
        if not chunk:
            break
        raw.extend(chunk)
        assert len(raw) < 1048576
    os.close(read)
    _, status = os.waitpid(pid, 0)
    assert status == 0, 'owned UID child failed'
    return json.loads(raw)


class ActualPrivateInputTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, 'fixture needs actual container root')
        self.assertEqual(sys.version_info[:2], (3, 12))
        self.assertIsNotNone(importlib.util.find_spec('mop.node.host_inputs'), 'private phase transport missing (#746)')
        from mop.node import host_inputs as m, host_input_files as f
        self.m, self.f = m, f
        self.tmp = tempfile.TemporaryDirectory(prefix='mop-owned-phase-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.base.chmod(0o755)
        self.private = self.base/'node-private'
        self.private.mkdir(mode=0o700)
        os.chown(self.private, NODE, NODE)
        self.ingress = self.private/'inputs'
        self.ingress.mkdir(mode=0o700)
        os.chown(self.ingress, NODE, NODE)
        self.bearer = self.private/'bearer'
        self.bearer.write_bytes(b'ambient-node-bearer-sentinel')
        self.bearer.chmod(0o600)
        os.chown(self.bearer, NODE, NODE)
        self.snapshots = self.base/'snapshots'
        self.snapshots.mkdir(mode=0o750)
        os.chown(self.snapshots, 0, BODY)
        self.a, self.s, self.value = selected()
        self.owner = self.store()
        self.addCleanup(self.owner.close)

    def store(self, **changes):
        args = dict(node_uid=NODE, body_uid=BODY, body_gid=BODY,
            backend_epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch)
        args.update(changes)
        return self.f.SnapshotStore(self.ingress, self.snapshots, **args)

    def write(self, a=None, s=None, value=None):
        a, s, value = a or self.a, s or self.s, self.value if value is None else value
        def produce():
            with self.f.NodeWriter(self.ingress, node_uid=NODE) as writer:
                return writer.write(a, s, value).wire()
        result = as_uid(NODE, produce)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['uid'], NODE)
        return self.m.Descriptor.parse(result['value'])

    def read(self, descriptor, admission=None):
        def consume():
            with self.f.BodyReader(self.snapshots, body_uid=BODY, body_gid=BODY,
                    backend_epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch) as reader:
                body = reader.read(descriptor, admission or self.a)
                return dict(value=body.value, payload=body.payload.decode())
        return as_uid(BODY, consume)

    def test_actual_exact_bytes_root_ownership_and_kernel_uid_denials(self):
        with patch.dict(os.environ, NODE_BEARER='ambient-node-bearer-sentinel'):
            descriptor = self.write()
            receipt = self.owner.snapshot(NODE, descriptor, self.a)
        source, snapshot = self.ingress/descriptor.filename, self.snapshots/descriptor.filename
        self.assertEqual(source.read_bytes(), snapshot.read_bytes())
        st = snapshot.stat()
        self.assertEqual((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_nlink), (0, BODY, 0o440, 1))
        result = self.read(descriptor)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['uid'], BODY)
        self.assertEqual(result['value']['value'], self.value)
        self.assertEqual(result['value']['payload'].encode(), self.m.canonical_json(self.value))
        for uid, function in ((BODY, lambda: source.read_bytes().decode()),
                              (BODY, lambda: self.bearer.read_bytes().decode()),
                              (NODE, lambda: snapshot.write_bytes(b'node-mutation')),
                              (BODY, lambda: snapshot.write_bytes(b'body-mutation'))):
            denied = as_uid(uid, function)
            self.assertFalse(denied['ok'])
            self.assertEqual(denied['error'], 'PermissionError')
        self.assertNotIn(b'ambient-node-bearer-sentinel', snapshot.read_bytes())
        self.assertNotIn(self.value['consumers'][0]['value'], repr(receipt))

    def test_authenticated_peer_uid_and_trusted_backend_pins_before_copy(self):
        descriptor = self.write()
        for uid in (0, BODY, NODE+100, True):
            with self.assertRaisesRegex(Refused, 'peer'):
                self.owner.snapshot(uid, descriptor, self.a)
        for changes in (dict(backend_epoch=str(uuid4())), dict(node_id=str(uuid4())),
                        dict(executor_epoch=str(uuid4()))):
            owner = self.store(**changes)
            with owner, self.assertRaises(Refused):
                owner.snapshot(NODE, descriptor, self.a)
        self.assertEqual(list(self.snapshots.iterdir()), [])

    def test_snapshot_repeat_is_read_only_and_unknown_existing_never_adopted(self):
        descriptor = self.write()
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        path = self.snapshots/descriptor.filename
        before = path.stat()
        self.assertIs(self.owner.snapshot(NODE, descriptor, self.a), receipt)
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_ino, after.st_mtime_ns, after.st_ctime_ns))
        other = self.store()
        with other, self.assertRaisesRegex(Refused, 'unknown|exists|owned'):
            other.snapshot(NODE, descriptor, self.a)
        for directory in (self.ingress, self.snapshots):
            path = directory/descriptor.filename
            backup = directory/'kept-original'
            path.rename(backup)
            path.write_bytes(backup.read_bytes())
            os.chown(path, NODE if directory == self.ingress else 0, NODE if directory == self.ingress else BODY)
            path.chmod(0o600 if directory == self.ingress else 0o440)
            with self.assertRaises(Refused):
                self.owner.snapshot(NODE, descriptor, self.a)
            path.unlink()
            backup.rename(path)

    def test_wrong_digest_scope_attempt_work_lease_and_expiry_do_not_copy(self):
        descriptor = self.write()
        for d, a in ((replace(descriptor, sha256='0'*64), self.a),
                     (replace(descriptor, attempt_id=str(uuid4())), self.a),
                     (replace(descriptor, scope=replace(self.s, launch_id=str(uuid4()))), self.a),
                     (descriptor, replace(self.a, revision=2)),
                     (descriptor, replace(self.a, lease_until=self.a.lease_until-timedelta(seconds=1))),
                     (descriptor, replace(self.a, lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))):
            with self.assertRaises(Refused):
                self.owner.snapshot(NODE, d, a)
        self.assertEqual(list(self.snapshots.iterdir()), [])

    def test_nofollow_hardlinks_loose_mode_fifo_oversize_and_wrong_owner(self):
        descriptor = self.write()
        path = self.ingress/descriptor.filename
        raw = path.read_bytes()
        path.unlink()
        foreign = self.private/'foreign'
        foreign.write_bytes(raw)
        os.chown(foreign, NODE, NODE)
        foreign.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'fifo', 'mode', 'owner', 'size'):
            if kind == 'symlink': path.symlink_to(foreign)
            elif kind == 'hardlink': os.link(foreign, path)
            elif kind == 'fifo': os.mkfifo(path, 0o600)
            else:
                path.write_bytes(raw if kind != 'size' else b'x'*(self.m.Limits().max_file_bytes+1))
            if kind != 'symlink':
                os.chown(path, BODY if kind == 'owner' else NODE, NODE)
                path.chmod(0o644 if kind == 'mode' else 0o600)
            with self.subTest(kind=kind), self.assertRaises(Refused):
                self.owner.snapshot(NODE, descriptor, self.a)
            path.unlink()
        self.assertEqual(list(self.snapshots.iterdir()), [])

    def test_parent_symlink_replacement_and_file_swap_during_read_never_copy(self):
        descriptor = self.write()
        self.ingress.rename(self.private/'old')
        self.ingress.symlink_to(self.private/'old', target_is_directory=True)
        with self.assertRaises(Refused):
            self.owner.snapshot(NODE, descriptor, self.a)
        self.ingress.unlink()
        (self.private/'old').rename(self.ingress)
        path = self.ingress/descriptor.filename
        real_read = os.read
        swapped = False
        def racing(fd, size):
            nonlocal swapped
            raw = real_read(fd, size)
            if not swapped:
                swapped = True
                path.rename(self.private/'old-input')
                path.write_bytes(raw)
                os.chown(path, NODE, NODE)
                path.chmod(0o600)
            return raw
        with patch('os.read', side_effect=racing), self.assertRaises(Refused):
            self.owner.snapshot(NODE, descriptor, self.a)
        self.assertEqual(list(self.snapshots.iterdir()), [])

    def test_in_place_mutation_while_reading_never_copies(self):
        descriptor = self.write()
        path = self.ingress/descriptor.filename
        real_read = os.read
        changed = False
        def racing(fd, size):
            nonlocal changed
            raw = real_read(fd, size)
            if not changed:
                changed = True
                with path.open('ab') as output:
                    output.write(b' ')
            return raw
        with patch('os.read', side_effect=racing), self.assertRaises(Refused):
            self.owner.snapshot(NODE, descriptor, self.a)
        self.assertEqual(list(self.snapshots.iterdir()), [])

    def test_body_strict_actual_delivery_not_root_nested_interpretation(self):
        descriptor = self.write()
        source = self.ingress/descriptor.filename
        outer = json.loads(source.read_bytes())
        outer['payload'] = 'opaque malformed project YAML: [!execute-root]'
        outer['payload_digest'] = hashlib.sha256(outer['payload'].encode()).hexdigest()
        raw = self.m.canonical_json(outer)
        source.write_bytes(raw)
        descriptor = replace(descriptor, sha256=hashlib.sha256(raw).hexdigest())
        with patch('mop.common.initialcreate.parse_delivery', side_effect=AssertionError('root parsed payload')):
            self.owner.snapshot(NODE, descriptor, self.a)
        result = self.read(descriptor)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'Refused')
        self.assertNotIn('execute-root', result['reason'])

    def test_retained_cleanup_after_work_expiry_is_not_body_retirement(self):
        descriptor = self.write()
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        # Receipt хранит physical ownership, а не новую Work/lease authority.
        from mop.node import host_inputs
        future = self.a.work.not_after + timedelta(seconds=1)
        with patch.object(host_inputs, 'now', return_value=future):
            with self.assertRaises(Refused):
                self.owner.snapshot(NODE, descriptor, self.a)
            self.assertTrue(self.owner.cleanup_input(receipt))
            self.assertTrue(self.owner.cleanup_input(receipt))
            self.assertTrue((self.snapshots/descriptor.filename).exists())
            denied = self.read(descriptor)
            self.assertFalse(denied['ok'])
            self.assertTrue(self.owner.cleanup_snapshot(receipt))
            self.assertTrue(self.owner.cleanup_snapshot(receipt))
        self.assertFalse((self.ingress/descriptor.filename).exists())
        self.assertEqual(list(self.snapshots.iterdir()), [])

    def test_retained_cleanup_never_deletes_foreign_replacement_or_forged_receipt(self):
        descriptor = self.write()
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        with self.assertRaises(Refused):
            self.owner.cleanup_snapshot(copy.copy(receipt))
        for directory, cleanup in ((self.ingress, self.owner.cleanup_input),
                                   (self.snapshots, self.owner.cleanup_snapshot)):
            path = directory/descriptor.filename
            path.rename(directory/'original-retained')
            path.write_bytes(b'foreign replacement must survive')
            st = path.stat()
            with self.assertRaises(Refused):
                cleanup(receipt)
            self.assertEqual(path.stat().st_ino, st.st_ino)
            self.assertEqual(path.read_bytes(), b'foreign replacement must survive')

    def test_cleanup_swap_between_check_and_rename_preserves_foreign_physical_file(self):
        descriptor = self.write()
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        path = self.ingress/descriptor.filename
        real_rename = os.rename
        swapped = False
        replacement_inode = None
        def racing(src, dst, **kwargs):
            nonlocal swapped, replacement_inode
            if not swapped and src == descriptor.filename:
                swapped = True
                path.rename(self.private/'original-retained')
                path.write_bytes(b'foreign cleanup race')
                os.chown(path, NODE, NODE)
                path.chmod(0o600)
                replacement_inode = path.stat().st_ino
            return real_rename(src, dst, **kwargs)
        with patch('os.rename', side_effect=racing), self.assertRaises(Refused):
            self.owner.cleanup_input(receipt)
        candidates = [p for directory in (self.ingress, self.snapshots) for p in directory.iterdir()]
        self.assertTrue(any(p.stat().st_ino == replacement_inode and p.read_bytes() == b'foreign cleanup race'
                            for p in candidates), 'foreign raced inode must not be deleted')

    def test_root_file_and_byte_capacity_stays_bounded_with_concurrent_calls(self):
        first = self.write()
        a2, s2, value2 = selected()
        # Все inputs одного trusted backend/node/executor, но другого Work/Scope.
        work = replace(a2.work, epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch)
        a2 = replace(a2, work=work)
        s2 = replace(s2, epoch=work.epoch, node_id=work.node_id, executor_epoch=work.executor_epoch)
        value2['work'] = work.wire()
        second = self.write(a2, s2, value2)
        size = len((self.ingress/first.filename).read_bytes())
        owner = self.store(limits=self.m.Limits(max_files=1, max_bytes=size, max_file_bytes=size))
        results = []
        def attempt(d, a):
            try: results.append(owner.snapshot(NODE, d, a))
            except Refused: results.append(None)
        with owner:
            threads = [threading.Thread(target=attempt, args=item) for item in ((first, self.a), (second, a2))]
            for t in threads: t.start()
            for t in threads: t.join(timeout=5)
            self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertLessEqual(len(list(self.snapshots.iterdir())), 1)
        self.assertLessEqual(sum(p.stat().st_size for p in self.snapshots.iterdir()), size)
        self.assertEqual(sum(r is not None for r in results), 1)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ActualPrivateInputTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(json.dumps(dict(status='accepted', python='3.12', root_uid=0, node_uid=NODE, body_uid=BODY,
        checks=result.testsRun, transport_only=True, native_execution=False), sort_keys=True))
