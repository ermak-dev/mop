#!/usr/bin/env python3
"""HYPOTHESIS: Root копирует socket_inode тела, не наблюдая derived socket.
SPEC: настоящий owned Unix peer/inode, exact PID/start/cgroup и original pin.
Остальные proc/systemd ports синтетические: это не kernel/native acceptance.
SOLUTION: Root pin'ит fixed socket/dirs и самостоятельно проверяет kernel peer,
mount view, PID/start/cgroup до/после waits, не читая native registry/key.
RESULT: missing/claimed/foreign/same-UID/replaced/raced negatives RED -> GREEN;
реальные Root/body UID и mount view отдельно проверены scope-probe.
STATUS: FIXED — see #783

HYPOTHESIS (#798): proof ошибочен, пока connect резолвит имя повторно. Body
владеет `r/` и может поменять запись местами: имя в момент connect указывает
на native, а закреплён и возвращён чужой inode; SO_PEERCRED подтверждает
native, но не тот inode, что Root выдал за доказательство.
SOLUTION: connect идёт через magic link уже разрешённого O_PATH
дескриптора того же закреплённого inode; имя больше не authority. Отказ
закрывает fd закрепления, а не держит их до aclose().
RESULT: детерминированный swap перед connect и органический renameat2 race
RED -> GREEN; refused pin освобождает три fd.
STATUS: FIXED — see #798
"""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _host_native import LISTENER, SWAP, NativeProofFixture
from mop.common.producer import ProducerReport
from mop.common.telemetry import SessionScope
from mop.node.host_backend import Process
from mop.node.host_handoff import LaunchWindow
from mop.node.host_scope import Reference, Refused, Unknown
from mop.node import host_scope_systemd as module


class RootSocketProof(NativeProofFixture):
    async def test_missing_derived_socket_cannot_be_claimed_as_root_proof(self):
        self.native = Process(os.getpid(), 1002, self.reference.boot_id)
        self.assertFalse(self.path.exists())
        with self.assertRaises((Refused, Unknown)):
            await self.attach(dict(tmux_pid=self.tmux.pid, tmux_start=self.tmux.start,
                socket_inode=[987654, 123456]))

    async def test_real_owned_socket_peer_positive_and_repeat(self):
        await self.listener()
        first = await self.attach()
        self.assertEqual(first['socket_inode'], self.identity()['socket_inode'])
        self.assertEqual(await self.attach(), first)

    async def test_claimed_inode_cannot_override_observed_inode(self):
        await self.listener()
        value = self.identity() | dict(socket_inode=[987654, 123456])
        with self.assertRaises((Refused, Unknown)):
            await self.attach(value)

    async def test_regular_file_and_symlink_are_not_native_socket(self):
        self.native = Process(os.getpid(), 1002, self.reference.boot_id)
        self.path.write_bytes(b'not a socket')
        with self.assertRaises((Refused, Unknown)):
            await self.attach()
        self.path.unlink()
        self.path.symlink_to(self.home/'absent')
        with self.assertRaises((Refused, Unknown)):
            await self.attach()

    async def test_socket_owner_is_installed_body_not_root_observer_uid(self):
        await self.listener()
        self.adapter.installed.body_uid = os.getuid()+1
        with self.assertRaises((Refused, Unknown)):
            await self.attach()

    async def test_same_uid_wrong_pid_is_not_retained_native(self):
        await self.listener()
        self.native = Process(os.getpid(), 1002, self.reference.boot_id)
        with self.assertRaises((Refused, Unknown)):
            await self.attach()

    async def test_exact_cgroup_and_start_remain_required(self):
        await self.listener()
        for field in ('bad_cgroup', 'changed_start'):
            with self.subTest(field=field):
                setattr(self, field, True)
                with self.assertRaises((Refused, Unknown)):
                    await self.attach()
                setattr(self, field, False)

    async def test_original_socket_cannot_be_replaced_even_with_new_body_claim(self):
        await self.listener()
        await self.attach()
        await self.replace_socket()
        with self.assertRaises((Refused, Unknown)):
            await self.attach()

    async def test_socket_replacement_during_peer_connect_is_refused(self):
        await self.listener()
        loop = asyncio.get_running_loop()
        connect = loop.sock_connect
        async def replace_after_connect(peer, path):
            await connect(peer, path)
            await self.replace_socket()
        with patch.object(loop, 'sock_connect', replace_after_connect):
            with self.assertRaises((Refused, Unknown)):
                await self.attach()
        # Наблюдение invalidates original pin; новая claim не оживляет source.
        with self.assertRaises((Refused, Unknown)):
            await self.attach()

    async def test_runtime_replacement_cannot_redirect_pinned_socket_connect(self):
        await self.listener()
        await self.attach()
        self.runtime.rename(self.home/'old-runtime')
        self.runtime.mkdir(mode=0o700)
        await self.listener()
        with self.assertRaises((Refused, Unknown)):
            await self.attach()

    async def test_other_root_pid_or_user_domain_is_not_silently_translated(self):
        await self.listener()
        for namespace in ('pid', 'user'):
            def readlink(path, *, dir_fd=None):
                if str(path) == '/proc/self/ns/'+namespace:
                    return 'namespace:[other-root-domain]'
                return 'namespace:[1]'
            with self.subTest(namespace=namespace), patch.object(module.os, 'readlink', readlink):
                with self.assertRaisesRegex(Refused, 'domain differs'):
                    await self.attach()

    async def test_socket_replacement_during_root_process_wait_is_refused(self):
        await self.listener()
        await self.attach()
        count = 0
        async def runner(reference):
            nonlocal count
            count += 1
            if count == 1:
                await self.replace_socket()
            return self.runner
        self.adapter.runner_process.side_effect = runner
        with self.assertRaises((Refused, Unknown)):
            await self.attach()


