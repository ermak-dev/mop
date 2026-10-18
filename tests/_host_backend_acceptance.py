"""Actual root/nonroot Unix lifetime gate только внутри private systemd #745.

HTTP/SQL здесь явно registry double; actual PG/HTTPS — отдельный gate.
Body — containment fixture runner, не vendor-native.
HYPOTHESIS: PID frontend ошибочно заменяет lifetime physical backend.
SOLUTION: installed root entry + peer-verified bridge, retained refs/fences.
RESULT: actual missing-startup RED; shell ambient-import RED; actual GREEN.
STATUS: FIXED — see #745
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
import signal
import sys
from uuid import uuid4

sys.path.insert(0, '/opt/fixture')
NODE_UID, BODY_UID = 21071, 21072
INSTALLATION = 'host-backend-fixture'
SOCKET = Path('/run/mop-host/backend.sock')
ROOT_ENTRY = Path('/opt/fixture/bin/mop')
PHYSICAL = Path('/etc/mop-host-physical.json')


async def frontend(value):
    os.setgroups([])
    os.setgid(NODE_UID)
    os.setuid(NODE_UID)
    from mop.node.host_backend import Identity
    from mop.node.host_backend_client import BackendClient
    from mop.node.host_scope import Refused, Unknown, Reference, Scope
    from mop.node.host_scope_client import Client
    from mop.node.http_execution import EffectGuard, Work, Unavailable
    from mop.node import native_startup
    assert 'backend' in inspect.signature(native_startup.start).parameters, \
        'native startup does not consume the concrete verified HOST backend'
    calls = []
    discovery_entered, close_entered, release_close = (asyncio.Event() for _ in range(3))
    class Registry:
        epoch = value['epoch']
        executor = value['executor']
        empty = value['empty']
        async def host_executor_state(self, node):
            calls.append('host-discovery')
            if value.get('cancel'):
                discovery_entered.set()
                await asyncio.Event().wait()
            return dict(schema_version=1, installation_id=INSTALLATION,
                epoch=self.epoch, node_id=node, executor_epoch=self.executor, empty=self.empty)
        async def host_bound(self, node, epoch, previous, new):
            calls.append('host-CAS')
            assert previous == self.executor and self.empty
            self.executor = new
        async def restarted(self, *args):
            raise AssertionError('HOST frontend called generic restarted')
        async def executor_state(self, *args):
            raise AssertionError('HOST frontend lost concrete startup discovery')
        async def aclose(self):
            calls.append('http-close')
    class Bridge:
        async def reconcile(self, *args):
            raise AssertionError('frontend restart reconciled/retired the physical backend')
        async def aclose(self):
            calls.append('bridge-close')
            if value.get('cancel'):
                close_entered.set()
                await release_close.wait()
    backend = BackendClient(SOCKET, node_uid=NODE_UID,
        identity=Identity(value.get('installation', INSTALLATION), value['node']))
    if value.get('cancel'):
        task = asyncio.create_task(native_startup.start(Registry(), Bridge(), installation_id=INSTALLATION,
            node_id=value['node'], backend=backend))
        async with asyncio.timeout(5.):
            await discovery_entered.wait()
            task.cancel()
            await close_entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), 'repeated cancellation lost the cleanup owner'
            release_close.set()
            try:
                await task
                raise AssertionError('cancelled startup became ready')
            except asyncio.CancelledError:
                pass
        assert calls == ['host-discovery', 'bridge-close', 'http-close'], calls
        print(json.dumps(dict(status='cancelled-and-cleaned', calls=calls)), flush=True)
        return
    runtime = None
    try:
        runtime = await native_startup.start(Registry(), Bridge(), installation_id=INSTALLATION,
            node_id=value['node'], backend=backend)
        if value.get('expect_refusal'):
            raise AssertionError('unsafe HOST frontend unexpectedly resumed')
        if value.get('reference'):
            reference = Reference.parse(value['reference'])
            s = reference.scope
        else:
            s = Scope(value['epoch'], value['node'], runtime.executor_epoch,
                value['target'], value['incarnation'], value['launch'])
        w = Work.parse(dict(schema_version=1, **{k: getattr(s, k) for k in
            ('epoch', 'node_id', 'executor_epoch', 'target_id', 'incarnation_id')},
            operation_id=str(uuid4()), step_id=str(uuid4()), grant_id=str(uuid4()),
            owner_id='1', generation='1', revision='1', role='fixture', bundle_digest='a'*64,
            action='restart_session', not_after=(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()))
        scopes = Client(SOCKET, node_uid=NODE_UID, work=w)
        async def fixture_admission():
            pass
        guard = EffectGuard(fixture_admission, lambda: None)
        if value.get('reference'):
            snapshot = await scopes.inspect(reference, guard)
        else:
            snapshot = await scopes.start(s, guard)
        # Root должен отказать и authenticated node UID с неверными координатами.
        from mop.node.host_scope_supervisor import read_frame
        from mop.node.host_scope import Request, decode
        for change in ({'node_id': str(uuid4())}, {'epoch': str(uuid4())},
                       {'executor_epoch': str(uuid4())}):
            reader, writer = await asyncio.open_unix_connection(str(SOCKET))
            try:
                writer.write(Request('start', replace(s, **change), str(uuid4())).encode()+b'\n')
                await writer.drain()
                reply = decode(await read_frame(reader))
                assert reply['status'] == 'refused' and reply['snapshot'] is None, reply
            finally:
                writer.close()
                await writer.wait_closed()
        print(json.dumps(dict(frontend_pid=os.getpid(), executor_epoch=runtime.executor_epoch,
            snapshot=snapshot.wire(), calls=calls)), flush=True)
    except (Refused, Unknown, Unavailable) as error:
        if not value.get('expect_refusal'):
            raise
        assert 'host-CAS' not in calls, calls
        print(json.dumps(dict(status='refused', reason=str(error), calls=calls)), flush=True)
    finally:
        if runtime is not None:
            await runtime.aclose()


async def fake_peer(path):
    os.setgroups([])
    os.setgid(NODE_UID)
    os.setuid(NODE_UID)
    async def reject(reader, writer):
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_unix_server(reject, path=path)
    os.chmod(path, 0o600)
    print('ready', flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
        await server.wait_closed()


async def wrong_peer(value):
    os.setgroups([])
    os.setgid(NODE_UID)
    os.setuid(NODE_UID)
    from mop.node.host_backend import Identity
    from mop.node.host_backend_client import BackendClient
    from mop.node.host_scope import Refused
    backend = BackendClient(Path(value['path']), node_uid=NODE_UID,
        identity=Identity(INSTALLATION, value['node']))
    try:
        async with backend.connect():
            raise AssertionError('nonroot peer became a verified root authority')
    except Refused as error:
        assert 'root Unix peer' in str(error), str(error)
        print(json.dumps(dict(status='refused', reason=str(error))), flush=True)


async def accept(installed, adapter, command):
    from mop.node.host_scope import Reference, Unknown
    from mop.node.host_scope_systemd import parse_proc_start
    assert os.geteuid() == 0 and Path('/proc/1/comm').read_text().strip() == 'systemd'
    node, epoch, previous, target, incarnation, launch = (str(uuid4()) for _ in range(6))
    physical = dict(schema_version=1, installation_id=INSTALLATION, node_id=node,
        node_uid=NODE_UID, body_uid=BODY_UID, body_gid=BODY_UID,
        runner=str(installed.runner), body_root=str(installed.body_root),
        node_private=str(installed.node_private), socket=str(SOCKET))
    PHYSICAL.write_text(json.dumps(physical))
    PHYSICAL.chmod(0o600)
    body = installed.body_root/launch
    body.mkdir(mode=0o700)
    os.chown(body, BODY_UID, BODY_UID)
    (body/'consumer.json').write_text(json.dumps(dict(schema_version=1, fixture='double-fork')))
    (body/'consumer.json').chmod(0o600)
    os.chown(body/'consumer.json', BODY_UID, BODY_UID)
    value = dict(node=node, epoch=epoch, executor=previous, empty=True,
                 target=target, incarnation=incarnation, launch=launch)
    supervisor = None
    reference = None
    async def root_start():
        # Hostile ambient Python paths должны остаться вне privileged imports.
        hostile = Path('/opt/hostile-python')
        hostile.mkdir(exist_ok=True)
        (hostile/'sitecustomize.py').write_text('raise AssertionError("ambient site imported")')
        (hostile/'mop.py').write_text('raise AssertionError("ambient mop imported")')
        (hostile/'bashenv').write_text('exit 85\n')
        for tool in ('dirname', 'readlink'):
            (hostile/tool).write_text('#!/bin/sh\nexit 86\n')
            (hostile/tool).chmod(0o755)
        env = dict(PATH=str(hostile)+':/usr/bin:/bin', BASH_ENV=str(hostile/'bashenv'),
                   PYTHONPATH=str(hostile), PYTHONUSERBASE=str(hostile))
        process = await asyncio.create_subprocess_exec(str(ROOT_ENTRY), 'host-root', '--physical', str(PHYSICAL),
            env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(5.):
                while not SOCKET.exists():
                    if process.returncode is not None:
                        _, errors = await process.communicate()
                        raise AssertionError('installed root entry failed: '+errors.decode())
                    await asyncio.sleep(.02)
        except BaseException:
            if process.returncode is None:
                process.terminate()
            await process.communicate()
            raise
        return process
    async def root_stop(process):
        if process is None:
            return
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        async with asyncio.timeout(15.):
            stdout, stderr = await process.communicate()
        assert process.returncode == 0 and not stdout and not stderr, (process.returncode, stdout, stderr)
    async def call(document):
        try:
            raw = await command.run(sys.executable, '-I', '/opt/hostbackend-acceptance.py',
                                    'frontend', json.dumps(document))
        except Unknown as error:
            detail = getattr(error, 'stderr', b'').decode(errors='replace')
            raise AssertionError('actual HOST frontend failed: '+str(error)+' '+detail) from error
        return json.loads(raw)
    async def config_negatives():
        # Файлы принадлежат только private fixture; никакой host installation.
        base = Path('/etc/mop-host-config-cases')
        base.mkdir(mode=0o755)
        for kind in ('symlink', 'hardlink', 'writable', 'owner', 'oversized', 'policy', 'epoch', 'ancestor'):
            candidate = base/kind
            if kind == 'symlink':
                candidate.symlink_to(PHYSICAL)
            elif kind == 'hardlink':
                os.link(PHYSICAL, candidate)
            else:
                candidate.write_text(json.dumps({**physical, **({'policy': {}} if kind == 'policy'
                    else {'epoch': epoch} if kind == 'epoch' else {})}) if kind != 'oversized' else ' '*4097)
                candidate.chmod(0o600)
                if kind == 'writable': candidate.chmod(0o622)
                if kind == 'owner': os.chown(candidate, NODE_UID, NODE_UID)
                if kind == 'ancestor': base.chmod(0o777)
            process = await asyncio.create_subprocess_exec(str(ROOT_ENTRY), 'host-root', '--physical', str(candidate),
                env=dict(PATH='/usr/bin:/bin'), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(5.):
                    stdout, stderr = await process.communicate()
                assert process.returncode == 1 and not stdout and stderr, (kind, process.returncode, stdout, stderr)
                assert not SOCKET.exists(), kind
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.communicate()
                base.chmod(0o755)
                candidate.unlink()
        return ['symlink', 'hardlink', 'writable', 'owner', 'oversized', 'policy', 'epoch', 'ancestor']

    async def peer_negative():
        directory = Path('/run/mop-host/nonroot-peer')
        directory.mkdir(mode=0o755)
        os.chown(directory, NODE_UID, NODE_UID)
        endpoint = directory/'peer.sock'
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '/opt/hostbackend-acceptance.py',
            'fake-peer', str(endpoint), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(5.):
                assert await process.stdout.readline() == b'ready\n'
            # После bind immutable root ancestor: отказ обязан быть peer credential,
            # не более ранняя проверка writable parent.
            os.chown(directory, 0, 0)
            raw = await command.run(sys.executable, '-I', '/opt/hostbackend-acceptance.py',
                'wrong-peer', json.dumps(dict(path=str(endpoint), node=node)))
            assert json.loads(raw)['status'] == 'refused'
        finally:
            if process.returncode is None: process.terminate()
            await process.communicate()
            endpoint.unlink(missing_ok=True)
            directory.rmdir()
    try:
        config_checks = await config_negatives()
        await peer_negative()
        supervisor = await root_start()
        one = await call(value)
        reference = Reference.parse(one['snapshot']['reference'])
        assert one['calls'].count('host-CAS') == 1, one
        async with asyncio.timeout(5.):
            while not (body/'descendant.json').exists():
                await asyncio.sleep(.02)
        record = json.loads((body/'descendant.json').read_text())
        pid = record['pid']
        start = parse_proc_start(Path(f'/proc/{pid}/stat').read_bytes(), pid)
        assert Path(f'/proc/{pid}/cgroup').read_text().strip() == '0::'+reference.scope.cgroup
        live = dict(**value)
        live.update(executor=one['executor_epoch'], empty=False, reference=reference.wire())
        two = await call(live)
        assert two['frontend_pid'] != one['frontend_pid']
        assert two['executor_epoch'] == one['executor_epoch']
        assert two['snapshot'] == one['snapshot'] and two['snapshot']['populated'] is True
        assert two['calls'] == ['host-discovery'], two
        assert parse_proc_start(Path(f'/proc/{pid}/stat').read_bytes(), pid) == start
        assert Path(f'/proc/{pid}/cgroup').read_text().strip() == '0::'+reference.scope.cgroup
        observed = await adapter.inspect(reference.scope)
        observed.verify(installed, reference.scope, reference)
        assert observed.populated
        cancelled = await call({**live, 'cancel': True})
        assert cancelled['status'] == 'cancelled-and-cleaned'
        assert parse_proc_start(Path(f'/proc/{pid}/stat').read_bytes(), pid) == start
        for change in ({'node': str(uuid4())}, {'installation': 'foreign'},
                       {'epoch': str(uuid4())}, {'executor': str(uuid4())}):
            refusal = await call({**live, **change, 'expect_refusal': True})
            assert refusal['status'] == 'refused' and 'host-CAS' not in refusal['calls']
            assert parse_proc_start(Path(f'/proc/{pid}/stat').read_bytes(), pid) == start
        # Root потерял process-local ownership. Новому root нельзя усыновить
        # старый unit, даже если frontend солжёт про SQL-empty.
        await root_stop(supervisor)
        supervisor = None
        assert parse_proc_start(Path(f'/proc/{pid}/stat').read_bytes(), pid) == start
        supervisor = await root_start()
        for empty in (False, True):
            refusal = await call({**live, 'empty': empty, 'expect_refusal': True})
            assert refusal['status'] == 'refused' and 'host-CAS' not in refusal['calls']
        assert parse_proc_start(Path(f'/proc/{pid}/stat').read_bytes(), pid) == start
        return dict(status='accepted', root_entry='immutable isolated installed release',
            frontend_pids=[one['frontend_pid'], two['frontend_pid']], body_pid=pid,
            root_config_negatives=config_checks, wrong_unix_peer='actual nonroot listener refused by SO_PEERCRED',
            repeated_cancel='cleanup joined; body PID/start unchanged',
            body_process_start=start, reference=reference.wire(), first_bind_cas=1,
            reconnect_cas=0, reconnect_start=0, reconnect_kill=0,
            changed_backend='refused before CAS; old body retained',
            restore='refused before CAS; old body retained',
            http_sql='registry double; actual PG/HTTPS checked separately',
            native='not vendor-native; complete action guard remains closed')
    finally:
        await root_stop(supervisor)
        if reference is not None:
            # Только fixture owner с первым actual reference, не новый backend.
            facts = await adapter.inspect(reference.scope)
            if facts is not None and facts.populated:
                empty = await adapter.retire(reference)
                assert not empty.populated


if __name__ == '__main__':
    if sys.argv[1] == 'fake-peer':
        asyncio.run(fake_peer(sys.argv[2]))
    elif sys.argv[1] == 'wrong-peer':
        asyncio.run(wrong_peer(json.loads(sys.argv[2])))
    else:
        asyncio.run(frontend(json.loads(sys.argv[2])))
