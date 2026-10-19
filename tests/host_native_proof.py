#!/usr/bin/env python3
"""HYPOTHESIS: Root копирует socket_inode тела, не наблюдая derived socket.
SPEC: настоящий owned Unix peer/inode, exact PID/start/cgroup и original pin.
Остальные proc/systemd ports синтетические: это не kernel/native acceptance.
SOLUTION: Root pin'ит fixed socket/dirs и самостоятельно проверяет kernel peer,
mount view, PID/start/cgroup до/после waits, не читая native registry/key.
RESULT: missing/claimed/foreign/same-UID/replaced/raced negatives RED -> GREEN;
реальные Root/body UID и mount view отдельно проверены scope-probe.
STATUS: FIXED — see #783
"""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
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
from host_native import model_delivery
from mop.common.producer import ProducerReport
from mop.common.telemetry import SessionScope
from mop.node.host_backend import Process
from mop.node.host_scope import Reference, Refused, Unknown
from mop.node import host_scope_systemd as module


# Только наш non-native listener; не читает registry/key и не посылает данные.
LISTENER = '''import os,select,socket,sys
def listen():
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
 s.bind(sys.argv[1]);os.chmod(sys.argv[1],0o600);s.listen(16)
 print("ready",flush=True)
 return s
s=listen()
while True:
 ready,_,_=select.select([s,sys.stdin],[],[])
 if sys.stdin in ready:
  if not sys.stdin.readline(): break
  os.unlink(sys.argv[1]);s.close();s=listen()
 elif s in ready:
  c,_=s.accept();c.close()
'''


