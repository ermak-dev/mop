"""Реальный ASGI/ORM контракт #628; никаких credentials старой установки.

HYPOTHESIS: unbound_websocket закрывает даже допустимые подключения, terminal
control не имеет межпроцессного fence. SQL запись не означает живой PTY.
SPEC: Django session/DRF token, Origin, текущий scope перед приватной доставкой,
bounded lifecycle, один writer независимо от gateway, no-replay input.
RED: PG16 — unbound 4404 вместо auth 4401; PG17 — actual #626 batch закрывался,
coverage терялась. Native NATS — PING обгонял buffered SUB; пустой allow list
не запрещал publish. PG17 PTY flags и PG16 active REST detach также дали red.
SOLUTION: Channels adapters с owned Runtime, SQL-only Access/Grant authority,
отдельными bounded subscriptions и terminal control. Broker ACL — explicit deny;
command drain до receipt; источник сохраняет собственные cursor/coverage.
RESULT: собственный slice + sealed grant и существующий ASGI lifecycle —
по 32/32 green на owned PostgreSQL 16/17, включая 21 исходный case, настоящие
WSS/TLS/NATS/Unix/PTY и independent gateway processes. Собственные red проверки
доказали expiry replacement, unproven release, unbound Root capability, подмену
retired instance и скрытый detach timeout. Full strict CI — gate точного SHA.
STATUS: FIXED — see #628; root bindings #625/#626/#649 и physical node resolver
не выдаются за принятую интеграцию нового выпуска.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

from asgiref.sync import async_to_sync
from asgiref.testing import ApplicationCommunicator
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone


class GatewayBoundary(TransactionTestCase):
    serialized_rollback = True
    # HYPOTHESIS: default root не маршрутизирует private inbox к текущей auth.
    # SPEC: fixed UUID route, исходный Token/session, без pool-view permission.
    # SOLUTION: конечный private consumer включён в существующий ASGI gateway.
    # RESULT: 4404 вместо 4401 дал RED; проверка текущей auth прошла GREEN.
    # STATUS: FIXED — see #763.
    def test_actual_asgi_private_inbox_requires_authentication_not_unbound_route(self):
        from mop.server.controller.asgi import ControllerApplication
        from mop.server.controller.runtime import Runtime
        async def run():
            runtime = Runtime()
            app = ControllerApplication(runtime=runtime, startup=lambda: None)
            app.started = True
            socket = ApplicationCommunicator(app, {'type':'websocket',
                'path':'/ws/v1/inbox/'+str(uuid4()), 'headers':[], 'query_string':b'',
                'scheme':'wss', 'server':('testserver',443), 'subprotocols':[]})
            try:
                await socket.send_input({'type':'websocket.connect'})
                self.assertEqual(await socket.receive_output(2),
                    {'type':'websocket.close','code':4401},
                    'private route must authenticate, not disappear as an unknown route')
            finally:
                socket.stop()
                await app.gateway.aclose()
                await runtime.aclose()
        with override_settings(ALLOWED_HOSTS=['testserver']):
            async_to_sync(run)()

    def test_actual_asgi_events_route_requires_live_authentication(self):
        from mop.server.controller.asgi import ControllerApplication
        from mop.server.controller.runtime import Runtime
        async def run():
            runtime = Runtime()
            app = ControllerApplication(runtime=runtime, startup=lambda: None)
            app.started = True
            socket = ApplicationCommunicator(app, {'type':'websocket', 'path':'/ws/v1/events',
                'headers':[(b'origin',b'https://testserver')], 'query_string':b'', 'scheme':'wss',
                'server':('testserver',443), 'subprotocols':[]})
            try:
                await socket.send_input({'type':'websocket.connect'})
                reply = await socket.receive_output(2)
                self.assertEqual(reply, {'type':'websocket.close','code':4401},
                    'events must reach current Django authentication, not an unbound route')
            finally:
                socket.stop()
                await runtime.aclose()
        with override_settings(ALLOWED_HOSTS=['testserver']):
            async_to_sync(run)()


class RealtimeCases(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        try:
            apps.get_model('execution','TerminalAttachment')
        except LookupError:
            self.fail('private terminal attachment and cross-gateway control fence must be installed')
        from mop.server.sessions import terminal_control as tc
        from mop.server.access import models as a, services as auth
        from mop.server.catalog.models import Project, Node, NodeProject
        from mop.server.sessions.models import Assignment, ExecutionIncarnation
        from mop.server.controller.runtime import initialize
        initialize()
        self.tc, self.auth = tc, auth
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        self.node = Node.objects.create(name='node',driver='host',ssh_host='example.invalid',
            control_origin='https://example.invalid',lan_address='example.invalid',bootstrap_address='example.invalid')
        NodeProject.objects.create(node=self.node,project=self.project)
        self.user = get_user_model().objects.create_user('alice',password='fixture-password')
        auth.bind_local_identity(self.user.pk)
        source = a.IdentitySource.objects.get(name='local')
        a.ProjectMembership.objects.create(user=self.user,project=self.project,source=source)
        a.AllProjectMembership.objects.create(user=self.user,source=source)
        self.user.user_permissions.add(*Permission.objects.filter(codename__in=[
            'view_pool','read_terminal','control_terminal']))
        self.key = auth.issue_token(self.user.pk)
        self.actor = auth.token_context(self.key)
        self.target = Assignment.objects.create(project=self.project,node=self.node,owner=self.user,
            role='worker',bundle_digest='a'*64,grant_id=uuid4(),expires_at=timezone.now()+timedelta(hours=1))
        self.inc = ExecutionIncarnation.objects.create(assignment=self.target,node=self.node,executor_epoch=uuid4())
        self.target.current_incarnation=self.inc
        self.target.save()
        self.control = tc.Terminals()
        self.run_id=uuid4()

    def reserve(self, mode='rw', actor=None):
        return self.control.open(actor or self.actor,target_id=self.target.pk,
            incarnation_id=self.inc.pk,mode=mode,cols=100,rows=30)

    async def physical(self, root, context, current):
        from channels.db import database_sync_to_async
        async def pin(instance):
            pinned=await database_sync_to_async(self.control.pin_instance)(self.actor,context,**instance)
            context.update(pinned)
            return pinned
        return await root.open(context,current=current,pin=pin)

    # BUG: истёкший SQL lease освобождает writer при ещё живом borrowed PTY.
    # HYPOTHESIS: unknown ошибочно исключён из exclusive fence.
    # SOLUTION: unknown входит в SQL unique fence; Root pins actual instance
    # до start и принимает retirement только этого fd/epochs после detach.
    # RESULT: PG16/17 — старый input после expiry не попадает в actual PTY,
    # competing processes не получают writer, timeout/cancel сохраняют fence.
    # STATUS: FIXED — see #628.
    def test_expired_inflight_writer_retains_fence_until_physical_retirement(self):
        from mop.server.sessions.terminal_models import TerminalAttachment
        from mop.server.realtime.terminal import PtyTerminalStream
        import os, tty
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        async def run():
            from channels.db import database_sync_to_async
            master,slave=os.openpty()
            os.set_blocking(master,False)
            tty.setraw(slave)
            from mop.server.realtime.terminal import RootTerminalStream
            from _terminal_fixture import NativePtyResolver
            root=RootTerminalStream(NativePtyResolver(master,bound))
            async def current():
                await database_sync_to_async(self.control.validate)(self.actor,bound)
            stream=await self.physical(root,bound,current)
            await database_sync_to_async(self.control.before_input)(self.actor,bound,1)
            try:
                await database_sync_to_async(lambda: TerminalAttachment.objects.filter(pk=value['id']).update(
                    expires_at=timezone.now()-timedelta(seconds=1)))()
                with self.assertRaises(self.tc.Conflict,msg='expired writer still owns a physical handle; replacement must be fenced'):
                    await database_sync_to_async(self.reserve)()
                with self.assertRaises(self.tc.Conflict):
                    await stream.write(b'old')
                os.set_blocking(slave,False)
                with self.assertRaises(BlockingIOError):
                    os.read(slave,16)
                self.assertTrue(os.isatty(master))
            finally:
                await stream.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_unproven_release_or_rest_detach_cannot_free_bound_writer(self):
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        self.control.release(self.actor,bound)
        with self.assertRaises(self.tc.Conflict,msg='cleanup without physical proof must retain the writer fence'):
            self.reserve()

    def test_root_rejects_callable_without_physical_fencing_capabilities(self):
        from mop.server.realtime.terminal import RootTerminalStream, TerminalUnavailable, PtyTerminalStream
        import os
        async def run():
            master,slave=os.openpty()
            os.set_blocking(master,False)
            async def resolver(context):
                return PtyTerminalStream(master,writable=True)
            root=RootTerminalStream(resolver)
            try:
                with self.assertRaises(TerminalUnavailable,msg='Root must preflight physical guard and retirement capabilities'):
                    stream=await root.open({})
                    await stream.aclose()
            finally:
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_expired_inflight_native_write_blocks_independent_gateway_processes(self):
        from mop.server.sessions.terminal_models import TerminalAttachment
        from mop.server.realtime.terminal import RootTerminalStream
        from mop.server.controller.runtime import Runtime
        from _terminal_fixture import NativePtyResolver, claim
        from concurrent.futures import ProcessPoolExecutor
        from multiprocessing import get_context
        from django.conf import settings
        from pathlib import Path
        import os,tty,tempfile,json,time
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        config=dict(settings.BOOTSTRAP)
        config['database']=dict(config['database'])
        config['database']['NAME']=connections['default'].settings_dict['NAME']
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bootstrap.json'
            path.write_text(json.dumps(config));path.chmod(0o600)
            async def run():
                runtime=Runtime()
                master,slave=os.openpty()
                tty.setraw(slave)
                os.set_blocking(master,False);os.set_blocking(slave,False)
                root=RootTerminalStream(NativePtyResolver(master,bound))
                async def current():
                    await runtime.database(self.control.validate,self.actor,bound)
                stream=await self.physical(root,bound,current)
                await runtime.database(self.control.before_input,self.actor,bound,1)
                waiting=asyncio.Event()
                original_ready=stream._ready
                async def ready(**kwargs):
                    waiting.set()
                    await original_ready(**kwargs)
                stream._ready=ready
                original_guard=stream.guard
                saturate=True
                def guard(*args):
                    nonlocal saturate
                    original_guard(*args)
                    if saturate:
                        # Насыщение прямо после DB await: line discipline могла
                        # освободить место после первоначального заполнения.
                        for chunk in (b'x'*4096,b'x'):
                            while True:
                                try:
                                    os.write(master,chunk)
                                except BlockingIOError:
                                    break
                        saturate=False
                stream.guard=guard
                task=None
                try:
                    # Настоящий PTY backpressure, не mock os.write/SQL.
                    while True:
                        try:
                            os.write(master,b'x'*4096)
                        except BlockingIOError:
                            break
                    task=asyncio.create_task(stream.write(b'old'))
                    await asyncio.wait_for(waiting.wait(),2)
                    await runtime.database(lambda: TerminalAttachment.objects.filter(pk=value['id']).update(
                        expires_at=timezone.now()-timedelta(seconds=1)))
                    def contend():
                        with ProcessPoolExecutor(max_workers=2,mp_context=get_context('spawn')) as pool:
                            start=time.time()+1
                            calls=[pool.submit(claim,str(path),self.actor,self.target.pk,self.inc.pk,start) for _ in range(2)]
                            return [call.result(timeout=20) for call in calls]
                    self.assertEqual(await asyncio.to_thread(contend),[None,None])
                    received=bytearray()
                    while True:
                        try:
                            received.extend(os.read(slave,65536))
                        except BlockingIOError:
                            break
                    with self.assertRaises(self.tc.Conflict):
                        await asyncio.wait_for(task,2)
                    self.assertNotIn(b'old',received)
                    with self.assertRaises(BlockingIOError):
                        os.read(slave,16)
                    with self.assertRaises(self.tc.Conflict):
                        await runtime.database(self.control.before_input,self.actor,bound,1)
                finally:
                    if task is not None:
                        task.cancel();await asyncio.gather(task,return_exceptions=True)
                    await stream.aclose();await runtime.aclose()
                    os.close(master);os.close(slave)
            async_to_sync(run)()

    def test_instance_retirement_proof_not_json_or_lease_expired_releases_writer(self):
        from mop.server.realtime.terminal import RootTerminalStream, TerminalUnavailable
        from mop.server.controller.runtime import Runtime
        from _terminal_fixture import NativePtyResolver
        from dataclasses import replace,asdict
        import os,tty
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        async def run():
            runtime=Runtime()
            master,slave=os.openpty();os.set_blocking(master,False);tty.setraw(slave)
            resolver=NativePtyResolver(master,bound)
            root=RootTerminalStream(resolver)
            control=self.tc.Terminals(retirement_authority=root)
            async def current():
                await runtime.database(control.validate,self.actor,bound)
            stream=await self.physical(root,bound,current)
            try:
                await runtime.database(control.release,self.actor,bound)
                with self.assertRaises(TerminalUnavailable):
                    await root.retirement(bound)
                with self.assertRaises(TerminalUnavailable):
                    await runtime.database(control.retire,bound,{'retired':True,'reason':'LeaseExpired'})
                self.assertFalse((await root.inspect(bound))['retired'])
                proof=await root.detach(bound)
                self.assertTrue((await root.inspect(bound))['retired'])
                with self.assertRaises(TerminalUnavailable):
                    await runtime.database(control.retire,bound,asdict(proof))
                with self.assertRaises(TerminalUnavailable):
                    await runtime.database(control.retire,bound,replace(proof,instance_id=str(uuid4())))
                wrong=dict(bound,connection_id=str(uuid4()))
                with self.assertRaises(TerminalUnavailable):
                    await runtime.database(control.retire,wrong,proof)
                with self.assertRaises(TerminalUnavailable):
                    RootTerminalStream(resolver).verify_retirement(bound,proof)
                await root.reconcile(self.actor,UUID(value['id']),runtime=runtime,terminals=control)
                newer=await runtime.database(self.reserve)
                self.assertGreater(newer['generation'],bound['generation'])
                with self.assertRaises(TerminalUnavailable):
                    await stream.write(b'old')
                with self.assertRaises(TerminalUnavailable):
                    await stream.start()
                self.assertTrue(os.isatty(master))
            finally:
                await stream.aclose();await runtime.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_detach_timeout_and_repeated_cancel_retain_fence_until_real_close(self):
        from mop.server.realtime.terminal import RootTerminalStream, TerminalUnavailable
        from mop.server.controller.runtime import Runtime
        from _terminal_fixture import NativePtyResolver
        import os
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        async def run():
            runtime=Runtime()
            master,slave=os.openpty();os.set_blocking(master,False)
            root=RootTerminalStream(NativePtyResolver(master,bound))
            control=self.tc.Terminals(retirement_authority=root)
            async def current():
                await runtime.database(control.validate,self.actor,bound)
            stream=await self.physical(root,bound,current)
            original_close=stream.aclose
            entered,release=asyncio.Event(),asyncio.Event()
            async def delayed_close():
                entered.set()
                await release.wait()
                await original_close()
            stream.aclose=delayed_close
            try:
                await runtime.database(control.release,self.actor,bound)
                with self.assertRaises(TerminalUnavailable):
                    await root.detach(bound,timeout=.01)
                await entered.wait()
                for _ in range(3):
                    detach=asyncio.create_task(root.detach(bound))
                    await asyncio.sleep(0)
                    detach.cancel();await asyncio.gather(detach,return_exceptions=True)
                self.assertFalse(stream.retired)
                self.assertIsNotNone(stream.fd)
                with self.assertRaises(TerminalUnavailable):
                    await root.retirement(bound)
                with self.assertRaises(self.tc.Conflict):
                    await runtime.database(self.reserve)
                release.set()
                await root.detach(bound)
                # После потери Root receipt допустима только actual inspection
                # original instance, не новый fd/unknown->closed по таймеру.
                restarted=RootTerminalStream(root.resolver)
                recovered=self.tc.Terminals(retirement_authority=restarted)
                await restarted.reconcile(self.actor,UUID(value['id']),runtime=runtime,terminals=recovered)
                self.assertEqual((await runtime.database(self.reserve))['mode'],'rw')
                self.assertTrue(os.isatty(master))
            finally:
                release.set();await original_close();await runtime.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_physical_guard_rechecks_identity_after_authority_wait_for_write_and_resize(self):
        from mop.server.realtime.terminal import RootTerminalStream, TerminalUnavailable
        from mop.server.controller.runtime import Runtime
        from _terminal_fixture import NativePtyResolver
        import os,tty,termios
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        async def run():
            runtime=Runtime()
            master,slave=os.openpty();tty.setraw(slave)
            os.set_blocking(master,False);os.set_blocking(slave,False)
            resolver=NativePtyResolver(master,bound)
            root=RootTerminalStream(resolver)
            entered,release=asyncio.Event(),asyncio.Event()
            blocking=False
            async def current():
                await runtime.database(self.control.validate,self.actor,bound)
                if blocking:
                    entered.set();await release.wait()
            stream=await self.physical(root,bound,current)
            try:
                for operation,field in ((lambda:stream.write(b'old'),'conversation_epoch'),
                                        (lambda:stream.resize(121,41),'terminal_epoch')):
                    original=getattr(resolver,field)
                    before=termios.tcgetwinsize(slave)
                    blocking=True;entered.clear();release.clear()
                    task=asyncio.create_task(operation())
                    await asyncio.wait_for(entered.wait(),2)
                    setattr(resolver,field,str(uuid4()))
                    release.set()
                    with self.assertRaises(TerminalUnavailable):
                        await task
                    setattr(resolver,field,original)
                    self.assertEqual(termios.tcgetwinsize(slave),before)
                    with self.assertRaises(BlockingIOError):
                        os.read(slave,16)
            finally:
                await stream.aclose();await runtime.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_reconcile_rechecks_source_authority_after_physical_inspection_wait(self):
        from mop.server.realtime.terminal import RootTerminalStream
        from mop.server.controller.runtime import Runtime
        from mop.server.sessions.terminal_models import TerminalAttachment
        from mop.server.access.models import IdentitySource
        from _terminal_fixture import NativePtyResolver
        import os
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        async def run():
            runtime=Runtime()
            master,slave=os.openpty();os.set_blocking(master,False)
            entered,release=asyncio.Event(),asyncio.Event()
            class Resolver(NativePtyResolver):
                async def inspect(inner,context):
                    entered.set();await release.wait()
                    return await super().inspect(context)
            resolver=Resolver(master,bound)
            root=RootTerminalStream(resolver)
            async def current():
                await runtime.database(self.control.validate,self.actor,bound)
            stream=await self.physical(root,bound,current)
            try:
                await runtime.database(self.control.release,self.actor,bound)
                await stream.aclose()
                restarted=RootTerminalStream(resolver)
                control=self.tc.Terminals(retirement_authority=restarted)
                task=asyncio.create_task(restarted.reconcile(self.actor,UUID(value['id']),runtime=runtime,terminals=control))
                await asyncio.wait_for(entered.wait(),2)
                await runtime.database(lambda: IdentitySource.objects.filter(name='local').update(enabled=False))
                release.set()
                with self.assertRaises(PermissionError):
                    await task
                self.assertEqual(await runtime.database(lambda: TerminalAttachment.objects.get(pk=value['id']).state),'unknown')
            finally:
                release.set();await stream.aclose();await runtime.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_asgi_repeated_cancellation_waits_for_owned_physical_detach(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.terminal import RootTerminalStream
        from mop.server.controller.runtime import Runtime
        from mop.server.sessions.terminal_models import TerminalAttachment
        from _terminal_fixture import NativePtyResolver
        import os
        value=self.reserve()
        async def run():
            runtime=Runtime()
            master,slave=os.openpty();os.set_blocking(master,False)
            entered,release=asyncio.Event(),asyncio.Event()
            class Resolver(NativePtyResolver):
                async def resolve(inner,context):
                    stream=await super().resolve(context)
                    original=stream.aclose
                    async def delayed():
                        entered.set();await release.wait();await original()
                    stream.aclose=delayed
                    return stream
            resolver=Resolver(master,value)
            root=RootTerminalStream(resolver)
            gateway=Gateway(runtime=runtime,streams=root)
            socket=ApplicationCommunicator(gateway,{'type':'websocket','path':'/ws/v1/terminal/'+value['id'],
                'scheme':'wss','server':('testserver',443),'headers':[
                    (b'authorization',('Token '+self.key).encode())],'query_string':b'','subprotocols':[]})
            try:
                await socket.send_input({'type':'websocket.connect'})
                self.assertEqual((await socket.receive_output(2))['type'],'websocket.accept')
                await socket.receive_output(2)
                socket.future.cancel()
                await asyncio.wait_for(entered.wait(),2)
                for _ in range(3):
                    socket.future.cancel();await asyncio.sleep(0)
                with self.assertRaises(self.tc.Conflict):
                    await runtime.database(self.reserve)
                self.assertFalse(resolver.stream.retired)
                release.set()
                await socket.wait(3)
                self.assertTrue(resolver.stream.retired)
                self.assertEqual(await runtime.database(lambda: TerminalAttachment.objects.get(pk=value['id']).state),'closed')
                self.assertEqual(gateway.active,0)
                self.assertEqual(runtime.pending,0)
                self.assertEqual(root.closings,{})
                self.assertEqual((await runtime.database(self.reserve))['mode'],'rw')
                self.assertTrue(os.isatty(master))
            finally:
                release.set();socket.stop(exceptions=False)
                await gateway.aclose();await runtime.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_reconciliation_rejects_retired_substitute_for_original_physical_instance(self):
        from mop.server.realtime.terminal import RootTerminalStream, TerminalUnavailable
        from mop.server.controller.runtime import Runtime
        from _terminal_fixture import NativePtyResolver
        import os
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        async def run():
            runtime=Runtime()
            master,slave=os.openpty();os.set_blocking(master,False)
            resolver=NativePtyResolver(master,bound)
            root=RootTerminalStream(resolver)
            async def current():
                await runtime.database(self.control.validate,self.actor,bound)
            original=await self.physical(root,bound,current)
            substitute=await resolver.resolve(bound)
            substitute.bind(bound,current)
            await substitute.start();await substitute.aclose()
            recovered=RootTerminalStream(resolver)
            control=self.tc.Terminals(retirement_authority=recovered)
            try:
                await runtime.database(self.control.release,self.actor,bound)
                with self.assertRaises((TerminalUnavailable,self.tc.Conflict),msg='retired substitute must not prove original instance retirement'):
                    await recovered.reconcile(self.actor,UUID(value['id']),runtime=runtime,terminals=control)
                self.assertIsNotNone(original.fd)
            finally:
                await original.aclose();await runtime.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_asgi_detach_timeout_is_reported_and_shutdown_cannot_claim_quiescence(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.terminal import RootTerminalStream, TerminalUnavailable
        from mop.server.controller.runtime import Runtime
        from mop.server.sessions.terminal_models import TerminalAttachment
        from _terminal_fixture import NativePtyResolver
        import os
        value=self.reserve()
        async def run():
            runtime=Runtime()
            master,slave=os.openpty();os.set_blocking(master,False)
            release=asyncio.Event()
            class Resolver(NativePtyResolver):
                async def resolve(inner,context):
                    stream=await super().resolve(context)
                    original=stream.aclose
                    async def delayed():
                        await release.wait();await original()
                    stream.aclose=delayed
                    return stream
            resolver=Resolver(master,value)
            root=RootTerminalStream(resolver)
            gateway=Gateway(runtime=runtime,streams=root)
            socket=ApplicationCommunicator(gateway,{'type':'websocket','path':'/ws/v1/terminal/'+value['id'],
                'scheme':'wss','server':('testserver',443),'headers':[
                    (b'authorization',('Token '+self.key).encode())],'query_string':b'','subprotocols':[]})
            try:
                await socket.send_input({'type':'websocket.connect'})
                self.assertEqual((await socket.receive_output(2))['type'],'websocket.accept')
                await socket.receive_output(2)
                await socket.send_input({'type':'websocket.disconnect','code':1000})
                with self.assertRaises(TerminalUnavailable,msg='physical detach timeout must surface its reason'):
                    await socket.wait(3)
                with self.assertRaises(TerminalUnavailable,msg='shutdown must not claim pending detach is quiescent'):
                    await gateway.aclose()
                self.assertEqual(await runtime.database(lambda: TerminalAttachment.objects.get(pk=value['id']).state),'unknown')
                with self.assertRaises(self.tc.Conflict):
                    await runtime.database(self.reserve)
                release.set()
                await root.detach(resolver.stream.context)
                await root.reconcile(self.actor,UUID(value['id']),runtime=runtime,terminals=gateway.terminals)
                await gateway.aclose()
            finally:
                release.set();socket.stop(exceptions=False)
                await gateway.aclose();await runtime.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_independent_connections_reserve_one_writer_before_external_attach(self):
        barrier=Barrier(2)
        def open_one(_):
            connections.close_all()
            try:
                barrier.wait(5)
                try:
                    return self.reserve()['id']
                except self.tc.Conflict:
                    return None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as workers:
            values=list(workers.map(open_one,range(2)))
        self.assertEqual(sum(x is not None for x in values),1)
        self.assertEqual(self.reserve('ro')['mode'],'ro')

    def test_independent_gateway_processes_share_terminal_writer_fence(self):
        from concurrent.futures import ProcessPoolExecutor
        from multiprocessing import get_context
        from django.conf import settings
        from pathlib import Path
        import tempfile,json,time
        from _terminal_fixture import claim
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bootstrap.json'
            config=dict(settings.BOOTSTRAP)
            config['database']=dict(config['database'])
            config['database']['NAME']=connections['default'].settings_dict['NAME']
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            start=time.time()+2
            with ProcessPoolExecutor(max_workers=2,mp_context=get_context('spawn')) as pool:
                calls=[pool.submit(claim,str(path),self.actor,self.target.pk,self.inc.pk,start) for _ in range(2)]
                results=[call.result(timeout=20) for call in calls]
            self.assertEqual(sum(value is not None for value in results),1)
        self.assertEqual(self.reserve('ro')['mode'],'ro')

    def test_reader_cap_browser_binding_no_replay_and_restart_fence(self):
        value=self.reserve()
        attached=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        with self.assertRaises(self.tc.Conflict):
            self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        self.assertEqual(self.control.before_input(self.actor,attached,1)['mode'],'rw')
        with self.assertRaises(self.tc.Conflict):
            self.control.before_input(self.actor,attached,1)
        self.control.release(self.actor,attached)
        with self.assertRaises(self.tc.Conflict):
            self.control.validate(self.actor,attached)
        reader=self.reserve('ro')
        bound=self.control.bind(self.actor,uuid4(),UUID(reader['id']),gateway_id=self.run_id)
        with self.assertRaises(PermissionError):
            self.control.before_input(self.actor,bound,1)
        for _ in range(7):
            self.reserve('ro')
        with self.assertRaises(self.tc.Capacity):
            self.reserve('ro')

    def test_rest_close_fences_active_attachment_not_the_body(self):
        from django.test import Client
        value=self.reserve()
        bound=self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)
        client=Client(HTTP_AUTHORIZATION='Token '+self.key)
        with override_settings(ALLOWED_HOSTS=['testserver']):
            response=client.delete('/api/v1/terminal/attachments/'+value['id'],secure=True)
        self.assertEqual(response.status_code,204)
        with self.assertRaises(self.tc.Conflict):
            self.control.validate(self.actor,bound)
        # Не меняются Assignment/executor incarnation: close не kill/restart job.
        self.target.refresh_from_db()
        self.assertEqual(self.target.current_incarnation_id,self.inc.pk)
        with self.assertRaises(self.tc.Conflict):
            self.reserve()

    def test_assignment_other_human_work_never_grants_terminal_writer(self):
        other=get_user_model().objects.create_user('bob')
        self.target.owner=other
        self.target.save()
        with self.assertRaises(PermissionError):
            self.reserve()
        self.assertEqual(self.reserve('ro')['mode'],'ro')

    def test_session_bound_rest_open_cannot_be_claimed_by_same_users_token(self):
        from django.test import Client
        client=Client()
        client.force_login(self.user,backend='mop.server.controller.authbackend.ControllerBackend')
        self.auth.bind_session(self.user.pk,client.session.session_key)
        actor=self.auth.session_context(client.session.session_key)
        with override_settings(ALLOWED_HOSTS=['testserver']):
            response=client.post('/api/v1/terminal/attachments',{'schema_version':1,
                'target_id':str(self.target.pk),'incarnation_id':str(self.inc.pk),'mode':'rw',
                'cols':100,'rows':30},content_type='application/json',secure=True)
        self.assertEqual(response.status_code,201,response.content)
        value=response.json()
        with self.assertRaises(PermissionError):
            self.control.bind(self.actor,uuid4(),UUID(value['id']),gateway_id=self.run_id)

    def test_asgi_event_lifecycle_rechecks_scope_and_cleans_subscription(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.events import MemoryEvents
        from mop.server.controller.runtime import Runtime
        project=str(self.project.pk)
        async def run():
            runtime=Runtime()
            bus=MemoryEvents()
            gateway=Gateway(runtime=runtime,subscriber=bus,recheck_interval=.05)
            scope={'type':'websocket','path':'/ws/v1/events','scheme':'wss','server':('testserver',443),
                'headers':[(b'origin',b'https://testserver'),(b'authorization',('Token '+self.key).encode())],
                'query_string':('project_id='+project).encode(),'subprotocols':[]}
            socket=ApplicationCommunicator(gateway,scope)
            try:
                await socket.send_input({'type':'websocket.connect'})
                self.assertEqual((await socket.receive_output(2))['type'],'websocket.accept')
                ready=await socket.receive_output(2)
                self.assertIn('ready',ready['text'])
                self.assertEqual(bus.active,1)
                await runtime.database(lambda: get_user_model().objects.filter(pk=self.user.pk).update(is_active=False))
                self.assertEqual((await socket.receive_output(2))['code'],4403)
                await socket.wait(2)
                self.assertEqual(bus.active,0)
                self.assertEqual(gateway.active,0)
            finally:
                socket.stop()
                await gateway.aclose()
                await runtime.aclose()
        with override_settings(ALLOWED_HOSTS=['testserver']):
            async_to_sync(run)()

    def test_private_unix_stream_is_real_bytes_not_generic_transport(self):
        from mop.server.realtime.terminal import UnixTerminalStream
        async def run():
            received=[]
            async def serve(reader,writer):
                writer.write(b'actual-pty-output')
                await writer.drain()
                received.append(await reader.read(10))
                writer.close()
                await writer.wait_closed()
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as folder:
                path=str(Path(folder)/'pty.sock')
                server=await asyncio.start_unix_server(serve,path)
                stream=UnixTerminalStream(path)
                await stream.start()
                self.assertEqual(await stream.read(),b'actual-pty-output')
                self.assertEqual(await stream.write(b'key'),3)
                await stream.aclose()
                server.close()
                await server.wait_closed()
                await asyncio.sleep(.01)
                self.assertEqual(received,[b'key'])
        async_to_sync(run)()

    def test_browser_session_ws_is_revoked_by_actual_session_logout(self):
        from django.test import Client
        from django.contrib.sessions.models import Session
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.events import MemoryEvents
        from mop.server.controller.runtime import Runtime
        client=Client()
        client.force_login(self.user,backend='mop.server.controller.authbackend.ControllerBackend')
        session_key=client.session.session_key
        self.auth.bind_session(self.user.pk,session_key)
        async def run():
            runtime=Runtime()
            source=MemoryEvents()
            gateway=Gateway(runtime=runtime,subscriber=source,recheck_interval=.05)
            scope={'type':'websocket','path':'/ws/v1/events','scheme':'wss','server':('testserver',443),
                'headers':[(b'origin',b'https://testserver'),(b'cookie',('sessionid='+session_key).encode())],
                'query_string':('project_id='+str(self.project.pk)).encode(),'subprotocols':[]}
            socket=ApplicationCommunicator(gateway,scope)
            try:
                await socket.send_input({'type':'websocket.connect'})
                self.assertEqual((await socket.receive_output(2))['type'],'websocket.accept')
                await socket.receive_output(2)
                await runtime.database(lambda: Session.objects.filter(pk=session_key).delete())
                self.assertEqual((await socket.receive_output(2))['code'],4403)
                await socket.wait(2)
                self.assertEqual(source.active,0)
                self.assertEqual(gateway.active,0)
            finally:
                socket.stop()
                await gateway.aclose();await runtime.aclose()
        with override_settings(ALLOWED_HOSTS=['testserver']):
            async_to_sync(run)()

    def test_origin_scope_and_crud_are_refused_without_broker_or_effects(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.events import MemoryEvents
        from mop.server.controller.runtime import Runtime
        import json
        async def run():
            runtime=Runtime()
            source=MemoryEvents()
            gateway=Gateway(runtime=runtime,subscriber=source)
            async def connect(origin,query,expected,frame=None):
                scope={'type':'websocket','path':'/ws/v1/events','scheme':'wss','server':('testserver',443),
                    'headers':[(b'origin',origin),(b'authorization',('Token '+self.key).encode())],
                    'query_string':query.encode(),'subprotocols':[]}
                socket=ApplicationCommunicator(gateway,scope)
                try:
                    await socket.send_input({'type':'websocket.connect'})
                    if frame is not None:
                        self.assertEqual((await socket.receive_output(2))['type'],'websocket.accept')
                        await socket.receive_output(2)
                        await socket.send_input({'type':'websocket.receive','text':json.dumps(frame)})
                    self.assertEqual((await socket.receive_output(2))['code'],expected)
                    await socket.wait(2)
                    self.assertEqual(source.active,0)
                finally:
                    socket.stop()
            try:
                query='project_id='+str(self.project.pk)
                await connect(b'https://evil.invalid',query,4403)
                await connect(b'null',query,4403)
                await connect(b'https://testserver','project_id='+str(uuid4()),4403)
                await connect(b'https://testserver',query+'&token=not-a-url-credential',4400)
                await connect(b'https://testserver',query,4400,{'type':'restart','target_id':str(self.target.pk)})
            finally:
                await gateway.aclose();await runtime.aclose()
        with override_settings(ALLOWED_HOSTS=['testserver']):
            async_to_sync(run)()

    def test_gateway_overflow_emits_resync_and_releases_actual_connection_tasks(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.events import MemoryEvents
        from mop.server.controller.runtime import Runtime
        import json
        async def run():
            runtime=Runtime()
            source=MemoryEvents(maximum=2)
            gateway=Gateway(runtime=runtime,subscriber=source)
            socket=ApplicationCommunicator(gateway,{'type':'websocket','path':'/ws/v1/events','scheme':'wss',
                'headers':[(b'authorization',('Token '+self.key).encode())],'server':('testserver',443),
                'query_string':('project_id='+str(self.project.pk)).encode(),'subprotocols':[]})
            try:
                await socket.send_input({'type':'websocket.connect'})
                await socket.receive_output(2);await socket.receive_output(2)
                event={'event_id':str(uuid4()),'epoch':str(uuid4()),'project_ids':[str(self.project.pk)]}
                for _ in range(3):
                    await source.publish(event)
                self.assertEqual(json.loads((await socket.receive_output(2))['text'])['type'],'reset_required')
                self.assertEqual((await socket.receive_output(2))['code'],4409)
                await socket.wait(2)
                self.assertEqual(source.active,0)
                self.assertEqual(gateway.active,0)
                self.assertEqual(runtime.pending,0)
            finally:
                socket.stop()
                await gateway.aclose();await runtime.aclose()
        async_to_sync(run)()

    def test_cancel_during_subscribe_releases_late_broker_resource(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.events import MemoryEvents
        from mop.server.controller.runtime import Runtime
        async def run():
            opened,release=asyncio.Event(),asyncio.Event()
            class Delayed(MemoryEvents):
                async def subscribe(inner,projects):
                    sub=await super().subscribe(projects)
                    opened.set()
                    await release.wait()
                    return sub
            source=Delayed()
            runtime=Runtime()
            gateway=Gateway(runtime=runtime,subscriber=source)
            socket=ApplicationCommunicator(gateway,{'type':'websocket','path':'/ws/v1/events','scheme':'wss',
                'headers':[(b'authorization',('Token '+self.key).encode())], 'server':('testserver',443),
                'query_string':('project_id='+str(self.project.pk)).encode(),'subprotocols':[]})
            try:
                await socket.send_input({'type':'websocket.connect'})
                await asyncio.wait_for(opened.wait(),2)
                socket.future.cancel()
                release.set()
                await socket.wait(2)
                self.assertEqual(source.active,0)
                self.assertEqual(gateway.active,0)
            finally:
                release.set()
                socket.stop(exceptions=False)
                await gateway.aclose();await runtime.aclose()
        async_to_sync(run)()

    def test_ws_catchup_preserves_626_results_and_cursor_without_nats_payload(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.events import MemoryEvents
        from mop.server.controller.runtime import Runtime
        import json
        epoch,event_id=str(uuid4()),str(uuid4())
        item={'schema_version':1,'event_id':event_id,'epoch':epoch,'project_id':str(self.project.pk),
            'type':'puppet.changed','entity':{'kind':'puppet','id':str(self.target.pk),
            'incarnation':str(self.inc.pk),'revision':'123','deleted':False}}
        class History:
            def after(inner,actor,projects,cursor):
                return {'schema_version':1,'epoch':epoch,'cursor':'next-opaque','results':[item],
                        'coverage':['observations','explicit_owner_publication']}
        async def run():
            runtime=Runtime()
            gateway=Gateway(runtime=runtime,subscriber=MemoryEvents(),history=History())
            scope={'type':'websocket','path':'/ws/v1/events','scheme':'wss','server':('testserver',443),
                'headers':[(b'authorization',('Token '+self.key).encode())],
                'query_string':('project_id='+str(self.project.pk)).encode(),'subprotocols':[]}
            socket=ApplicationCommunicator(gateway,scope)
            try:
                await socket.send_input({'type':'websocket.connect'})
                self.assertEqual((await socket.receive_output(2))['type'],'websocket.accept')
                await socket.receive_output(2)
                await socket.send_input({'type':'websocket.receive','text':json.dumps({'type':'subscribe','cursor':'snapshot-opaque'})})
                frame=await socket.receive_output(2)
                self.assertEqual(frame['type'],'websocket.send')
                frame=json.loads(frame['text'])
                self.assertEqual(frame['type'],'events')
                self.assertEqual(frame['results'],[item])
                self.assertEqual(frame['cursor'],'next-opaque')
                self.assertEqual(frame['coverage'],['observations','explicit_owner_publication'])
            finally:
                await socket.send_input({'type':'websocket.disconnect','code':1000})
                await socket.wait(2)
                await gateway.aclose();await runtime.aclose()
        with override_settings(ALLOWED_HOSTS=['testserver']):
            async_to_sync(run)()

    def test_ws_terminal_checks_after_wait_and_never_replays_input(self):
        from mop.server.realtime.gateway import Gateway
        from mop.server.realtime.terminal import RootTerminalStream
        from mop.server.controller.runtime import Runtime
        from _terminal_fixture import NativePtyResolver
        import os,tty,json
        attachment=self.reserve()
        async def run():
            runtime=Runtime()
            master,slave=os.openpty()
            os.set_blocking(master,False)
            os.set_blocking(slave,False)
            tty.setraw(slave)
            release=asyncio.Event()
            key_seen=asyncio.Event()
            received=[]
            async def serve():
                while True:
                    try:
                        received.append(os.read(slave,32))
                        break
                    except BlockingIOError:
                        await asyncio.sleep(.001)
                key_seen.set()
                await release.wait()
                os.write(slave,b'private-after-wait')
            server=asyncio.create_task(serve())
            root=RootTerminalStream(NativePtyResolver(master,attachment))
            gateway=Gateway(runtime=runtime,streams=root,recheck_interval=5)
            scope={'type':'websocket','path':'/ws/v1/terminal/'+attachment['id'],
                'scheme':'wss','server':('testserver',443),'headers':[
                (b'origin',b'https://testserver'),(b'authorization',('Token '+self.key).encode())],
                'query_string':b'','subprotocols':[]}
            socket=ApplicationCommunicator(gateway,scope)
            try:
                await socket.send_input({'type':'websocket.connect'})
                self.assertEqual((await socket.receive_output(2))['type'],'websocket.accept')
                self.assertEqual(json.loads((await socket.receive_output(2))['text'])['type'],'opened')
                frame={'type':'input','seq':1,'data':'a2V5cw=='}
                await socket.send_input({'type':'websocket.receive','text':json.dumps(frame)})
                ack=json.loads((await socket.receive_output(2))['text'])
                self.assertEqual((ack['type'],ack['seq'],ack['outcome']),('ack',1,'written'))
                await asyncio.wait_for(key_seen.wait(),2)
                await runtime.database(lambda: get_user_model().objects.filter(pk=self.user.pk).update(is_active=False))
                release.set()
                self.assertEqual((await socket.receive_output(2))['code'],4403)
                await socket.wait(3)
                self.assertEqual(received,[b'keys'])
                self.assertEqual(gateway.active,0)
                self.assertTrue(os.isatty(master))
            finally:
                release.set()
                socket.stop()
                await gateway.aclose()
                await runtime.aclose()
                await server
                os.close(master);os.close(slave)
        with override_settings(ALLOWED_HOSTS=['testserver']):
            async_to_sync(run)()

    def test_native_tls_nats_acl_and_memory_port_have_same_overflow_cleanup(self):
        from mop.server.realtime.events import NatsEvents, MemoryEvents, ResyncRequired
        from _nats_fixture import broker
        async def exercise(source,publisher):
            sub=await source.subscribe((self.project.pk,))
            event={'event_id':str(uuid4()),'epoch':str(uuid4()),'project_ids':[str(self.project.pk)]}
            await publisher.publish(event)
            await asyncio.wait_for(sub.get(),2)
            await sub.aclose()
            self.assertEqual(source.active,0)
            sub=await source.subscribe((self.project.pk,))
            sub.offer();sub.offer();sub.offer()
            with self.assertRaises(ResyncRequired):
                await sub.get()
            await sub.aclose()
            self.assertEqual(source.active,0)
        async def run():
            memory=MemoryEvents(maximum=2)
            await exercise(memory,memory)
            async with broker(self.project.pk,uuid4()) as fixture:
                kwargs={key:fixture[key] for key in ('servers','tls')}
                gateway=NatsEvents(installation='fixture',user='gateway',password='fixture-gateway',maximum=2,**kwargs)
                publisher=NatsEvents(installation='fixture',user='publisher',password='fixture-publisher',**kwargs)
                await gateway.start();await publisher.start()
                try:
                    await exercise(gateway,publisher)
                    # Пустой allow list не должен молча разрешить весь NATS namespace.
                    with self.assertRaises(ResyncRequired):
                        await gateway.publish({'event_id':str(uuid4()),'epoch':str(uuid4()),
                            'project_ids':[str(self.project.pk)]})
                    with self.assertRaises(ResyncRequired):
                        await publisher.subscribe((self.project.pk,))
                    try:
                        await gateway.subscribe((uuid4(),))
                    except ResyncRequired:
                        pass
                    else:
                        self.fail('cross-project subscription accepted; broker log: '+
                                  __import__('pathlib').Path(fixture['log']).read_text())
                    self.assertEqual(gateway.active,0)
                    node=NatsEvents(installation='fixture',user='node',password='fixture-node',**kwargs)
                    await node.start()
                    try:
                        with self.assertRaises(ResyncRequired):
                            await node.subscribe((self.project.pk,))
                        self.assertEqual(node.active,0)
                    finally:
                        await node.aclose()
                    with self.assertRaises(ResyncRequired):
                        await publisher.publish({'event_id':str(uuid4()),'epoch':str(uuid4()),'project_ids':[str(uuid4())]})
                    sub=await gateway.subscribe((self.project.pk,))
                    await fixture['revoke_gateway']()
                    try:
                        with self.assertRaises(ResyncRequired):
                            await asyncio.wait_for(sub.get(),3)
                    finally:
                        await sub.aclose()
                    self.assertEqual(gateway.active,0)
                finally:
                    await gateway.aclose();await publisher.aclose()
        async_to_sync(run)()

    def test_native_wss_channels_and_owned_nats_cleanup(self):
        from mop.server.realtime.events import NatsEvents
        from mop.server.realtime.gateway import Gateway
        from mop.server.controller.runtime import Runtime
        from _nats_fixture import broker
        import aiohttp,uvicorn,socket,json
        async def run():
            async with broker(self.project.pk,uuid4()) as fixture:
                runtime=Runtime()
                source=NatsEvents(installation='fixture',user='gateway',password='fixture-gateway',
                    servers=fixture['servers'],tls=fixture['tls'])
                await source.start()
                gateway=Gateway(runtime=runtime,subscriber=source,recheck_interval=.1)
                listener=socket.socket()
                listener.bind(('127.0.0.1',0))
                listener.setblocking(False)
                port=listener.getsockname()[1]
                server=uvicorn.Server(uvicorn.Config(gateway,host='127.0.0.1',port=port,
                    ssl_certfile=fixture['cert'],ssl_keyfile=fixture['key'],lifespan='off',log_level='error'))
                task=asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    for _ in range(100):
                        if server.started:
                            break
                        await asyncio.sleep(.02)
                    self.assertTrue(server.started)
                    async with aiohttp.ClientSession() as client:
                        url=f'wss://127.0.0.1:{port}/ws/v1/events?project_id={self.project.pk}'
                        async with client.ws_connect(url,ssl=fixture['tls'],headers={
                            'Authorization':'Token '+self.key,'Origin':f'https://127.0.0.1:{port}'}) as ws:
                            self.assertEqual((await ws.receive_json(timeout=2))['type'],'ready')
                            self.assertEqual(source.active,1)
                            # Logout/token rotation на открытом native TLS socket.
                            await runtime.database(self.auth.rotate_token,self.actor)
                            message=await ws.receive(timeout=2)
                            self.assertEqual(message.type,aiohttp.WSMsgType.CLOSE)
                            self.assertEqual(message.data,4403)
                    for _ in range(100):
                        if not gateway.active:
                            break
                        await asyncio.sleep(.01)
                    self.assertEqual(gateway.active,0)
                    self.assertEqual(source.active,0)
                finally:
                    server.should_exit=True
                    await asyncio.wait_for(task,5)
                    listener.close()
                    await gateway.aclose();await runtime.aclose()
        with override_settings(ALLOWED_HOSTS=['127.0.0.1']):
            async_to_sync(run)()

    def test_borrowed_pty_cannot_silently_change_body_fd_flags(self):
        from mop.server.realtime.terminal import PtyTerminalStream, TerminalUnavailable
        import os
        async def run():
            master,slave=os.openpty()
            stream=PtyTerminalStream(master)
            try:
                self.assertTrue(os.get_blocking(master))
                with self.assertRaises(TerminalUnavailable):
                    await stream.start()
                self.assertTrue(os.get_blocking(master))
            finally:
                await stream.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()

    def test_real_pty_attach_readonly_and_close_preserve_body(self):
        from mop.server.realtime.terminal import PtyTerminalStream
        import os, termios
        async def run():
            master,slave=os.openpty()
            os.set_blocking(master,False)
            attrs=termios.tcgetattr(slave)
            attrs[3] &= ~(termios.ECHO|termios.ICANON)
            termios.tcsetattr(slave,termios.TCSANOW,attrs)
            stream=PtyTerminalStream(master,writable=False)
            try:
                await stream.start()
                os.write(slave,b'fixture-output')
                self.assertEqual(await asyncio.wait_for(stream.read(),2),b'fixture-output')
                with self.assertRaises(PermissionError):
                    await stream.resize(120,40)
                with self.assertRaises(PermissionError):
                    await stream.write(b'keys')
                await stream.aclose()
                self.assertTrue(os.isatty(master),'detach must not close the body owner descriptor')
                def physical_guard(*_):
                    self.assertTrue(os.isatty(master))
                writer=PtyTerminalStream(master,writable=True,guard=physical_guard)
                try:
                    await writer.start()
                    await writer.resize(120,40)
                    self.assertEqual(termios.tcgetwinsize(slave),(40,120))
                finally:
                    await writer.aclose()
            finally:
                await stream.aclose()
                os.close(master);os.close(slave)
        async_to_sync(run)()
