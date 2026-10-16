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
по 21/21 green на owned PostgreSQL 16/17, с настоящими WSS/TLS/NATS/Unix/PTY
и независимыми gateway processes. Full strict CI — отдельный gate точного SHA.
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
        self.assertEqual(self.reserve()['mode'],'rw')

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
        from mop.server.realtime.terminal import RootTerminalStream, UnixTerminalStream
        from mop.server.controller.runtime import Runtime
        import tempfile,json
        from pathlib import Path
        attachment=self.reserve()
        async def run():
            runtime=Runtime()
            release=asyncio.Event()
            key_seen=asyncio.Event()
            received=[]
            async def serve(reader,writer):
                received.append(await reader.read(32))
                key_seen.set()
                await release.wait()
                writer.write(b'private-after-wait')
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            with tempfile.TemporaryDirectory() as folder:
                path=str(Path(folder)/'private.sock')
                server=await asyncio.start_unix_server(serve,path)
                async def resolver(context):
                    return UnixTerminalStream(path)
                gateway=Gateway(runtime=runtime,streams=RootTerminalStream(resolver),recheck_interval=5)
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
                    await socket.wait(2)
                    self.assertEqual(received,[b'keys'])
                    self.assertEqual(gateway.active,0)
                finally:
                    release.set()
                    socket.stop()
                    await gateway.aclose()
                    await runtime.aclose()
                    server.close();await server.wait_closed()
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
                writer=PtyTerminalStream(master,writable=True)
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
