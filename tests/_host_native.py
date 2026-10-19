"""Общие fixture'ы HOST native проверок (#782/#784/#798/#803).

Не проверка: `mop dev test` и hermetic пропускают `_*.py`. До #803
`model_delivery` жил в host_native.py, `NativeProofFixture` -- в
host_native_proof.py, и проверки импортировали друг друга; tests_lib это
запрещает (#269). Общее живёт здесь и не тянет назад в проверки.

`NativeProofFixture` -- настоящий owned Unix peer/inode, exact PID/start/cgroup
и original pin; остальные proc/systemd порты синтетические (это не kernel/native
acceptance). Фикстура остаётся ровно той, что была в host_native_proof.py:
перенос не меняет ни одного утверждения #783/#784/#798.
"""
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _host_inputs_data import selected
from mop.common.producer import ProducerReport
from mop.common.telemetry import SessionScope
from mop.node.host_backend import Process
from mop.node.host_handoff import LaunchWindow
from mop.node.host_scope import Reference
from mop.node import host_scope_systemd as module


# Органическая гонка: отдельный процесс крутит renameat2(RENAME_EXCHANGE).
SWAP = '''import ctypes,os,sys
lib=ctypes.CDLL(None,use_errno=True);rename=lib.renameat2
rename.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
a,b=sys.argv[1].encode(),sys.argv[2].encode()
while True:
 if rename(-100,a,-100,b,2)!=0 and ctypes.get_errno()!=2: raise SystemExit(1)
'''

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


def model_delivery():
    """Delivery-документ с model consumers: основа model/submission проверок."""
    import base64
    import hashlib
    from _body_fixture import bundle
    from mop.node.host_inputs import canonical_json
    a, scope, value = selected('native_exec')
    role = bundle()
    a = replace(a, work=replace(a.work, role=role.role, bundle_digest=role.digest))
    value['work'] = a.work.wire()
    value['role'] = dict(manifest=role.manifest.to_dict(), prompt=base64.b64encode(role.prompt).decode())
    config_id, credential_id = str(uuid4()), str(uuid4())
    config = dict(schema_version=1, profile_version=1, provider_profile='owned-fixture-v1',
        credential_version=credential_id, proof_strategy='bootstrap-to-desired',
        bootstrap_model='fixture-bootstrap', desired_model='fixture-desired')
    raw = canonical_json(config)
    value['selection'].update(desired_model=config['desired_model'], model_config_digest=hashlib.sha256(raw).hexdigest(),
        consumers=[dict(purpose='model_config', version_id=config_id),
                   dict(purpose='model_credential', version_id=credential_id)])
    credential = dict(schema_version=1, provider_profile='owned-fixture-v1', token='fixture-secret-not-for-repr')
    value['consumers'] = [dict(purpose='model_config', version_id=config_id, name='NODE_OPTIONS',
        kind='file', value=base64.b64encode(raw).decode()),
        dict(purpose='model_credential', version_id=credential_id, name='../../arbitrary',
             kind='credential', value=base64.b64encode(canonical_json(credential)).decode())]
    return a, scope, value


class NativeProofFixture(unittest.IsolatedAsyncioTestCase):
    """HYPOTHESIS: Root копирует socket_inode тела, не наблюдая derived socket.

    SPEC: настоящий owned Unix peer/inode, exact PID/start/cgroup и original pin.
    SOLUTION: Root pin'ит fixed socket/dirs и самостоятельно проверяет kernel
    peer, mount view, PID/start/cgroup до/после waits, не читая native registry.
    RESULT: missing/claimed/foreign/same-UID/replaced/raced negatives RED ->
    GREEN; реальные Root/body UID и mount view отдельно проверены scope-probe.
    STATUS: FIXED -- see #783
    """

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
        # Явный Root retained-state port (#784): original window захвачен до
        # launch; argv обязан нести именно его deadline, а не любой canonical.
        self.window = LaunchWindow(datetime.now(timezone.utc), self.admission.work.not_after)
        self.adapter._native_launches = {self.reference: (self.admission, self.window)}
        self.adapter.installed = SimpleNamespace(body_uid=os.getuid(), body_gid=os.getgid(),
            body=lambda scope: self.home, runner=Path('/opt/runtime/mop/node/host_body.py'))
        self.runner = Process(101, 1001, self.reference.boot_id)
        self.tmux = Process(103, 1003, self.reference.boot_id)
        self.native = None
        self.deadline = self.window.deadline.isoformat()
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
            for stream in (child.stdout, child.stdin):
                if stream is not None:
                    stream.close()
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
