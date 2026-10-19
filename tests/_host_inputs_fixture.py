"""Actual Python 3.12 root/node/body acceptance только внутри owned Docker (#746).

HYPOTHESIS: совпадение synthetic stat/UID не проверяет kernel permission boundary.
SOLUTION: реальные fork/setgroups/setgid/setuid, node-private input и root copy.
RESULT: actual Python 3.12 root=0/node=21071/body=21072: 19 checks GREEN.
Отдельные RED для mutable receipt/large reader, failed cleanup, stale release и
world-readable foreign quarantine.
BUG/HYPOTHESIS: body O_RDONLY directory flock блокирует root snapshot/cleanup.
SOLUTION: owner-private 0600 regular lock, новый open description и pinned inode.
RESULT: actual UID barrier RED до fix; 24 checks GREEN, body держит directory
flock во время fresh/repeated snapshot и обеих уборок; private lock body недоступен.
Это не root backend/phase runner acceptance.
STATUS: FIXED — see #746
"""
import copy
from contextlib import contextmanager
import fcntl
import select
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
LOCK = '.phase-input.lock'


def data_entries(directory):
    """Fixed lock metadata не расходует budget approved data; unknown entries остаются."""
    return [entry for entry in directory.iterdir() if entry.name != LOCK]


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

    # BUG/HYPOTHESIS #751: strict codec принимает retained maximum, но
    # default writer/root copy отказывают после admission на старых 256 KiB.
    # Actual UID + hashes не подменяются mocked stat или большим pipe budget.
    def bootstrap_budget_roundtrip(self, tasks, variables=b'', provenance='fixture', refs=()):
        from mop.common.bootstrap_envelope import bounded_content, encode
        import base64
        prepare_a, prepare_value = replace(self.a, attempt_id=str(uuid4())), self.value
        _, _, self.value = selected('bootstrap_body')
        self.a = replace(self.a, work=replace(self.a.work, action='bootstrap_body'))
        self.value['work'] = self.a.work.wire()
        raw = encode(bounded_content(tasks, variables, provenance, 'a'*64, refs))
        self.value['bootstrap'] = dict(envelope=base64.b64encode(raw).decode())
        self.value['bootstrap_digest'] = hashlib.sha256(raw).hexdigest()
        private_size = len(self.m.pack(self.a, self.s, self.value).raw)
        for cap in (262144, private_size-1):
            def lowered():
                with self.f.NodeWriter(self.ingress, node_uid=NODE,
                        limits=self.m.Limits(max_file_bytes=cap)) as writer:
                    return writer.write(self.a, self.s, self.value).wire()
            denied = as_uid(NODE, lowered)
            self.assertFalse(denied['ok'], denied)
            self.assertEqual(denied['error'], 'Refused')
            self.assertEqual(data_entries(self.ingress), [])
        def produce():
            with self.f.NodeWriter(self.ingress, node_uid=NODE) as writer:
                large = writer.write(self.a, self.s, self.value).wire()
                # Mixed retained phases: prepare не получает bootstrap budget,
                # но большой owned bootstrap не ломает следующий small write.
                small = writer.write(prepare_a, self.s, prepare_value).wire()
                return large, small
        produced = as_uid(NODE, produce)
        self.assertTrue(produced['ok'], produced)
        descriptor, small = (self.m.Descriptor.parse(value) for value in produced['value'])
        for cap in (262144, private_size-1):
            with self.store(limits=self.m.Limits(max_file_bytes=cap)) as lower:
                with self.assertRaises(Refused):
                    lower.snapshot(NODE, descriptor, self.a)
                self.assertEqual(lower.retained, ())
                self.assertEqual(data_entries(self.snapshots), [])
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        small_receipt = self.owner.snapshot(NODE, small, prepare_a)
        def consume():
            from mop.common.bootstrap_envelope import decode_base64
            with self.f.BodyReader(self.snapshots, body_uid=BODY, body_gid=BODY,
                    backend_epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch) as reader:
                result = reader.read(descriptor, self.a)
                decoded = decode_base64(result.value['bootstrap']['envelope'], result.value['bootstrap_digest'])
                return dict(tasks=hashlib.sha256(decoded.tasks).hexdigest(),
                    variables=hashlib.sha256(decoded.variables).hexdigest(), refs=len(decoded.secret_refs))
        result = as_uid(BODY, consume)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['uid'], BODY)
        self.assertEqual(result['value'], dict(tasks=hashlib.sha256(tasks).hexdigest(),
            variables=hashlib.sha256(variables).hexdigest(), refs=len(refs)))
        self.owner.cleanup_input(receipt)
        self.owner.cleanup_snapshot(receipt)
        self.owner.cleanup_input(small_receipt)
        self.owner.cleanup_snapshot(small_receipt)
        self.assertEqual(data_entries(self.ingress), [])
        self.assertEqual(data_entries(self.snapshots), [])

    def test_150k_bootstrap_crosses_default_actual_node_root_body_transport(self):
        self.bootstrap_budget_roundtrip(b'x'*150000)

    def test_maximum_retained_envelope_crosses_actual_node_root_body_transport(self):
        self.bootstrap_budget_roundtrip(b'x'*262144, b'y'*131072, '\U0001f600'*1024,
            tuple(uuid4() for _ in range(32)))

    def read(self, descriptor, admission=None):
        def consume():
            with self.f.BodyReader(self.snapshots, body_uid=BODY, body_gid=BODY,
                    backend_epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch) as reader:
                body = reader.read(descriptor, admission or self.a)
                return dict(value=body.value, payload=body.payload.decode())
        return as_uid(BODY, consume)

    @contextmanager
    def held_directory_lock(self, uid, directory):
        """Actual UID держит LOCK_EX до явного release, не timing-based sleep."""
        ready_r, ready_w = os.pipe()
        release_r, release_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(ready_r)
            os.close(release_w)
            try:
                # Body не получает даже унаследованные root-private descriptors.
                self.owner.close()
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(ready_w, str(os.geteuid()).encode())
                if not select.select([release_r], [], [], 5)[0]:
                    os._exit(3)
                os.read(release_r, 1)
                os.close(fd)
                os._exit(0)
            except BaseException:
                os._exit(2)
        os.close(ready_w)
        os.close(release_r)
        try:
            self.assertTrue(select.select([ready_r], [], [], 5)[0], 'UID lock barrier missing')
            self.assertEqual(os.read(ready_r, 32), str(uid).encode(), 'UID directory lock not held')
            yield
        finally:
            os.close(ready_r)
            try:
                os.write(release_w, b'1')
            except BrokenPipeError:
                pass
            os.close(release_w)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, 'UID lock child failed')

    # BUG #759 / HYPOTHESIS: node pre-submit cleanup требует retained inode owner,
    # не descriptor; close не вправе удалять replacement или uncertain input.
    # SOLUTION: exact saved receipt и private capture; original inode pinned.
    # RESULT: 27 actual UID проверок прошли на Python 3.12, root/node/body раздельны.
    # STATUS: FIXED — see #759.
    def test_actual_node_uid_stage_cleanup_requires_same_receipt_and_exact_inode(self):
        def produce():
            from dataclasses import replace
            self.owner.close()
            with self.f.NodeWriter(self.ingress, node_uid=NODE) as writer:
                assert callable(getattr(writer, 'stage', None)), 'node staging receipt missing'
                receipt = writer.stage(self.a, self.s, self.value)
                path = self.ingress/receipt.descriptor.filename
                for forged in (receipt.descriptor, replace(receipt)):
                    try: writer.cleanup(forged)
                    except Refused: pass
                    else: raise AssertionError('forged node cleanup authority accepted')
                    assert path.exists()
                assert writer.cleanup(receipt) and not path.exists() and not writer.retained
                receipt = writer.stage(self.a, self.s, self.value)
                path.rename(self.ingress/'original-owned')
                path.write_bytes(b'foreign-input-never-delete')
                path.chmod(0o600)
                inode = path.stat().st_ino
                try: writer.cleanup(receipt)
                except Refused: pass
                else: raise AssertionError('foreign node inode accepted')
                writer.close()
                assert path.stat().st_ino == inode and path.read_bytes() == b'foreign-input-never-delete'
                assert (self.ingress/'original-owned').exists() and len(writer.retained) == 1
            return True
        result = as_uid(NODE, produce)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['uid'], NODE)

    def test_actual_node_uid_capture_race_never_unlinks_foreign_input(self):
        def produce():
            self.owner.close()
            with self.f.NodeWriter(self.ingress, node_uid=NODE) as writer:
                assert callable(getattr(writer, 'stage', None)), 'node staging receipt missing'
                receipt = writer.stage(self.a, self.s, self.value)
                path = self.ingress/receipt.descriptor.filename
                rename, foreign = os.rename, []
                def race(src, dst, **kw):
                    if src == path.name and kw.get('src_dir_fd') == writer.directory.fd:
                        rename(path, self.ingress/'original-owned')
                        path.write_bytes(b'foreign-race-preserve')
                        path.chmod(0o600)
                        foreign.append(path.stat().st_ino)
                    return rename(src, dst, **kw)
                try:
                    with patch('os.rename', side_effect=race): writer.cleanup(receipt)
                except Refused: pass
                else: raise AssertionError('foreign capture was deleted')
                copies = [p for p in self.ingress.rglob('*') if p.is_file() and p.stat().st_ino in foreign]
                assert foreign and copies and len(writer.retained) == 1
                assert all(p.read_bytes() == b'foreign-race-preserve' for p in copies)
            return True
        result = as_uid(NODE, produce)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['uid'], NODE)

    def test_body_readonly_directory_flock_cannot_block_root_snapshot_or_cleanup(self):
        # BUG: body O_RDONLY directory fd удерживает flock всего root transport.
        # HYPOTHESIS: доступный body inode нельзя использовать как root lock.
        descriptor = self.write()
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        a, s, value = selected()
        work = replace(a.work, epoch=self.s.epoch, node_id=self.s.node_id,
                       executor_epoch=self.s.executor_epoch)
        a = replace(a, work=work)
        s = replace(s, epoch=work.epoch, node_id=work.node_id, executor_epoch=work.executor_epoch)
        value['work'] = work.wire()
        second = self.write(a, s, value)
        blocked, created = [], []
        with self.held_directory_lock(BODY, self.snapshots):
            for name, operation in (
                    ('snapshot_repeat', lambda: self.owner.snapshot(NODE, descriptor, self.a)),
                    ('snapshot_fresh_scope', lambda: created.append(self.owner.snapshot(NODE, second, a))),
                    ('cleanup_input', lambda: self.owner.cleanup_input(receipt)),
                    ('cleanup_snapshot', lambda: self.owner.cleanup_snapshot(receipt))):
                try:
                    operation()
                except Refused as error:
                    blocked.append((name, str(error)))
            self.assertEqual(blocked, [], 'body read-only directory flock blocked root operations')
            self.assertEqual(len(created), 1)
            self.assertTrue(self.owner.cleanup_input(created[0]))
            self.assertTrue(self.owner.cleanup_snapshot(created[0]))
        self.assertEqual(data_entries(self.ingress), [])
        self.assertEqual(data_entries(self.snapshots), [])

    def test_root_lock_is_private_even_through_body_readonly_directory_fd(self):
        descriptor = self.write()
        self.owner.snapshot(NODE, descriptor, self.a)
        for directory, uid in ((self.ingress, NODE), (self.snapshots, 0)):
            value = (directory/LOCK).stat()
            self.assertTrue(stat.S_ISREG(value.st_mode))
            self.assertEqual((value.st_uid, stat.S_IMODE(value.st_mode), value.st_nlink, value.st_size),
                             (uid, 0o600, 1, 0))
        def via_body_fd():
            with self.f.BodyReader(self.snapshots, body_uid=BODY, body_gid=BODY,
                    backend_epoch=self.s.epoch, node_id=self.s.node_id,
                    executor_epoch=self.s.executor_epoch) as reader:
                fd = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=reader.directory.fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
        for uid, function in ((BODY, via_body_fd), (NODE, lambda: os.open(self.snapshots/LOCK, os.O_RDONLY)),
                              (BODY, lambda: os.open(self.ingress/LOCK, os.O_RDONLY))):
            denied = as_uid(uid, function)
            self.assertFalse(denied['ok'], denied)
            self.assertEqual(denied['error'], 'PermissionError', denied)
        self.assertTrue(self.read(descriptor)['ok'])

    def test_root_private_lock_serializes_store_processes_and_threads(self):
        descriptor = self.write()
        other = self.store()
        with other, self.owner.snapshots.lock():
            result = as_uid(0, lambda: other.snapshot(NODE, descriptor, self.a))
            self.assertFalse(result['ok'], result)
            self.assertEqual(result['error'], 'Refused')
            self.assertIn('busy', result['reason'])
            errors = []
            def attempt():
                try:
                    other.snapshot(NODE, descriptor, self.a)
                except Refused as error:
                    errors.append(str(error))
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIn('busy', errors[0])
            self.assertEqual(data_entries(self.snapshots), [])
        self.owner.snapshot(NODE, descriptor, self.a)

    def test_root_lock_inode_survives_cleanup_forget_close_and_reopen(self):
        descriptor = self.write()
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        path = self.snapshots/LOCK
        before = path.stat()
        self.owner.cleanup_input(receipt)
        self.owner.cleanup_snapshot(receipt)
        self.owner.forget(receipt)
        self.owner.close()
        with self.store() as other, other.snapshots.lock():
            after = path.stat()
            self.assertEqual((before.st_dev, before.st_ino, before.st_ctime_ns),
                             (after.st_dev, after.st_ino, after.st_ctime_ns))
        self.assertEqual(data_entries(self.snapshots), [])

    def test_root_lock_rejects_untrusted_inode_and_never_adopts_replacement(self):
        descriptor = self.write()
        path = self.snapshots/LOCK
        # Root-owned namespace меняется только fixture root: unprivileged UID не может.
        self.assertTrue(path.exists(), 'dedicated private lock inode missing')
        old = self.snapshots/'saved-lock'
        path.rename(old)
        for kind in ('symlink', 'hardlink', 'fifo', 'directory', 'owner', 'mode', 'contents'):
            if kind == 'symlink': path.symlink_to(old)
            elif kind == 'hardlink': os.link(old, path)
            elif kind == 'fifo': os.mkfifo(path, 0o600)
            elif kind == 'directory': path.mkdir(mode=0o600)
            else:
                path.touch(mode=0o600)
                if kind == 'owner': os.chown(path, BODY, BODY)
                if kind == 'mode': path.chmod(0o640)
                if kind == 'contents': path.write_bytes(b'not-lock-metadata')
            with self.subTest(kind=kind), self.assertRaises(Refused):
                with self.store():
                    pass
            if kind == 'directory': path.rmdir()
            else: path.unlink()
        path.touch(mode=0o600)
        replacement = path.stat()
        with self.assertRaisesRegex(Refused, 'lock.*(identity|changed|mismatch)'):
            self.owner.snapshot(NODE, descriptor, self.a)
        self.assertEqual(path.stat().st_ino, replacement.st_ino)
        self.assertFalse((self.snapshots/descriptor.filename).exists())

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
        self.assertEqual(data_entries(self.snapshots), [])

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
        self.assertEqual(data_entries(self.snapshots), [])

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
        self.assertEqual(data_entries(self.snapshots), [])

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
        self.assertEqual(data_entries(self.snapshots), [])

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
        self.assertEqual(data_entries(self.snapshots), [])

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

    def test_body_checks_selected_bootstrap_digest_after_opaque_root_snapshot(self):
        # HYPOTHESIS #751: transport hash верен и root копирует bytes, но body
        # обязан отдельно отказать envelope с чужими tasks при прежнем pin.
        # SOLUTION: общий strict codec только в body, root не вызывает parser.
        # RESULT: настоящий UID body принимает original и отказывает mixed
        # envelope после успешного opaque root snapshot; 25/25 checks GREEN.
        # STATUS: FIXED — see #751; YAML не разбирается и не исполняется.
        import base64
        from mop.common import bootstrap_envelope as codec
        a, s, value = selected('bootstrap_body')
        work = replace(a.work, epoch=self.s.epoch, node_id=self.s.node_id, executor_epoch=self.s.executor_epoch)
        a = replace(a, work=work)
        s = replace(s, epoch=work.epoch, node_id=work.node_id, executor_epoch=work.executor_epoch)
        value['work'] = work.wire()
        descriptor = self.write(a, s, value)
        with patch('mop.common.initialcreate.parse_delivery', side_effect=AssertionError('root parsed bootstrap')):
            receipt = self.owner.snapshot(NODE, descriptor, a)
        original = self.read(descriptor, a)
        self.assertTrue(original['ok'], original)
        delivered = original['value']['value']
        content = codec.decode_base64(delivered['bootstrap']['envelope'], delivered['bootstrap_digest'])
        self.assertEqual(content.tasks, b'- command: never-root-executed\n')
        self.assertEqual(content.variables, b'opaque: project-yaml\n')
        self.assertTrue(self.owner.cleanup_input(receipt))
        self.assertTrue(self.owner.cleanup_snapshot(receipt))
        descriptor = self.write(a, s, value)
        source = self.ingress/descriptor.filename
        outer = json.loads(source.read_bytes())
        payload = json.loads(outer['payload'])
        bootstrap = json.loads(base64.b64decode(payload['bootstrap']['envelope']))
        bootstrap['tasks'] = base64.b64encode(b'private-unapproved-bootstrap').decode()
        payload['bootstrap']['envelope'] = base64.b64encode(self.m.canonical_json(bootstrap)).decode()
        outer['payload'] = self.m.canonical_json(payload).decode()
        outer['payload_digest'] = hashlib.sha256(outer['payload'].encode()).hexdigest()
        raw = self.m.canonical_json(outer)
        source.write_bytes(raw)
        descriptor = replace(descriptor, sha256=hashlib.sha256(raw).hexdigest())
        # Другой exact snapshot, не подмена уже удерживаемого receipt.
        self.owner.close()
        self.owner = self.store()
        self.addCleanup(self.owner.close)
        with patch('mop.common.initialcreate.parse_delivery', side_effect=AssertionError('root parsed bootstrap')):
            self.owner.snapshot(NODE, descriptor, a)
        result = self.read(descriptor, a)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'Refused')
        self.assertIn('digest mismatch', result['reason'])
        self.assertNotIn('private-unapproved-bootstrap', result['reason'])

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
        self.assertEqual(data_entries(self.snapshots), [])

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
                path.chmod(0o444)
                replacement_inode = path.stat().st_ino
            return real_rename(src, dst, **kwargs)
        with patch('os.rename', side_effect=racing), self.assertRaises(Refused):
            self.owner.cleanup_input(receipt)
        candidates = [p for directory in (self.ingress, self.snapshots) for p in directory.rglob('*')
                      if p.is_file()]
        captured = [p for p in candidates if p.stat().st_ino == replacement_inode]
        self.assertTrue(captured, 'foreign raced inode must not be deleted')
        self.assertTrue(all(p.read_bytes() == b'foreign cleanup race' for p in captured))
        for path in captured:
            denied = as_uid(BODY, lambda: path.read_bytes().decode())
            self.assertFalse(denied['ok'], 'quarantine must not expose even world-readable private input')
            self.assertEqual(denied['error'], 'PermissionError')

    def test_receipt_is_immutable_and_retained_slots_need_explicit_completed_release(self):
        from dataclasses import FrozenInstanceError
        descriptor = self.write()
        size = len((self.ingress/descriptor.filename).read_bytes())
        owner = self.store(limits=self.m.Limits(max_files=1, max_bytes=size*2, max_file_bytes=size))
        with owner:
            receipt = owner.snapshot(NODE, descriptor, self.a)
            with self.assertRaises((FrozenInstanceError, AttributeError)):
                receipt.descriptor = replace(descriptor, sha256='0'*64)
            with self.assertRaises(Refused):
                owner.forget(receipt)
            # Cleanup перемещает уже выделенный ingress inode, не новый file/bytes.
            self.assertTrue(owner.cleanup_input(receipt))
            self.assertTrue((self.snapshots/descriptor.filename).exists())
            self.assertTrue(owner.cleanup_snapshot(receipt))
            owner.forget(receipt)
            self.assertEqual(owner.retained, ())
            with self.assertRaises(Refused):
                owner.cleanup_snapshot(receipt)

    def test_body_accepts_large_but_bounded_snapshot_and_refuses_substituted_snapshot(self):
        import base64
        value = copy.deepcopy(self.value)
        value['consumers'][0]['value'] = base64.b64encode(b'x'*200000).decode()
        raw = self.m.pack(self.a, self.s, value).raw
        self.assertGreater(len(raw), self.m.Limits().max_file_bytes)
        def produce():
            with self.f.NodeWriter(self.ingress, node_uid=NODE,
                    limits=self.m.Limits(max_file_bytes=1048576)) as writer:
                return writer.write(self.a, self.s, value).wire()
        produced = as_uid(NODE, produce)
        self.assertTrue(produced['ok'], produced)
        descriptor = self.m.Descriptor.parse(produced['value'])
        with self.store(limits=self.m.Limits(max_file_bytes=1048576)) as owner:
            owner.snapshot(NODE, descriptor, self.a)
            read = self.read(descriptor)
            self.assertTrue(read['ok'], read)
            self.assertEqual(read['value']['value'], value)
        path = self.snapshots/descriptor.filename
        path.unlink()
        path.symlink_to(self.bearer)
        read = self.read(descriptor)
        self.assertFalse(read['ok'])
        self.assertEqual(read['error'], 'Refused')

    def test_partial_write_keeps_exact_cleanup_receipt_and_never_publishes_body_bytes(self):
        descriptor = self.write()
        real_write = os.write
        calls = 0
        def failing(fd, raw):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, raw[:100])
            raise OSError('raw-secret-must-not-leak')
        with patch('os.write', side_effect=failing), self.assertRaises(Refused) as caught:
            self.owner.snapshot(NODE, descriptor, self.a)
        self.assertNotIn('raw-secret-must-not-leak', str(caught.exception))
        receipt, = self.owner.retained
        self.assertFalse(self.read(descriptor)['ok'])
        with self.assertRaises(Refused):
            self.owner.snapshot(NODE, descriptor, self.a)
        self.assertTrue(self.owner.cleanup_snapshot(receipt))
        self.assertTrue(self.owner.cleanup_input(receipt))
        self.assertEqual(data_entries(self.snapshots), [])

    def test_cleanup_failure_retains_captured_inode_for_expired_retry_without_foreign_deletion(self):
        descriptor = self.write()
        receipt = self.owner.snapshot(NODE, descriptor, self.a)
        real_unlink = os.unlink
        failed = False
        def failing(name, **kwargs):
            nonlocal failed
            if not failed and name == descriptor.filename+'.cleanup':
                failed = True
                raise OSError('raw-cleanup-secret')
            return real_unlink(name, **kwargs)
        with patch('os.unlink', side_effect=failing), self.assertRaises(Refused) as caught:
            self.owner.cleanup_input(receipt)
        self.assertNotIn('raw-cleanup-secret', str(caught.exception))
        source = self.ingress/descriptor.filename
        self.assertFalse(source.exists())
        source.write_bytes(b'new foreign input after capture')
        source.chmod(0o600)
        os.chown(source, NODE, NODE)
        from mop.node import host_inputs
        with patch.object(host_inputs, 'now', return_value=self.a.work.not_after+timedelta(seconds=1)):
            self.assertTrue(self.owner.cleanup_input(receipt))
        self.assertEqual(source.read_bytes(), b'new foreign input after capture')
        self.assertTrue((self.snapshots/descriptor.filename).exists())
        self.assertFalse((self.snapshots/(descriptor.filename+'.cleanup')).exists())

    def test_source_mutation_during_copy_keeps_own_snapshot_cleanup_but_no_success_ack(self):
        descriptor = self.write()
        source = self.ingress/descriptor.filename
        real_write = os.write
        changed = False
        def racing(fd, raw):
            nonlocal changed
            if not changed:
                changed = True
                source.write_bytes(b'foreign modified source')
            return real_write(fd, raw)
        with patch('os.write', side_effect=racing), self.assertRaises(Refused):
            self.owner.snapshot(NODE, descriptor, self.a)
        receipt, = self.owner.retained
        with self.assertRaises(Refused):
            self.owner.cleanup_input(receipt)
        self.assertTrue(self.owner.cleanup_snapshot(receipt))
        self.assertEqual(source.read_bytes(), b'foreign modified source')

    def test_stale_forget_cannot_discard_new_ownership_after_lock_acquisition(self):
        from contextlib import contextmanager
        descriptor = self.write()
        old = self.owner.snapshot(NODE, descriptor, self.a)
        self.owner.cleanup_input(old)
        self.owner.cleanup_snapshot(old)
        original_lock = self.owner.ingress.lock
        triggered = False
        replacement = None
        @contextmanager
        def racing_lock():
            nonlocal triggered, replacement
            if not triggered:
                triggered = True
                self.owner.forget(old)
                self.write()
                replacement = self.owner.snapshot(NODE, descriptor, self.a)
            with original_lock():
                yield
        with patch.object(self.owner.ingress, 'lock', side_effect=racing_lock), self.assertRaises(Refused):
            self.owner.forget(old)
        self.assertIn(replacement, self.owner.retained)
        self.assertTrue(self.owner.cleanup_input(replacement))
        self.assertTrue(self.owner.cleanup_snapshot(replacement))

    def test_actual_node_processes_cannot_race_file_or_byte_cap(self):
        limit = self.m.Limits(max_files=1, max_bytes=262144, max_file_bytes=262144)
        children = []
        for _ in range(4):
            a, s, value = selected()
            pid = os.fork()
            if pid == 0:
                os.setgroups([])
                os.setgid(NODE)
                os.setuid(NODE)
                try:
                    with self.f.NodeWriter(self.ingress, node_uid=NODE, limits=limit) as writer:
                        writer.write(a, s, value)
                    os._exit(0)
                except Refused:
                    os._exit(2)
                except BaseException:
                    os._exit(3)
            children.append(pid)
        codes = [os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in children]
        self.assertEqual(codes.count(0), 1, codes)
        self.assertEqual(codes.count(2), 3, codes)
        self.assertEqual(len(data_entries(self.ingress)), 1)
        self.assertLessEqual(sum(p.stat().st_size for p in data_entries(self.ingress)), limit.max_bytes)

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
        self.assertLessEqual(len(data_entries(self.snapshots)), 1)
        self.assertLessEqual(sum(p.stat().st_size for p in data_entries(self.snapshots)), size)
        self.assertEqual(sum(r is not None for r in results), 1)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ActualPrivateInputTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(json.dumps(dict(status='accepted', python='3.12', root_uid=0, node_uid=NODE, body_uid=BODY,
        checks=result.testsRun, transport_only=True, native_execution=False), sort_keys=True))
