#!/usr/bin/env python3
"""HYPOTHESIS: pane/PID или свежий cwd registry не доказывают native session.
SPEC: exact registered tmux/runtime socket, process-derived config, registry/key,
Linux UID/start/boot/cwd/namespaces/TTY/foreground и connected SO_PEERCRED;
producer epoch даёт только explicit trusted lifecycle owner, не native CLI.
SOLUTION: mop-owned observer и stdlib identity helpers, явный lookup producer
у trusted lifecycle owner; native registry не выдаёт mop полномочия.
RESULT: первые 10 тестов RED (observer отсутствовал); отдельно воспроизведены
потеря safe refusal и подмена producer через executor_epoch. Все 16 тестов
GREEN: native-shaped синтетическая fixture с настоящими Linux процессом,
Unix listener и tmux, в том числе смена cwd и fd/0, peer/path replacement.
Ни actual Claude acceptance, ни readiness/retirement этим не подтверждены.
STATUS: FIXED — see #643 (только physical observer, production owner не подключён).
"""
import asyncio
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _body_fixture import work
from mop import session
from mop.driver.launch import RegisteredLayout, Registration
from mop.node.execution_adapters import SessionProof, process_identity
from mop.node.http_execution import Unavailable


class Owner:
    """Явная fixture-подмена будущего lifecycle owner, не native полномочия."""
    def __init__(self):
        self.epoch = uuid4()
        self.calls = []

    async def producer_epoch(self, layout, identity):
        self.calls.append((layout, identity))
        return self.epoch


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    def observer(self, owner=True):
        self.assertIsNotNone(importlib.util.find_spec('mop.node.native_observer'),
                             'mop-owned native physical observer is missing')
        from mop.node import native_observer
        self.module = native_observer
        return native_observer.NativePhysicalSessions(owner=self.owner if owner else None)

    async def asyncSetUp(self):
        if not shutil.which('tmux'):
            hermetic.skip('native observer', 'tmux is required')
            self.skipTest('tmux is required')
        self.directory = tempfile.TemporaryDirectory(prefix='m643o-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.registration = Registration(work(), str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        self.layout = RegisteredLayout(self.root, self.registration)
        self.layout.clone.mkdir(parents=True)
        self.layout.runtime.mkdir(parents=True)
        self.config = self.root/'native-config'
        self.config.mkdir()
        self.sock = self.layout.runtime/'session.sock'
        self.base = ['tmux', '-S', str(self.layout.tmux_socket)]
        self.owner = Owner()
        fixture = Path(__file__).with_name('_native_observer_fixture.py')
        env = {'HOME': str(self.root), 'CLAUDE_CONFIG_DIR': str(self.config),
               'PATH': os.defpath, 'TERM': 'xterm-256color', 'SHELL': '/bin/sh',
               'LC_ALL': 'C.UTF-8'}
        argv = [sys.executable, '-I', '-u', str(fixture), str(self.sock)]
        result = subprocess.run([*self.base, '-f', '/dev/null', 'new-session', '-d',
            '-s', 'owned', '-c', str(self.layout.clone), 'exec '+shlex.join(argv)],
            env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(self.stop)
        self.record_path = self.config/'sessions/owned.json'
        deadline = time.monotonic()+3
        while not self.record_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(self.record_path.exists(), 'owned fixture did not publish registry')
        self.record = json.loads(self.record_path.read_text())
        self.pid = self.record['pid']
        self.start = process_identity(self.pid)
        self.key_path = next((self.config/'sessions').glob('*.key'))

    def stop(self):
        subprocess.run([*self.base, 'kill-server'], capture_output=True, timeout=3)
        # Только PID своей fixture со снятым start, не чужая сессия/tmux.
        if not hasattr(self, 'pid'):
            return
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            try:
                if process_identity(self.pid) != self.start:
                    return
            except Unavailable:
                return
            time.sleep(.01)
        raise AssertionError('owned fixture process survived cleanup')

    def put(self, **changes):
        self.record_path.write_text(json.dumps({**self.record, **changes}))

    async def test_owned_native_shape_proves_exact_session_without_history_or_protocol(self):
        observer = self.observer()
        with (patch.object(session, '_history_identity', side_effect=AssertionError('history discovery')),
              patch.object(session, 'live_session_for_cwd', side_effect=AssertionError('cwd fallback'))):
            proof = await observer.observe(self.layout)
            again = await observer.observe(self.layout)
        self.assertIs(type(proof), SessionProof)
        self.assertEqual(proof, again)
        self.assertIs(proof.registration, self.registration)
        self.assertEqual((proof.pid, proof.process_start, proof.session_id, proof.session_socket),
                         (self.pid, self.start, 'owned-session', self.sock))
        self.assertEqual(proof.producer_epoch, self.owner.epoch)
        self.assertNotEqual(proof.producer_epoch, UUID(self.registration.work.executor_epoch))
        self.assertRegex(proof.pane, r'^%[0-9]+$')
        self.assertFalse((self.config/'projects').exists())
        self.assertFalse((self.config/'unexpected-frame').exists())
        self.assertFalse(getattr(observer, 'supports_launch_scope', False))
        self.assertFalse(getattr(observer, 'scope_retirement_v1', False))
        with self.assertRaisesRegex(Unavailable, 'reconciliation|retirement'):
            await observer.reconcile(self.registration.work.node_id, str(uuid4()),
                                     self.registration.work.executor_epoch)

    async def test_missing_or_unstable_lifecycle_owner_cannot_mint_authority(self):
        with self.assertRaisesRegex(Unavailable, 'owner'):
            await self.observer(owner=False).observe(self.layout)
        observer = self.observer()
        for value in (None, 'not-an-epoch', self.registration.work.executor_epoch):
            self.owner.epoch = value
            with self.subTest(value=value), self.assertRaises(Unavailable):
                await observer.observe(self.layout)
        async def random_epoch(layout, identity):
            return uuid4()
        self.owner.producer_epoch = random_epoch
        with self.assertRaisesRegex(Unavailable, 'producer|owner'):
            await observer.observe(self.layout)

    async def test_lifecycle_refusal_reason_and_executor_epoch_are_not_producer_authority(self):
        observer = self.observer()
        from mop.node.http_execution import Refused
        async def refused(layout, identity):
            raise Refused('producer attachment was revoked')
        self.owner.producer_epoch = refused
        with self.assertRaisesRegex(Refused, 'attachment was revoked'):
            await observer.observe(self.layout)
        self.owner.producer_epoch = Owner.producer_epoch.__get__(self.owner)
        self.owner.epoch = UUID(self.registration.work.executor_epoch)
        with self.assertRaisesRegex(Unavailable, 'producer'):
            await observer.observe(self.layout)

    async def test_registry_fields_and_native_key_are_exact_not_status_authority(self):
        observer = self.observer()
        proof = await observer.observe(self.layout)
        for fields in ({'pid': True}, {'pid': os.getpid()}, {'sessionId': ''},
                       {'sessionId': '../escape'}, {'cwd': str(self.root)},
                       {'messagingSocketPath': str(self.root/'unregistered.sock')}):
            self.put(**fields)
            with self.subTest(fields=fields), self.assertRaises(Unavailable):
                await observer.observe(self.layout)
        self.put(status='busy', statusUpdatedAt=999999999, waitingFor={'anything': True})
        self.assertEqual(await observer.observe(self.layout), proof)
        original = self.key_path.read_text()
        for value in ({}, {'procStart': int(self.start.split(':')[1])},
                      {'procStart': '0'}, {'procStart': self.start}):
            self.key_path.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(Unavailable):
                await observer.observe(self.layout)
        self.key_path.write_text(original)
        self.assertEqual(await observer.observe(self.layout), proof)

    async def test_clear_changes_conversation_not_process_and_owner_is_explicit(self):
        observer = self.observer()
        old = await observer.observe(self.layout)
        self.put(sessionId='owned-after-clear')
        new = await observer.observe(self.layout)
        self.assertEqual(new.pid, old.pid)
        self.assertEqual(new.process_start, old.process_start)
        self.assertNotEqual(new.session_id, old.session_id)
        self.assertEqual(new.producer_epoch, self.owner.epoch)
        identity = self.owner.calls[-1][1]
        self.assertEqual(identity.session_id, new.session_id)
        self.assertEqual(identity.config, self.config)

    async def test_no_other_registry_socket_is_probed_even_with_same_cwd(self):
        observer = self.observer()
        foreign = self.root/'foreign.sock'
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(foreign))
            listener.listen(8)
            noise = {**self.record, 'pid': os.getpid(), 'sessionId': 'other',
                     'messagingSocketPath': str(foreign), 'statusUpdatedAt': 10**18}
            (self.config/'sessions/foreign.json').write_text(json.dumps(noise))
            await observer.observe(self.layout)
            listener.settimeout(.03)
            with self.assertRaises(TimeoutError):
                listener.accept()
        duplicate = self.config/'sessions/duplicate.json'
        duplicate.write_text(json.dumps(self.record))
        with self.assertRaisesRegex(Unavailable, 'ambiguous|registry'):
            await observer.observe(self.layout)

    async def test_only_registered_tmux_socket_and_single_direct_pane(self):
        observer = self.observer()
        await observer.observe(self.layout)
        subprocess.run([*self.base, 'split-window', '-d', 'sleep 30'], check=True,
                       capture_output=True, timeout=3)
        with self.assertRaisesRegex(Unavailable, 'pane'):
            await observer.observe(self.layout)
        foreign = RegisteredLayout(self.root/'other', self.registration)
        with self.assertRaises(Unavailable):
            await observer.observe(foreign)

    async def test_symlink_socket_key_registry_and_config_are_not_followed(self):
        observer = self.observer()
        for path in (self.key_path, self.record_path, self.sock, self.config):
            saved = path.with_name(path.name+'-saved')
            path.rename(saved)
            path.symlink_to(saved)
            try:
                with self.subTest(path=path.name), self.assertRaises(Unavailable):
                    await observer.observe(self.layout)
            finally:
                path.unlink()
                saved.rename(path)

    async def test_connected_peer_must_be_the_pane_process_not_same_uid_listener(self):
        observer = self.observer()
        self.sock.unlink()
        with socket.socket(socket.AF_UNIX) as replacement:
            replacement.bind(str(self.sock))
            replacement.listen(16)
            with self.assertRaisesRegex(Unavailable, 'peer|socket'):
                await observer.observe(self.layout)

    async def test_mutation_during_owner_await_repeats_all_physical_checks(self):
        observer = self.observer()
        async def changed(layout, identity):
            self.put(sessionId='replaced-during-await')
            return self.owner.epoch
        self.owner.producer_epoch = changed
        with self.assertRaisesRegex(Unavailable, 'changed'):
            await observer.observe(self.layout)
        self.put()
        async def replaced_socket(layout, identity):
            if not hasattr(self, 'replacement'):
                self.sock.unlink()
                self.replacement = socket.socket(socket.AF_UNIX)
                self.addCleanup(self.replacement.close)
                self.replacement.bind(str(self.sock))
                self.replacement.listen(16)
            return self.owner.epoch
        self.owner.producer_epoch = replaced_socket
        with self.assertRaises(Unavailable):
            await observer.observe(self.layout)

    async def signal_fixture(self, number, marker):
        os.kill(self.pid, number)
        deadline = time.monotonic()+2
        while not (self.config/marker).exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue((self.config/marker).exists(), 'owned fixture did not apply mutation')

    async def test_real_process_cwd_change_is_not_hidden_by_stale_registry(self):
        observer = self.observer()
        await observer.observe(self.layout)
        await self.signal_fixture(signal.SIGUSR1, 'cwd-changed')
        self.owner.calls.clear()
        with self.assertRaisesRegex(Unavailable, 'clone|changed'):
            await observer.observe(self.layout)
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(json.loads(self.record_path.read_text()), self.record)

    async def test_real_process_stdin_redirection_is_not_a_valid_pane_tty(self):
        observer = self.observer()
        await observer.observe(self.layout)
        await self.signal_fixture(signal.SIGUSR2, 'stdin-changed')
        with self.assertRaisesRegex(Unavailable, 'TTY'):
            await observer.observe(self.layout)

    async def test_socket_inode_is_rechecked_even_when_pid_uid_and_start_stay_the_same(self):
        observer = self.observer()
        await observer.observe(self.layout)
        real_peer = session.socket_peer
        calls = 0
        def rebound(path):
            nonlocal calls
            peer = real_peer(path)
            if path == str(self.sock):
                calls += 1
                # Смена только inode при прежних PID/UID/start -- не та сессия.
                return peer[0], peer[1], [peer[2][0], peer[2][1]+calls]
            return peer
        with patch.object(session, 'socket_peer', side_effect=rebound):
            with self.assertRaises(Unavailable):
                await observer.observe(self.layout)

    async def test_second_owner_await_and_process_config_are_revalidated(self):
        observer = self.observer()
        calls = 0
        async def changed_on_confirmation(layout, identity):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.key_path.write_text(json.dumps({'procStart': '0'}))
            return self.owner.epoch
        self.owner.producer_epoch = changed_on_confirmation
        with self.assertRaises(Unavailable):
            await observer.observe(self.layout)
        self.key_path.write_text(json.dumps({'procStart': self.start.split(':')[1]}))
        self.key_path.chmod(0o600)
        self.owner.producer_epoch = Owner.producer_epoch.__get__(self.owner)
        actual = session.native_process(self.pid)
        # Ни cwd/native path, ни окружение наблюдателя не заменяют config PID.
        other_env = {**actual['env'], 'CLAUDE_CONFIG_DIR': str(self.root/'missing-config')}
        with patch.object(session, 'native_process', return_value={**actual, 'env': other_env}):
            with self.assertRaises(Unavailable):
                await observer.observe(self.layout)

    async def test_missing_socket_and_dead_native_process_are_not_retirement(self):
        observer = self.observer()
        await observer.observe(self.layout)
        self.sock.unlink()
        with self.assertRaises(Unavailable):
            await observer.observe(self.layout)
        # Respawn старого pane -- не proof того, что все старые effects кончились.
        subprocess.run([*self.base, 'respawn-pane', '-k', 'exec sleep 30'],
                       check=True, capture_output=True, timeout=3)
        with self.assertRaises(Unavailable):
            await observer.observe(self.layout)
        with self.assertRaisesRegex(Unavailable, 'retirement'):
            await observer.reconcile(self.registration.work.node_id, str(uuid4()),
                                     self.registration.work.executor_epoch)

    async def test_boot_namespace_uid_tty_foreground_and_start_are_required(self):
        observer = self.observer()
        await observer.observe(self.layout)
        # Физический happy path выше настоящий; fault injection меняет по одному
        # Linux evidence, чтобы отказ не маскировался отсутствием native registry.
        actual = session.native_process(self.pid)
        for fields in ({'uid': os.getuid()+1}, {'cwd': str(self.root)},
                       {'start': 'different-boot:'+actual['proc_start']},
                       {'proc_start': '0'}, {'namespaces': ()},
                       {'tty': 0}, {'foreground': 0}, {'stdin_device': 0}):
            with self.subTest(fields=fields), patch.object(session, 'native_process',
                    return_value={**actual, **fields}), self.assertRaises(Unavailable):
                await observer.observe(self.layout)


if __name__ == '__main__':
    unittest.main()
