"""Один fixed normal native и SessionRuntime внутри installed body MainPID.

Нет node bearer, imports client, arbitrary command/env или paid probe turn.
Frontend не владеет этим объектом: root control и cgroup живут дольше polling.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
from uuid import uuid4

from mop.common.management_context import body_deadline
from mop.driver.launch import RegisteredLayout, Registration
from mop.node.body_launch import SessionRuntime
from mop.node.host_backend import Process
from mop.node.host_inputs import canonical_json
from mop.node.host_native import ModelInputs, ProviderProfile
from mop.node.host_native_lifetime import ProxyRegistration, ProxyTelemetry
from mop.node.host_scope import canonical, decode, encode, exact
from mop.node.host_scope_supervisor import peer_credentials, read_frame
from mop.node.http_execution import Refused, Unavailable


def installed_profile(release):
    from mop.common.installed_content import json_value, read_immutable
    value = json_value(read_immutable(Path(release)/'host-native.json', limit=4096))
    exact(value, ('schema_version', 'provider'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise Refused('unsupported fixed native provider profile')
    p = value['provider']
    exact(p, ('identifier', 'origin', 'ca', 'auth'))
    return ProviderProfile(p['identifier'], p['origin'], Path(p['ca']), p['auth'])


class NativeChannel:
    """Единственный reader root channel; finite correlator, не generic RPC."""
    def __init__(self, broker):
        self.broker = broker
        self.pending = {}
        self.writer_lock = asyncio.Lock()
        self.reader_task = None
        self.closed = False

    def check(self):
        b = self.broker
        if (self.closed or peer_credentials(b.writer.get_extra_info('socket')) != b.peer
                or b.peer[1] != 0 or Process.read(b.peer[0]) != b.process):
            raise Refused('native lifetime lost its pinned root control process')

    async def send(self, value):
        self.check()
        async with self.writer_lock:
            self.broker.writer.write(encode(value)+b'\n')
            await self.broker.writer.drain()
        self.check()

    async def request(self, operation, payload):
        if operation not in ('checkpoint', 'native_launched', 'register_producer', 'ingest_telemetry'):
            raise Refused('body native operation is not enabled')
        self.check()
        if len(self.pending) >= 4 or len(canonical_json(payload)) > 16384:
            raise Refused('bounded native body mailbox capacity exceeded')
        identifier = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send(dict(schema_version=1, action='native_request', id=identifier,
                operation=operation, payload=payload))
            timeout = 60 if operation == 'checkpoint' else 5
            async with asyncio.timeout(timeout):
                return await asyncio.shield(future)
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()

    async def read(self):
        try:
            while True:
                self.check()
                value = decode(await read_frame(self.broker.reader))
                self.check()
                exact(value, ('schema_version', 'action', 'id', 'answer', 'error'))
                if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['action'] != 'native_reply':
                    raise Refused('fixed native root reply required')
                canonical(value['id'])
                future = self.pending.get(value['id'])
                # Просроченный ответ не создаёт capability и не оживляет attempt.
                if future is None or future.done():
                    continue
                if value['error'] is not None:
                    reason = value['error']
                    if type(reason) is not str or not 0 < len(reason) <= 1024 or not reason.isprintable():
                        raise Refused('invalid native root refusal')
                    future.set_exception(Unavailable(reason))
                else:
                    future.set_result(value['answer'])
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(Unavailable('native root lifetime channel closed'))

    async def aclose(self):
        self.closed = True
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)


class NativeBody:
    def __init__(self, broker, submission, delivery):
        self.broker, self.submission = broker, submission
        self.profile = installed_profile(broker.config.installed.runner.parents[2])
        self.inputs = ModelInputs.parse(delivery, self.profile)
        if self.inputs.pins() != submission.pins:
            raise Refused('body native inputs differ from admitted immutable model pins')
        p, w = submission.projection, submission.admission.work
        self.registration = Registration(w, p.bootstrap_revision, p.bootstrap_digest, p.image_digest, p.display_name)
        self.layout = RegisteredLayout(broker.body, self.registration, profile='host-native-v1')
        self.channel = NativeChannel(broker)
        self.telemetry = ProxyTelemetry(self.channel)
        self.producer = ProxyRegistration(self.channel, self.registration, self.layout)
        self.runtime = SessionRuntime(self.producer, self.telemetry)
        self.launcher_deadline = self.session_id = self.environment = None
        self.started = False
        self.cleanup = None

    async def checkpoint(self, effect, sequence):
        from mop.node.host_inputs import validate_admission
        validate_admission(self.submission.admission)
        result = await self.channel.request('checkpoint', dict(effect=effect, sequence=sequence))
        expected = dict(approved=effect, sequence=sequence)
        if effect == 'native-input':
            exact(result, (*expected, 'launcher_deadline'))
            if self.launcher_deadline is not None:
                raise Refused('original root launcher deadline cannot be replaced')
            try:
                deadline = body_deadline(result['launcher_deadline'])
            except ValueError:
                raise Refused('original root launcher deadline is invalid or expired') from None
            if deadline > self.submission.admission.work.not_after:
                raise Refused('root launcher deadline exceeds original intent')
            expected['launcher_deadline'] = deadline.isoformat()
        if result != expected:
            raise Refused('native body checkpoint approval changed')
        validate_admission(self.submission.admission)
        if effect == 'native-input':
            self.launcher_deadline = deadline
        if effect == 'native-launch':
            if self.launcher_deadline is None:
                raise Refused('original root launcher deadline is absent')
            try:
                body_deadline(self.launcher_deadline.isoformat())
            except ValueError:
                raise Refused('original root launcher deadline expired before native launch') from None
        self.broker.effect = effect

    async def tmux(self, *args):
        b = self.broker
        return await b.command([str(b.manifest.tools['tmux']), '-S', str(self.layout.tmux_socket), *args],
            env=self.environment, cwd=self.layout.clone)

    async def launch(self):
        if self.started:
            raise Refused('native body launch cannot be repeated')
        self.started = True
        b, l = self.broker, self.layout
        b.manifest.require_native_release(b.config.installed.runner.parents[2])
        await self.checkpoint('native-input', 0)
        l.runtime.mkdir(mode=0o700)
        l.management.mkdir(mode=0o700)
        config = l.runtime/'c'
        config.mkdir(mode=0o700)
        home = l.runtime/'home'
        home.mkdir(mode=0o700)
        self.session_id = str(uuid4())
        hook = shlex.join([str(b.manifest.tools['python']), '-I', str(b.manifest.tools['hook']), 'hook'])
        settings = dict(permissions=dict(defaultMode='default'), hooks={
            event: [dict(hooks=[dict(type='command', command=hook, timeout=2)])]
            for event in ('SessionStart', 'UserPromptSubmit', 'Stop', 'StopFailure', 'PostModelSwitch')})
        b.write(l.runtime/'settings.json', canonical_json(settings))
        # Никакой project trust/API-key dialog autoaccept или statusline hints.
        entry = b.config.installed.runner.parents[2]/'mop/client/body_entry.py'
        mcp = dict(mcpServers=dict(mop=dict(type='stdio', command=str(b.manifest.tools['python']),
            args=['-I', '-S', '-B', str(entry), '--scope', b.scope.unit,
                  '--deadline', self.launcher_deadline.isoformat()])))
        self.environment = dict(HOME=str(home), CLAUDE_CONFIG_DIR=str(config),
            PATH=str(b.manifest.tools['tmux'].parent)+':'+str(b.manifest.tools['git'].parent)+':/usr/bin:/bin',
            LANG='C.UTF-8', LC_ALL='C.UTF-8', TERM='xterm-256color', SHELL='/bin/sh',
            **self.inputs.environment(self.profile), **self.runtime.prepare(l, path=l.hook_socket))
        command = [str(b.manifest.tools['native']), '--model', self.inputs.bootstrap_model,
            '--session-id', self.session_id, '--messaging-socket-path', str(l.runtime/'session.sock'),
            '--permission-mode', 'default', '--settings', str(l.runtime/'settings.json'),
            '--setting-sources', 'user', '--strict-mcp-config', '--mcp-config',
            json.dumps(mcp, separators=(',', ':'))]
        b.write(l.launcher, ('#!/bin/sh\nexec '+shlex.join(command)+'\n').encode())
        os.chmod(l.launcher, 0o700)
        await self.checkpoint('native-launch', 1)
        # Нормальный интерактивный режим; не --bare/-p и не provider turn.
        await self.tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'native', '-c', str(l.clone),
            '-x', '120', '-y', '40', 'exec '+shlex.join(command))
        raw = await self.tmux('display-message', '-p', '-t', 'native:0.0', '#{pane_pid}|#{pid}')
        parts = raw.strip().split(b'|')
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise Refused('fixed launched native/tmux process report unavailable')
        native_pid, tmux_pid = map(int, parts)
        async with asyncio.timeout(3):
            while os.readlink('/proc/'+str(native_pid)+'/exe') != str(b.manifest.tools['native']):
                await asyncio.sleep(.01)
        native, tmux = Process.read(native_pid), Process.read(tmux_pid)
        retained = await self.channel.request('native_launched', dict(pid=native.pid,
            process_start=native.boot_id+':'+str(native.start), session_id=self.session_id,
            tmux_pid=tmux.pid, tmux_start=tmux.start))
        if retained != dict(retained=True):
            raise Refused('root did not retain original actual native launch')

    async def attach_and_switch(self):
        from mop.node.native_observer import _snapshot
        from mop.node.http_execution import Unavailable
        # Никаких retry attach/remint: ждём первую доказуемую physical identity,
        # затем единственный SessionRuntime.attach владеет ACK/HookBridge.
        while True:
            try:
                identity = await asyncio.to_thread(_snapshot, self.layout)
                if identity.session_id != self.session_id:
                    raise Refused('fixed launched native session id changed')
                break
            except Unavailable:
                await self.refuse_dialog()
                await asyncio.sleep(.1)
        await self.checkpoint('producer-attach', 2)
        await self.runtime.attach()
        await self.runtime._current()
        await self.checkpoint('desired-switch', 3)
        await self.refuse_dialog()
        await self.tmux('send-keys', '-t', 'native:0.0', '-l', '/model '+self.inputs.desired_model)
        await self.tmux('send-keys', '-t', 'native:0.0', 'Enter')
        confirmed = False
        while True:
            await self.runtime._current()
            row = self.telemetry.committed
            if (row is not None and row.event == 'PostModelSwitch' and row.model == self.inputs.desired_model
                    and row.dropped == 0):
                return row.sequence
            screen = await self.refuse_dialog()
            if b'Switch model?' in screen and not confirmed:
                # Это именно approved bootstrap-to-desired strategy, не trust.
                await self.tmux('send-keys', '-t', 'native:0.0', 'Enter')
                confirmed = True
            await asyncio.sleep(.05)

    async def refuse_dialog(self):
        screen = await self.tmux('capture-pane', '-p', '-t', 'native:0.0')
        lower = screen.lower()
        if any(text in lower for text in (b'trust this folder', b'trust this directory',
                b'log in', b'select login method', b'choose the text style', b'do you want to use this api key')):
            raise Refused('native startup requires explicit onboarding/trust approval; no automatic bypass')
        return screen

    async def retain(self):
        # Даже без frontend/hooks исчезнувший native инвалидирует producer.
        # Timeout ожидания не отменяет единственный reader/root channel.
        while True:
            await self.runtime._current()
            done, _ = await asyncio.wait((self.channel.reader_task,), timeout=.25)
            if done:
                return await self.channel.reader_task

    async def run(self):
        self.channel.reader_task = asyncio.create_task(self.channel.read())
        try:
            await self.launch()
            sequence = await self.attach_and_switch()
            await self.channel.send(dict(schema_version=1, action='native_complete',
                attempt_id=self.submission.admission.attempt_id, committed_sequence=str(sequence)))
            # Lifetime не завершается вместе с native phase/result/frontend.
            await self.retain()
        finally:
            if self.cleanup is None:
                self.cleanup = asyncio.create_task(self._close())
            await asyncio.shield(self.cleanup)

    async def _close(self):
        await self.runtime.aclose()
        await self.channel.aclose()
