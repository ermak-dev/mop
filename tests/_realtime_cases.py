"""Реальный ASGI/ORM контракт #628; никаких credentials старой установки.

HYPOTHESIS: unbound_websocket закрывает даже допустимые подключения, terminal
control не имеет межпроцессного fence. SQL запись не означает живой PTY.
SPEC: Django session/DRF token, Origin, текущий scope перед приватной доставкой,
bounded lifecycle, один writer независимо от gateway, no-replay input.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

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

    def test_reader_cap_browser_binding_no_replay_and_restart_fence(self):
        value=self.reserve()
        attached=self.control.bind(self.actor,uuid4(),value['id'],gateway_id=self.run_id)
        with self.assertRaises(self.tc.Conflict):
            self.control.bind(self.actor,uuid4(),value['id'],gateway_id=self.run_id)
        self.assertEqual(self.control.before_input(self.actor,attached,1)['mode'],'rw')
        with self.assertRaises(self.tc.Conflict):
            self.control.before_input(self.actor,attached,1)
        self.control.release(self.actor,attached)
        with self.assertRaises(self.tc.Conflict):
            self.control.validate(self.actor,attached)
        reader=self.reserve('ro')
        bound=self.control.bind(self.actor,uuid4(),reader['id'],gateway_id=self.run_id)
        with self.assertRaises(PermissionError):
            self.control.before_input(self.actor,bound,1)
        for _ in range(7):
            self.reserve('ro')
        with self.assertRaises(self.tc.Capacity):
            self.reserve('ro')

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
        value=self.reserve(actor=actor)
        with self.assertRaises(PermissionError):
            self.control.bind(self.actor,uuid4(),value['id'],gateway_id=self.run_id)

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
