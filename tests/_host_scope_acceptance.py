#!/usr/bin/env python3
"""Actual OS gate только внутри новой private Docker fixture #719.

Настоящие systemd/cgroup и разные UID; HTTP admission здесь явно fixture guard,
не actual server/native readiness acceptance. Никогда не вызывается на host.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from uuid import uuid4

NODE_UID, BODY_UID = 21071, 21072
sys.path.insert(0, '/opt/fixture')


def imports():
    from mop.node import host_scope as contract
    from mop.node.host_scope_client import Client
    from mop.node.host_scope_supervisor import SocketServer, Supervisor
    from mop.node.host_scope_systemd import BoundedCommand, SystemdCgroupAdapter
    from mop.node.http_execution import EffectGuard, Work
    return contract, Client, SocketServer, Supervisor, BoundedCommand, SystemdCgroupAdapter, EffectGuard, Work


async def node():
    os.setgroups([])
    os.setgid(NODE_UID)
    os.setuid(NODE_UID)
    m, Client, _, _, _, _, EffectGuard, Work = imports()
    request = json.loads(sys.argv[2])
    s = m.Scope.parse(request['scope'])
    w = Work.parse(dict(schema_version=1, **{k: getattr(s, k) for k in
        ('epoch', 'node_id', 'executor_epoch', 'target_id', 'incarnation_id')},
        operation_id=str(uuid4()), step_id=str(uuid4()), grant_id=str(uuid4()),
        owner_id='1', generation='1', revision='1', role='fixture', bundle_digest='a'*64,
        action='restart_session', not_after=(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()))
    client = Client(Path('/run/mop-host/supervisor.sock'), node_uid=NODE_UID, work=w)
    async def fixture_admission():
        pass
    guard = EffectGuard(fixture_admission, lambda: None)
    if request['action'] == 'lost-start':
        # Только fixture barriers; root ждёт reply до concurrent inspect,
        # затем разрешает разрыв без ACK. Настоящий Unix peer — node UID.
        from mop.node.host_scope_supervisor import read_frame
        reader, writer = await asyncio.open_unix_connection(str(client.path))
        value = m.Request('start', s, str(uuid4()))
        try:
            writer.write(value.encode()+b'\n')
            await writer.drain()
            reply = m.decode(await read_frame(reader))
            if reply['status'] != 'ok':
                raise RuntimeError('fixture lost-start did not receive a successful reply')
            snap = m.Snapshot.parse(reply['snapshot'])
            barrier = Path('/var/lib/mop/node-service')/s.launch_id
            barrier.write_text(json.dumps(snap.wire()))
            async with asyncio.timeout(20.):
                while not barrier.with_suffix('.disconnect').exists():
                    await asyncio.sleep(.02)
        finally:
            writer.close()
            await writer.wait_closed()
    elif request['action'] == 'start':
        snap = await client.start(s, guard)
    elif request['action'] == 'inspect':
        snap = await client.inspect(m.Reference.parse(request['reference']), guard)
    else:
        snap = await client.retire(m.Reference.parse(request['reference']), guard)
    print(json.dumps(snap.wire()), flush=True)


async def acceptance():
    if os.getuid() != 0 or Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise RuntimeError('acceptance requires the private fixture systemd PID 1')
    m, _, SocketServer, Supervisor, Command, Adapter, _, _ = imports()
    p = m.Installed(NODE_UID, BODY_UID, BODY_UID, Path('/opt/mop/libexec/host-runner'),
                    Path('/var/lib/mop/host-bodies'), Path('/var/lib/mop/node-service'))
    p.body_root.mkdir(parents=True, exist_ok=True)
    p.body_root.chmod(0o755)
    p.node_private.mkdir(parents=True, exist_ok=True)
    p.node_private.chmod(0o700)
    os.chown(p.node_private, NODE_UID, NODE_UID)
    bearer = p.node_private/'bearer'
    bearer.write_text('fixture-only-node-credential-not-a-real-bearer')
    bearer.chmod(0o600)
    os.chown(bearer, NODE_UID, NODE_UID)
    socket_parent = Path('/run/mop-host')
    socket_parent.mkdir(mode=0o755)
    s = m.Scope(*(str(uuid4()) for _ in range(6)))
    body = p.body(s)
    body.mkdir(mode=0o700)
    os.chown(body, BODY_UID, BODY_UID)
    consumer = body/'consumer.json'
    consumer.write_text(json.dumps(dict(schema_version=1, fixture='double-fork')))
    consumer.chmod(0o600)
    os.chown(consumer, BODY_UID, BODY_UID)
    adapter = Adapter(p)
    class FixtureDiagnostics(Command):
        async def run(self, *args):
            try:
                return await super().run(*args)
            except m.Unknown as error:
                detail = getattr(error, 'stderr', b'').decode('utf-8', errors='replace')
                print(json.dumps(dict(fixture_tool=list(args), diagnostic=detail)), file=sys.stderr, flush=True)
                if args[0] == '/usr/bin/systemd-run':
                    for words in (('/usr/bin/systemctl', 'show', '--all',
                        '--property=Result,ExecMainStatus,ExecMainCode,StatusErrno,LoadState,SubState', '--', s.unit),
                        ('/usr/bin/journalctl', '--no-pager', '-n', '30', '--unit='+s.unit)):
                        try:
                            raw = await super().run(*words)
                            print(raw.decode('utf-8', errors='replace'), file=sys.stderr, flush=True)
                        except m.Unknown as diagnostic_error:
                            print('fixture diagnostic unavailable: '+str(diagnostic_error), file=sys.stderr, flush=True)
                raise
    adapter.command = FixtureDiagnostics()
    owner = Supervisor(p, adapter, timeout=20.)
    server = SocketServer(owner, socket_parent/'supervisor.sock')
    command = Command(timeout=30.)
    started = None
    retired = False
    try:
        await server.start()
        async def call(action, reference=None):
            value = dict(action=action, scope=s.wire(), reference=reference.wire() if reference else None)
            try:
                raw = await command.run(sys.executable, '-I', __file__, 'node', json.dumps(value))
            except m.Unknown as error:
                detail = getattr(error, 'stderr', b'').decode('utf-8', errors='replace')
                object_name = ''.join(c if c.isalnum() else '_'+format(ord(c), '02x') for c in s.unit)
                for prop in ('ExecStart', 'ExecStartPre', 'EnvironmentFiles'):
                    raw_property = await adapter.command.run('/usr/bin/busctl', '--system', '--json=short',
                        'get-property', 'org.freedesktop.systemd1', '/org/freedesktop/systemd1/unit/'+object_name,
                        'org.freedesktop.systemd1.Service', prop)
                    print(prop+': '+raw_property.decode(), file=sys.stderr, flush=True)
                raise RuntimeError('fixture node '+action+' failed: '+str(error)+' '+detail) from error
            return m.Snapshot.parse(json.loads(raw))
        started = await call('start')
        if not started.populated:
            raise AssertionError('owned runner never populated the actual cgroup')
        record_path = body/'descendant.json'
        async with asyncio.timeout(5.):
            while not record_path.exists():
                await asyncio.sleep(.02)
        record = json.loads(record_path.read_text())
        if (record['pid'] == record['starter'] or record['sid'] == record['starter']
                or record['uid'] != BODY_UID or record['gid'] != BODY_UID
                or record['cgroup'] != '0::'+s.cgroup or not all(record['checks'].values())):
            raise AssertionError('double-fork/setsid privilege escape negative check failed')
        # Не доверяем body JSON как OS authority: root независимо читает kernel.
        pid = record['pid']
        if Path(f'/proc/{pid}/cgroup').read_text().strip() != '0::'+s.cgroup:
            raise AssertionError('actual detached descendant left its cgroup')
        if stat.S_IMODE(body.stat().st_mode) != 0o755:
            raise AssertionError('owned body did not change its consumer cwd permissions')
        observed = await call('inspect', started.reference)
        if observed.reference != started.reference or not observed.populated:
            raise AssertionError('detached descendant was not physically inspected')
        # Только собственный fixture cwd: root меняет/убирает directory entry,
        # пока descendant держит старый cwd. Не трогаем чужие units/деревья.
        moved = body.with_name('moved-'+s.launch_id)
        body.rename(moved)
        observed = await call('inspect', started.reference)
        if observed.reference != started.reference or not observed.populated:
            raise AssertionError('renamed cwd blocked exact containment inspection')
        shutil.rmtree(moved)
        observed = await call('inspect', started.reference)
        if observed.reference != started.reference or not observed.populated:
            raise AssertionError('removed cwd blocked exact containment inspection')
        # Wrong InvocationID обязан отказать без cgroup.kill.
        from dataclasses import replace
        try:
            await adapter.retire(replace(started.reference, invocation_id=uuid4().hex))
        except m.Refused:
            pass
        else:
            raise AssertionError('foreign invocation was retired')
        if not Path(f'/proc/{pid}').exists():
            raise AssertionError('wrong reference killed the owned child')
        empty = await call('retire', started.reference)
        if empty.populated or empty.reference != started.reference:
            raise AssertionError('cgroup retirement did not prove the exact empty subtree')
        retired = True

        # Второй exact owned scope: start reply без ACK + concurrent actual
        # inspection. Fixture barriers удерживают порядок без OS fact stubs.
        s = m.Scope(*(str(uuid4()) for _ in range(6)))
        body = p.body(s)
        body.mkdir(mode=0o700)
        os.chown(body, BODY_UID, BODY_UID)
        consumer = body/'consumer.json'
        consumer.write_text(json.dumps(dict(schema_version=1, fixture='double-fork')))
        os.chown(consumer, BODY_UID, BODY_UID)
        consumer.chmod(0o600)
        started, retired = None, False
        lost_task = asyncio.create_task(call('lost-start'))
        barrier = p.node_private/s.launch_id
        entered, release, abandoning = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_inspect, original_abandon = adapter.inspect, owner.abandon_started
        inspection_task = None
        try:
            async with asyncio.timeout(10.):
                while not barrier.exists():
                    if lost_task.done():
                        await lost_task
                        raise AssertionError('lost-start exited before fixture barrier')
                    await asyncio.sleep(.02)
            started = m.Snapshot.parse(json.loads(barrier.read_text()))
            async def held(address):
                if address == s and not entered.is_set():
                    entered.set()
                    await release.wait()
                return await original_inspect(address)
            async def abandoning_start(*args):
                abandoning.set()
                return await original_abandon(*args)
            adapter.inspect, owner.abandon_started = held, abandoning_start
            inspection_task = asyncio.create_task(call('inspect', started.reference))
            async with asyncio.timeout(10.):
                await entered.wait()
                barrier.with_suffix('.disconnect').write_text('fixture-only')
                lost_snapshot = await lost_task
                if lost_snapshot != started:
                    raise AssertionError('lost-start reply reference changed')
                await abandoning.wait()
                release.set()
                await inspection_task
                await server.drain()
            empty = await original_inspect(s)
            if empty is None or empty.verify(p, s, started.reference).populated:
                raise AssertionError('lost ACK with concurrent inspection leaked actual containment')
            retired = True
            try:
                await owner.execute(NODE_UID, m.Request('start', s, str(uuid4())))
            except m.Refused:
                pass
            else:
                raise AssertionError('lost ACK replay started a second native body')
        finally:
            release.set()
            for task in (lost_task, inspection_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (lost_task, inspection_task)
                                   if task is not None), return_exceptions=True)
            adapter.inspect, owner.abandon_started = original_inspect, original_abandon
        # PID disappearance alone никогда не является positive criterion.
        print(json.dumps(dict(status='accepted', physical='systemd-cgroup-v2',
            double_fork=True, setsid=True, retired_empty=True, negative_checks=record['checks'],
            cwd_mutations=['body-chmod-0755', 'renamed', 'removed'],
            lost_ack_concurrent_inspection='retired exact empty cgroup, replay refused',
            admission='fixture guard, not HTTP acceptance', native='not tested')), flush=True)
    finally:
        primary = sys.exception()
        try:
            if started is not None and not retired:
                facts = await adapter.inspect(s)
                if facts is not None and facts.populated:
                    await adapter.retire(started.reference)
            await server.aclose()
        except (m.Refused, m.Unknown) as cleanup_error:
            if primary is None:
                raise
            primary.add_note('private fixture cleanup: '+str(cleanup_error))
        finally:
            await adapter.aclose()
            await command.aclose()


if __name__ == '__main__':
    asyncio.run(node() if sys.argv[1:2] == ['node'] else acceptance())