class RootSocketProof(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='np-', dir='/tmp')
        self.home = Path(self.temp.name)
        self.home.chmod(0o700)
        self.runtime = self.home/'r'
        self.runtime.mkdir(mode=0o700)
        self.path = self.runtime/'session.sock'
        self.children = []
        self.admission, self.scope, delivery = model_delivery()
        self.reference = Reference(self.scope, uuid4().hex, str(uuid4()))
        self.tools = {name: Path('/opt/tools')/name for name in ('native', 'tmux', 'python')}
        self.pins = SimpleNamespace(bootstrap_model='fixture-bootstrap')
        self.adapter = module.SystemdCgroupAdapter.__new__(module.SystemdCgroupAdapter)
        self.adapter._native_sockets = {}
        self.adapter.installed = SimpleNamespace(body_uid=os.getuid(), body_gid=os.getgid(),
            body=lambda scope: self.home, runner=Path('/opt/runtime/mop/node/host_body.py'))
        self.runner = Process(101, 1001, self.reference.boot_id)
        self.tmux = Process(103, 1003, self.reference.boot_id)
        self.native = None
        self.deadline = (datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()
        self.session = str(uuid4())
        w = self.admission.work
        self.report_scope = SessionScope(*(UUID(getattr(w, key)) for key in
            ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')), self.session, uuid4())
        self.delivery = delivery
        self.fds = {}
        self.bad_cgroup = False
        self.changed_start = False
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        original_open = module.open_directory
        self.original_readlink = os.readlink
        def open_proc(path, **kwargs):
            if Path(path).parent == Path('/proc') and Path(path).name.isdigit():
                pid = int(Path(path).name)
                # Native peer имеет настоящий /proc/root view; systemd порты fake.
                target = path if self.native is not None and pid == self.native.pid else self.home
                fd = original_open(target)
                self.fds[fd] = pid
                return fd
            return original_open(path, **kwargs)
        def cgroup(scope):
            fd = os.open(self.home, os.O_RDONLY|os.O_DIRECTORY)
            self.fds[fd] = 0
            return fd
        self.adapter._cgroup = cgroup
        self.adapter.runner_process = AsyncMock(return_value=self.runner)
        def read_bytes(fd, name, **kwargs):
            if name == 'status':
                uid, gid = self.adapter.installed.body_uid, self.adapter.installed.body_gid
                return (f'Uid:\t{uid} {uid} {uid} {uid}\n'
                    f'Gid:\t{gid} {gid} {gid} {gid}\n'
                    'Groups:\t\nNoNewPrivs:\t1\nCapInh:\t0\nCapPrm:\t0\nCapEff:\t0\nCapAmb:\t0\n').encode()
            if name == 'cmdline':
                argv = self.argv() if self.fds[fd] == self.native.pid else [str(self.tools['tmux']), '-S', str(self.runtime/'tmux'), 'new-session']
                return b'\0'.join(part.encode() for part in argv)+b'\0'
            raise AssertionError(name)
        def read_at(fd, name, **kwargs):
            if name == 'cgroup':
                return '0::'+(self.scope.cgroup if not self.bad_cgroup else '/foreign')
            if name == 'cgroup.procs':
                return '\n'.join(str(p.pid) for p in (self.runner, self.native, self.tmux))+'\n'
            raise AssertionError(name)
        def readlink(name, *, dir_fd=None):
            if str(name).startswith('ns/') or '/ns/' in str(name):
                return 'namespace:[1]'
            if name == 'exe':
                return str(self.tools['native' if self.fds[dir_fd] == self.native.pid else 'tmux'])
            if name == 'cwd':
                return str(self.home/'repo')
            return self.original_readlink(name, dir_fd=dir_fd)
        def process(pid):
            for p in (self.runner, self.native, self.tmux):
                if p is not None and p.pid == pid:
                    return Process(p.pid, p.start+1, p.boot_id) if self.changed_start and p == self.native else p
            raise ProcessLookupError(pid)
        for name, value in (('open_directory', open_proc), ('read_bytes_at', read_bytes), ('read_at', read_at)):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(module.os, 'readlink', readlink))
        self.stack.enter_context(patch.object(Process, 'read', side_effect=process))

    async def asyncTearDown(self):
        for record in getattr(self.adapter, '_native_sockets', {}).values():
            record.close()
        for child in self.children:
            child.terminate()
            await asyncio.to_thread(child.wait, 5)
            child.stdout.close()
            child.stdin.close()
        self.temp.cleanup()

    async def listener(self):
        child = subprocess.Popen([sys.executable, '-I', '-u', '-c', LISTENER, str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={'PATH': os.defpath})
        self.children.append(child)
        self.assertEqual(await asyncio.to_thread(child.stdout.readline), b'ready\n')
        # Synthetic process port: PID — настоящий listener, start/boot — явные facts.
        self.native = Process(child.pid, 1002, self.reference.boot_id)
        return child

    async def replace_socket(self):
        child = self.children[-1]
        child.stdin.write(b'replace\n')
        child.stdin.flush()
        self.assertEqual(await asyncio.to_thread(child.stdout.readline), b'ready\n')

    def argv(self):
        mcp = dict(mcpServers=dict(mop=dict(type='stdio', command=str(self.tools['python']),
            args=['-I', '-S', '-B', '/opt/runtime/mop/client/body_entry.py', '--scope', self.scope.unit,
                  '--deadline', self.deadline])))
        return [str(self.tools['native']), '--model', self.pins.bootstrap_model, '--session-id', self.session,
            '--messaging-socket-path', str(self.path), '--permission-mode', 'default',
            '--settings', str(self.runtime/'settings.json'), '--setting-sources', 'user',
            '--strict-mcp-config', '--mcp-config', json.dumps(mcp)]

    def report(self):
        return ProducerReport(self.report_scope, UUID(self.delivery['bootstrap_revision']),
            self.delivery['bootstrap_digest'], self.delivery['image_digest'], self.delivery['display_name'],
            '%1', self.native.pid, self.native.boot_id+':'+str(self.native.start))

    def identity(self):
        inode = self.path.lstat()
        return dict(tmux_pid=self.tmux.pid, tmux_start=self.tmux.start,
            socket_inode=[inode.st_dev, inode.st_ino])

    async def attach(self, identity=None):
        return await self.adapter.native_attachment(self.reference, self.report(),
            identity or self.identity(), self.tools, self.pins)

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


if __name__ == '__main__':
    unittest.main()