class NativeConnectBinding(NativeProofFixture):
    """#798: connect по имени резолвится заново, поэтому body подменяет запись
    `r/` между check() и connect() и получает чужой inode в доказательство."""

    async def listen(self, path):
        child = subprocess.Popen([sys.executable, '-I', '-u', '-c', LISTENER, str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'PATH': os.defpath})
        self.children.append(child)
        self.assertEqual(await asyncio.to_thread(child.stdout.readline), b'ready\n')
        return child

    async def decoy_pair(self):
        """Fake listener на fixed name и настоящий native на r/native.real."""
        await self.listen(self.path)
        real = self.runtime/'native.real'
        native_child = await self.listen(real)
        self.native = Process(native_child.pid, 1002, self.reference.boot_id)
        return real

    async def test_connect_binds_the_pinned_inode_not_the_name(self):
        # Детерминированное сведение (swap_e2e): body-выбранный fake закреплён,
        # native подменяется на fixed name непосредственно перед sock_connect.
        real = await self.decoy_pair()
        fake_inode = [self.path.lstat().st_dev, self.path.lstat().st_ino]
        real_inode = [real.lstat().st_dev, real.lstat().st_ino]
        self.assertNotEqual(fake_inode, real_inode)
        loop = asyncio.get_running_loop()
        connect = loop.sock_connect
        async def swapped(peer, name):
            if str(name).endswith('/session.sock'):
                os.rename(self.path, self.runtime/'fake.hold')
                os.rename(real, self.path)
            try:
                return await connect(peer, name)
            finally:
                if str(name).endswith('/session.sock'):
                    os.rename(self.path, real)
                    os.rename(self.runtime/'fake.hold', self.path)
        with patch.object(loop, 'sock_connect', swapped):
            # Явный fake claim: root обязан отказать, а не вернуть fake inode,
            # к которому native не имеет отношения.
            with self.assertRaises((Refused, Unknown)):
                await self.attach(dict(tmux_pid=self.tmux.pid,
                    tmux_start=self.tmux.start, socket_inode=fake_inode))

    async def test_organic_rename_exchange_never_accepts_a_foreign_inode(self):
        # Органическая гонка (swap_live): pin снят, пока на fixed name стоял fake;
        # отдельный процесс крутит renameat2(RENAME_EXCHANGE) с native.
        real = await self.decoy_pair()
        pin = module._NativeSocket(self.adapter.installed, self.reference, self.native)
        self.assertEqual(pin.inode, [self.path.lstat().st_dev, self.path.lstat().st_ino])
        swapper = subprocess.Popen([sys.executable, '-I', '-c', SWAP, str(self.path), str(real)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={'PATH': os.defpath})
        self.children.append(swapper)
        accepted, refused = [], 0
        try:
            for _ in range(600):
                try:
                    await pin.observe()
                except (Refused, Unknown):
                    refused += 1
                    continue
                accepted.append(pin.inode)
                if len(accepted) >= 3:
                    break
                await asyncio.sleep(0)
        finally:
            swapper.terminate()
            await asyncio.to_thread(swapper.wait, 5)
            pin.close()
        # Pin всегда на fake: ни одно принятое наблюдение недопустимо.
        self.assertGreater(refused, 0)
        self.assertEqual(accepted, [])

    async def test_refused_pin_does_not_retain_three_descriptors(self):
        # pin_leak: отказ не оставляет home/runtime/socket fd до aclose().
        await self.listen(self.path)
        self.native = Process(os.getpid(), 1002, self.reference.boot_id)
        before = len(os.listdir('/proc/self/fd'))
        with self.assertRaises((Refused, Unknown)):
            await self.attach(dict(tmux_pid=self.tmux.pid, tmux_start=self.tmux.start,
                socket_inode=[987654, 123456]))
        pins = list(self.adapter._native_sockets.values())
        self.assertEqual([pin.failed for pin in pins], [True], 'failed pin must stay refused')
        self.assertEqual([pin.home for pin in pins], [None], 'failed pin must release its dirs')
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


if __name__ == '__main__':
    unittest.main()
