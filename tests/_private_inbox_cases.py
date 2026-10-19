"""Owned PG17/private WS #763: контролируемый peer, не native proof.

HYPOTHESIS: production root не связывает protected take с исходным credential.
SPEC: конечная schema, current human authority, одна попытка, COMMIT receipts.
SOLUTION: один ASGI Runtime владеет исходным credential, конечным peer,
protected jobs и cleanup; metadata живёт в отдельной короткой DB lane.
RESULT: маршрут 4404, пропущенный rollback, writer lock вне deadline и
последовательный drain дали RED до исправлений. Одного PQfinish оказалось
недостаточно: точный pg_sleep остался активен; один access_log=False также
оставил rejected query в uvicorn.error. Общий SQL deadline и warning listener
дали GREEN: 28 private проверок и шесть настоящих UID gates на owned PG17.
STATUS: FIXED — see #763.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
from threading import Event
from unittest.mock import patch
from uuid import UUID, uuid4

from asgiref.sync import async_to_sync
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.db import connection
from django.test import override_settings

from _endpoint_credential_cases import EndpointCredentialFixture
from mop.server.controller import credentials as service
from mop.server.controller.runtime import Runtime
from mop.server.sessions import endpoint_models as m


class PrivateInboxCases(EndpointCredentialFixture):
    def setUp(self):
        super().setUp()
        self.metadata.disconnect(self.context)
        with override_settings(BOOTSTRAP={**settings.BOOTSTRAP, 'role':'credential'}):
            self.protected = service.application(provider=self.provider)

    @asynccontextmanager
    async def root(self):
        from mop.server.controller.asgi import ControllerApplication
        daemon_runtime = Runtime(queue_limit=0)
        daemon = service.CredentialService(self.path, application=self.protected, runtime=daemon_runtime,
            allowed_uids=(os.geteuid(),))
        await daemon.start()
        runtime = Runtime()
        bootstrap = {**settings.BOOTSTRAP, 'role':'web',
            'credential_service':dict(path=self.path, server_uid=os.geteuid())}
        with override_settings(BOOTSTRAP=bootstrap, ALLOWED_HOSTS=['testserver','localhost','127.0.0.1']):
            app = ControllerApplication(runtime=runtime, startup=lambda: None)
            await app.gateway.start()
            app.started = True
            try:
                yield app, daemon
            finally:
                app.started = False
                await app.gateway.aclose()
                await runtime.aclose()
                await daemon.aclose()
                await daemon_runtime.aclose()
        self.assertEqual(runtime.pending, 0)

    def scope(self, **changes):
        return dict(dict(type='websocket', path='/ws/v1/inbox/'+str(self.endpoint.id), scheme='wss',
            headers=[(b'authorization',('Token '+self.auth.value).encode())], query_string=b'',
            server=('testserver',443), subprotocols=[]), **changes)

    async def send_wire(self, ws, kind, **fields):
        await ws.send_input({'type':'websocket.receive',
            'text':json.dumps(dict(schema_version=1, type=kind, **fields))})

    async def receive_wire(self, ws):
        reply = await ws.receive_output(3)
        self.assertEqual(reply['type'], 'websocket.send', reply)
        return json.loads(reply['text'])

    async def bind(self, app):
        ws = ApplicationCommunicator(app, self.scope())
        await ws.send_input({'type':'websocket.connect'})
        self.assertEqual((await ws.receive_output(3))['type'], 'websocket.accept')
        self.assertFalse(await app.runtime.database(lambda:m.ConnectionLease.objects.get(endpoint_id=self.endpoint.id).connected))
        await self.send_wire(ws,'peer_ready',external_session_id=self.endpoint.external_session_id)
        bound = await self.receive_wire(ws)
        self.assertEqual(set(bound), {'schema_version','type','endpoint_id','external_session_id','epoch',
            'generation','connection_nonce','lease_seconds'})
        self.assertEqual(bound['type'],'bound')
        self.assertEqual(bound['lease_seconds'],15)
        await self.send_wire(ws,'bound_received',connection_nonce=bound['connection_nonce'])
        return ws, bound['connection_nonce']

    async def eof(self, ws):
        await ws.send_input({'type':'websocket.disconnect','code':1000})
        await ws.wait(7)

    def test_default_root_protected_take_prewrite_committed_acceptance_idle(self):
        async def run():
            async with self.root() as (app, daemon):
                ws, nonce = await self.bind(app)
                try:
                    sent = await daemon.runtime.blocking(self.send,
                        __import__('mop.common.credential_client',fromlist=['CredentialClient']).CredentialClient(
                            self.path, server_uid=os.geteuid()))
                    delivery = await self.receive_wire(ws)
                    self.assertEqual(delivery['type'],'delivery')
                    frame = delivery['frame']
                    self.assertEqual((frame['id'],frame['text']), (str(sent.id),self.values['text']))
                    self.assertNotEqual(frame['id'],frame['message_id'])
                    self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'attempting')
                    ids = dict(connection_nonce=nonce,delivery_id=frame['id'],message_id=frame['message_id'])
                    await self.send_wire(ws,'prewrite',**ids)
                    self.assertEqual((await self.receive_wire(ws))['type'],'write_allowed')
                    self.assertFalse(await app.runtime.database(m.DeliveryReceipt.objects.exists))
                    await self.send_wire(ws,'application_acceptance',outcome='accepted',**ids)
                    receipt = await self.receive_wire(ws)
                    self.assertEqual(receipt['result']['state'],'accepted')
                    self.assertEqual(await app.runtime.database(m.DeliveryReceipt.objects.count),1)
                    await self.send_wire(ws,'idle_observed',**ids)
                    receipt = await self.receive_wire(ws)
                    self.assertTrue(receipt['result']['idle'])
                    self.assertEqual(await app.runtime.database(m.DeliveryReceipt.objects.count),2)
                finally:
                    await self.eof(ws)
                self.assertFalse(await app.runtime.database(lambda:m.ConnectionLease.objects.get(endpoint_id=self.endpoint.id).connected))
        async_to_sync(run)()

    def test_protocol_rejects_wrong_conversation_binary_duplicate_and_unknown_fields(self):
        async def run():
            async with self.root() as (app, _):
                cases = [
                    {'bytes':b'{}'},
                    {'text':'{"schema_version":1,"schema_version":1,"type":"peer_ready","external_session_id":"receiver-conversation"}'},
                    {'text':json.dumps(dict(schema_version=True,type='peer_ready',external_session_id='receiver-conversation'))},
                    {'text':json.dumps(dict(schema_version=1,type='peer_ready',external_session_id='receiver-conversation',actor={}))},
                    {'text':json.dumps(dict(schema_version=1,type='peer_ready',external_session_id='other'))},
                    {'text':'['*40+'0'+']'*40},
                    {'text':'{"schema_version":1,"type":"peer_ready","external_session_id":NaN}'},
                    {'text':json.dumps(dict(schema_version=1,type='peer_ready',external_session_id='é'*4100))},
                ]
                for frame in cases:
                    ws = ApplicationCommunicator(app,self.scope())
                    await ws.send_input({'type':'websocket.connect'})
                    self.assertEqual((await ws.receive_output(3))['type'],'websocket.accept')
                    await ws.send_input(dict(type='websocket.receive',**frame))
                    reply=await ws.receive_output(3)
                    self.assertIn(reply.get('code'),(4400,4403))
                    await ws.wait(3)
                self.assertFalse(await app.runtime.database(lambda:m.ConnectionLease.objects.get(endpoint_id=self.endpoint.id).connected))
        async_to_sync(run)()

    def test_recipient_revocation_closes_independently_during_protected_provider(self):
        entered, release = Event(), Event()
        def wait():
            entered.set()
            release.wait(8)
        self.after_decrypt = wait
        async def run():
            async with self.root() as (app, daemon):
                ws, _ = await self.bind(app)
                from mop.common.credential_client import CredentialClient
                await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                try:
                    deadline=asyncio.get_running_loop().time()+3
                    while not entered.is_set() and asyncio.get_running_loop().time()<deadline:
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.is_set())
                    from mop.server.access.models import ClientOwnership
                    await app.runtime.database(lambda:ClientOwnership.objects.filter(user=self.user).delete())
                    start=asyncio.get_running_loop().time()
                    reply=await ws.receive_output(5)
                    self.assertEqual(reply.get('code'),4403)
                    self.assertLess(asyncio.get_running_loop().time()-start,5)
                finally:
                    release.set()
                    await ws.wait(7)
                self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get().state),'unknown')
                self.assertFalse(await app.runtime.database(lambda:m.MessageDelivery.objects.get().payload))
        try:
            async_to_sync(run)()
        finally:
            release.set()

    def test_protected_binding_owns_original_credential_worker_and_shared_capacity(self):
        from mop.server.controller import endpointbinding
        self.assertTrue(hasattr(endpointbinding,'ProtectedReceivers'),
            'original-credential-bound owned protected take port is missing')
        from mop.common.endpoint import TrustedEndpointContext
        from mop.server.controller.runtime import Backpressure
        async def run():
            runtime=Runtime(blocking_limit=1,queue_limit=0)
            entered, release=Event(),Event()
            root=endpointbinding.ProtectedReceivers(runtime,self.metadata)
            context=TrustedEndpointContext(self.context.user_id,self.context.credential_id,self.context.session_id,
                self.context.endpoint_id,self.context.gateway_id,self.context.generation,self.context.nonce)
            receiver=root.bind(self.auth,context)
            def blocked(credential,**locators):
                self.assertIs(credential,self.auth)
                self.assertEqual(locators,dict(endpoint_id=context.endpoint_id,connection_nonce=context.nonce))
                self.assertFalse(connection.in_atomic_block)
                self.assertTrue(__import__('threading').current_thread().name.startswith('mop-controller-io'))
                entered.set();release.wait(8)
                return None
            with patch.object(self.metadata,'take_private_delivery',blocked):
                take=asyncio.create_task(root.take_delivery(context))
                try:
                    for _ in range(200):
                        if entered.is_set():break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.is_set())
                    self.assertNotIn(self.auth.value,repr(receiver))
                    take.cancel();await asyncio.gather(take,return_exceptions=True)
                    self.assertGreater(runtime.pending,0)
                    with self.assertRaises(Backpressure):
                        await runtime.blocking(lambda:None)
                    receiver.close()
                    release.set()
                    await root.aclose()
                    self.assertEqual(runtime.pending,0)
                finally:
                    release.set()
                    await root.aclose();await runtime.aclose()
        async_to_sync(run)()

    def test_partial_startup_rolls_back_gateway_before_runtime(self):
        from mop.server.controller.asgi import ControllerApplication
        async def run():
            calls=[]
            runtime=Runtime()
            class FailingGateway:
                async def start(inner):
                    calls.append('start')
                    raise RuntimeError('fixture partial start')
                async def aclose(inner):
                    self.assertTrue(runtime.accepting)
                    await runtime.database(lambda:None)
                    calls.append('gateway-close')
            app=ControllerApplication(runtime=runtime,startup=lambda:None,realtime_gateway=FailingGateway())
            life=ApplicationCommunicator(app,dict(type='lifespan'))
            await life.send_input(dict(type='lifespan.startup'))
            self.assertEqual((await life.receive_output(3))['type'],'lifespan.startup.failed')
            await life.wait(3)
            self.assertEqual(calls,['start','gateway-close'])
            self.assertTrue(runtime._closed)
        async_to_sync(run)()

    def test_auth_route_owner_and_session_origin_are_current(self):
        from django.test import Client
        client=Client()
        client.force_login(self.user,backend='mop.server.controller.authbackend.ControllerBackend')
        from mop.server.access import services as access
        access.bind_session(self.user.pk,client.session.session_key)
        cookie=client.cookies[settings.SESSION_COOKIE_NAME].value
        async def run():
            async with self.root() as (app,_):
                auth=[(b'authorization',('Token '+self.auth.value).encode())]
                session=[(b'cookie',(settings.SESSION_COOKIE_NAME+'='+cookie).encode())]
                cases=[(self.scope(headers=[]),4401),
                    (self.scope(headers=auth+auth),4401),
                    (self.scope(headers=auth+session),4401),
                    (self.scope(headers=session),4403),
                    (self.scope(headers=auth+[(b'origin',b'https://foreign.invalid')]),4403),
                    (self.scope(scheme='ws'),4403),
                    (self.scope(query_string=b'token=forbidden'),4400),
                    (self.scope(path='/ws/v1/inbox/'+str(self.endpoint.id)+'/'),4400),
                    (self.scope(path='/ws/v1/inbox/'+str(self.endpoint.id).upper()),4400),
                    (self.scope(path='/ws/v1/inbox/'+str(self.sender_endpoint.id)),4403)]
                for scope,code in cases:
                    ws=ApplicationCommunicator(app,scope)
                    await ws.send_input(dict(type='websocket.connect'))
                    self.assertEqual((await ws.receive_output(3)).get('code'),code)
                    await ws.wait(3)
                ws=ApplicationCommunicator(app,self.scope(headers=session+[(b'origin',b'https://testserver')]))
                await ws.send_input(dict(type='websocket.connect'))
                self.assertEqual((await ws.receive_output(3))['type'],'websocket.accept')
                await self.send_wire(ws,'peer_ready',external_session_id=self.endpoint.external_session_id)
                self.assertEqual((await self.receive_wire(ws))['type'],'bound')
                await self.eof(ws)
        async_to_sync(run)()

    def test_duplicate_live_receiver_cannot_take_over_current_generation(self):
        async def run():
            async with self.root() as (app,_):
                first,nonce=await self.bind(app)
                duplicate=ApplicationCommunicator(app,self.scope())
                try:
                    await duplicate.send_input(dict(type='websocket.connect'))
                    self.assertEqual((await duplicate.receive_output(3))['type'],'websocket.accept')
                    await self.send_wire(duplicate,'peer_ready',external_session_id=self.endpoint.external_session_id)
                    reply=await duplicate.receive_output(3)
                    self.assertEqual(reply['code'],4409)
                    self.assertIn('current receiver',reply['reason'])
                    await duplicate.wait(3)
                    row=await app.runtime.database(lambda:m.ConnectionLease.objects.values('nonce','connected').get(endpoint_id=self.endpoint.id))
                    self.assertEqual(str(row['nonce']),nonce)
                    self.assertTrue(row['connected'])
                    self.assertEqual(len(app.gateway.delivery._peers),1)
                finally:
                    await self.eof(first)
                    await duplicate.wait(3)
        async_to_sync(run)()

    def test_token_revoked_after_acceptance_preserves_committed_receipt_without_idle(self):
        from rest_framework.authtoken.models import Token
        async def run():
            async with self.root() as (app,daemon):
                ws,nonce=await self.bind(app)
                from mop.common.credential_client import CredentialClient
                sent=await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                delivery=(await self.receive_wire(ws))['frame']
                ids=dict(connection_nonce=nonce,delivery_id=delivery['id'],message_id=delivery['message_id'])
                try:
                    await self.send_wire(ws,'prewrite',**ids)
                    self.assertEqual((await self.receive_wire(ws))['type'],'write_allowed')
                    await self.send_wire(ws,'application_acceptance',outcome='accepted',**ids)
                    self.assertEqual((await self.receive_wire(ws))['result']['state'],'accepted')
                    await app.runtime.database(lambda:Token.objects.filter(user=self.user).delete())
                    await self.send_wire(ws,'idle_observed',**ids)
                    self.assertEqual((await ws.receive_output(3)).get('code'),4403)
                    await ws.wait(7)
                    self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'accepted')
                    self.assertEqual(await app.runtime.database(m.DeliveryReceipt.objects.count),1)
                    self.assertFalse(await app.runtime.database(lambda:m.DeliveryReceipt.objects.filter(kind='idle').exists()))
                finally:
                    await self.eof(ws)
        async_to_sync(run)()

    def test_unknown_is_terminal_duplicate_evidence_never_replays_or_upgrades(self):
        async def run():
            async with self.root() as (app,daemon):
                ws,nonce=await self.bind(app)
                from mop.common.credential_client import CredentialClient
                sent=await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                frame=(await self.receive_wire(ws))['frame']
                ids=dict(connection_nonce=nonce,delivery_id=frame['id'],message_id=frame['message_id'])
                await self.send_wire(ws,'application_acceptance',outcome='unknown',**ids)
                self.assertEqual((await self.receive_wire(ws))['result']['state'],'unknown')
                await self.send_wire(ws,'application_acceptance',outcome='unknown',**ids)
                self.assertEqual((await self.receive_wire(ws))['result']['state'],'unknown')
                await self.send_wire(ws,'application_acceptance',outcome='accepted',**ids)
                self.assertEqual((await ws.receive_output(3)).get('code'),4409)
                await ws.wait(7)
                self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'unknown')
                ws,_=await self.bind(app)
                try:
                    self.assertTrue(await ws.receive_nothing(.4))
                    self.assertEqual(await app.runtime.database(m.DeliveryReceipt.objects.count),1)
                finally:
                    await self.eof(ws)
        async_to_sync(run)()

    def test_eof_during_protected_take_retains_worker_and_discards_late_plaintext(self):
        entered,release=Event(),Event()
        self.after_decrypt=lambda:(entered.set(),release.wait(8))
        async def run():
            async with self.root() as (app,daemon):
                ws,_=await self.bind(app)
                from mop.common.credential_client import CredentialClient
                sent=await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                try:
                    for _ in range(200):
                        if entered.is_set(): break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.is_set())
                    await ws.send_input(dict(type='websocket.disconnect',code=1000))
                    await asyncio.sleep(.1)
                    self.assertGreater(app.runtime.pending,0)
                    self.assertTrue(app.gateway.protected.jobs)
                    release.set()
                    await ws.wait(7)
                    self.assertTrue(await ws.receive_nothing(.1))
                    self.assertFalse(app.gateway.protected.receivers)
                    self.assertFalse(app.gateway.delivery._peers)
                    self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'unknown')
                    self.assertFalse(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).payload))
                finally:
                    release.set()
                    await ws.wait(7)
        try:
            async_to_sync(run)()
        finally:
            release.set()

    def test_shutdown_during_protected_take_drains_before_database_close(self):
        entered,release=Event(),Event()
        self.after_decrypt=lambda:(entered.set(),release.wait(8))
        async def run():
            async with self.root() as (app,daemon):
                ws,_=await self.bind(app)
                from mop.common.credential_client import CredentialClient
                sent=await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                life=ApplicationCommunicator(app,dict(type='lifespan'))
                try:
                    for _ in range(200):
                        if entered.is_set(): break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.is_set())
                    await life.send_input(dict(type='lifespan.shutdown'))
                    self.assertEqual((await ws.receive_output(3)).get('code'),1012)
                    self.assertGreater(app.runtime.pending,0)
                    self.assertFalse(app.runtime._closed)
                    release.set()
                    self.assertEqual((await life.receive_output(7))['type'],'lifespan.shutdown.complete')
                    await ws.wait(3)
                    self.assertTrue(app.runtime._closed)
                    self.assertFalse(app.gateway.protected.jobs)
                    self.assertFalse(app.gateway.delivery._peers)
                    self.assertTrue(await ws.receive_nothing(.05))
                    self.assertEqual(await daemon.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'unknown')
                    self.assertFalse(await daemon.runtime.database(m.DeliveryReceipt.objects.exists))
                finally:
                    release.set()
                    await life.wait(7)
                    await ws.wait(7)
        try:
            async_to_sync(run)()
        finally:
            release.set()

    def test_incomplete_protected_close_retains_binding_until_retry(self):
        from mop.server.controller.endpointbinding import ProtectedReceivers
        async def run():
            runtime=Runtime(queue_limit=0)
            root=ProtectedReceivers(runtime,self.metadata)
            receiver=root.bind(self.auth,self.context)
            entered,release=Event(),Event()
            def blocked(*args,**kwargs):
                entered.set();release.wait(9)
            take=None
            try:
                with patch.object(self.metadata,'take_private_delivery',blocked):
                    take=asyncio.create_task(root.take_delivery(self.context))
                    for _ in range(100):
                        if entered.is_set(): break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.is_set())
                    take.cancel();await asyncio.gather(take,return_exceptions=True)
                    with self.assertRaisesRegex(RuntimeError,'shutdown incomplete'):
                        await root.close_receiver(self.context)
                    self.assertIs(root.receivers[self.context],receiver)
                    self.assertIs(receiver.credential,self.auth)
                    self.assertGreater(runtime.pending,0)
                    self.assertTrue(receiver.closing.is_set())
                    release.set()
                    await root.close_receiver(self.context)
                    self.assertIsNone(receiver.credential)
                    self.assertFalse(root.receivers)
            finally:
                release.set()
                if take: await asyncio.gather(take,return_exceptions=True)
                await root.aclose();await runtime.aclose()
        async_to_sync(run)()

    def test_private_eight_jobs_are_owned_until_real_worker_completion(self):
        from dataclasses import replace
        from mop.server.controller.endpointbinding import ProtectedReceivers
        from mop.server.controller.runtime import Backpressure
        async def run():
            runtime=Runtime(queue_limit=0)
            root=ProtectedReceivers(runtime,self.metadata)
            contexts=[replace(self.context,endpoint_id=uuid4(),nonce=uuid4()) for _ in range(9)]
            for context in contexts:
                root.bind(self.auth,context)
            release=Event()
            def blocked(*args,**kwargs):
                release.wait(8)
                return None
            takes=[]
            try:
                with patch.object(self.metadata,'take_private_delivery',blocked):
                    takes=[asyncio.create_task(root.take_delivery(context)) for context in contexts[:8]]
                    for _ in range(100):
                        if runtime._counts['blocking']==8: break
                        await asyncio.sleep(.01)
                    self.assertEqual(len(root.jobs),8)
                    with self.assertRaisesRegex(Backpressure,'capacity exhausted'):
                        await root.take_delivery(contexts[8])
                    for take in takes: take.cancel()
                    await asyncio.gather(*takes,return_exceptions=True)
                    self.assertEqual(len(root.jobs),8)
                    self.assertEqual(runtime._counts['blocking'],8)
                    with self.assertRaises(Backpressure):
                        await runtime.blocking(lambda:None)
                    release.set()
                    await root.aclose()
                    self.assertEqual(runtime.pending,0)
                    self.assertFalse(root.receivers)
            finally:
                release.set()
                await asyncio.gather(*takes,return_exceptions=True)
                await root.aclose();await runtime.aclose()
        async_to_sync(run)()

    def test_queued_cancelled_take_never_starts_socket_and_keeps_admission(self):
        from mop.server.controller.endpointbinding import ProtectedReceivers
        async def run():
            runtime=Runtime(blocking_limit=1,queue_limit=1)
            root=ProtectedReceivers(runtime,self.metadata)
            root.bind(self.auth,self.context)
            entered,release=Event(),Event()
            def blocked():
                entered.set();release.wait(8)
            blocker=asyncio.create_task(runtime.blocking(blocked))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                with patch.object(self.metadata,'take_private_delivery') as socket_call:
                    take=asyncio.create_task(root.take_delivery(self.context))
                    await asyncio.sleep(.05)
                    take.cancel();await asyncio.gather(take,return_exceptions=True)
                    self.assertEqual(len(root.jobs),1)
                    self.assertEqual(runtime._counts['blocking'],2)
                    release.set()
                    await blocker
                    await root.aclose()
                    socket_call.assert_not_called()
                    self.assertEqual(runtime.pending,0)
            finally:
                release.set()
                await blocker
                await root.aclose();await runtime.aclose()
        async_to_sync(run)()

    def test_queued_take_deadline_includes_runtime_queue_and_never_starts_socket(self):
        from mop.server.controller.endpointbinding import ProtectedReceivers
        async def run():
            runtime=Runtime(blocking_limit=1,queue_limit=1)
            root=ProtectedReceivers(runtime,self.metadata)
            root.bind(self.auth,self.context)
            entered,release=Event(),Event()
            def blocked():
                entered.set();release.wait(8)
            blocker=asyncio.create_task(runtime.blocking(blocked))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                with patch.object(self.metadata,'take_private_delivery') as socket_call:
                    started=asyncio.get_running_loop().time()
                    with self.assertRaises(TimeoutError):
                        await root.take_delivery(self.context)
                    self.assertLess(asyncio.get_running_loop().time()-started,5.3)
                    self.assertEqual(len(root.jobs),1)
                    self.assertEqual(runtime._counts['blocking'],2)
                    release.set()
                    await blocker
                    await root.aclose()
                    socket_call.assert_not_called()
            finally:
                release.set()
                await blocker
                await root.aclose();await runtime.aclose()
        async_to_sync(run)()

    def test_sql_deadline_fences_actual_pg_wait_without_late_job(self):
        import time
        from mop.server.controller.runtime import realtime_database
        worker={}
        def slow():
            with connection.cursor() as cursor:
                worker['pid']=connection.connection.info.backend_pid
                cursor.execute('SELECT pg_sleep(5)')
        def active():
            with connection.cursor() as cursor:
                # Observer может получить тот же pooled backend; его SELECT
                # не является поздним pg_sleep исходного owned job.
                cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=%s AND state='active' AND query='SELECT pg_sleep(5)')",[worker['pid']])
                return cursor.fetchone()[0]
        async def run():
            runtime=Runtime()
            try:
                started=time.monotonic()
                with self.assertRaises(TimeoutError):
                    await realtime_database(runtime,slow)
                self.assertLess(time.monotonic()-started,2.6)
                for _ in range(100):
                    if not runtime.pending: break
                    await asyncio.sleep(.01)
                self.assertEqual(runtime.pending,0)
                self.assertFalse(await runtime.database(active),'expired SQL is still active on PostgreSQL')
            finally:
                await runtime.aclose()
        async_to_sync(run)()

    def test_only_current_prewrite_can_authorize_the_one_inflight_message(self):
        async def run():
            async with self.root() as (app,daemon):
                from mop.common.credential_client import CredentialClient
                from mop.server.access.models import ClientOwnership
                for kind in ('early-accepted','old-nonce','different-message','reflected-permit','revoked-prewrite'):
                    ws,nonce=await self.bind(app)
                    sent=await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()),
                        message_id=uuid4())
                    frame=(await self.receive_wire(ws))['frame']
                    ids=dict(connection_nonce=nonce,delivery_id=frame['id'],message_id=frame['message_id'])
                    if kind=='early-accepted':
                        await self.send_wire(ws,'application_acceptance',outcome='accepted',**ids)
                    elif kind=='old-nonce':
                        await self.send_wire(ws,'prewrite',**dict(ids,connection_nonce=str(uuid4())))
                    elif kind=='different-message':
                        await self.send_wire(ws,'prewrite',**dict(ids,message_id=str(uuid4())))
                    elif kind=='reflected-permit':
                        await self.send_wire(ws,'write_allowed',**ids)
                    else:
                        await app.runtime.database(lambda:ClientOwnership.objects.filter(user=self.user).delete())
                        await self.send_wire(ws,'prewrite',**ids)
                    self.assertEqual((await ws.receive_output(3)).get('code'),4403 if kind=='revoked-prewrite' else 4400)
                    await ws.wait(7)
                    self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'unknown')
                    self.assertFalse(await app.runtime.database(m.DeliveryReceipt.objects.exists))
        async_to_sync(run)()

    def test_private_pending_cap_is_nested_inside_shared_ws_cap(self):
        async def run():
            async with self.root() as (app,_):
                sockets=[]
                try:
                    for _ in range(16):
                        ws=ApplicationCommunicator(app,self.scope())
                        sockets.append(ws)
                        await ws.send_input(dict(type='websocket.connect'))
                        self.assertEqual((await ws.receive_output(3))['type'],'websocket.accept')
                    self.assertEqual(app.gateway.active,16)
                    extra=ApplicationCommunicator(app,self.scope())
                    await extra.send_input(dict(type='websocket.connect'))
                    self.assertEqual((await extra.receive_output(3)).get('code'),4429)
                    await extra.wait(3)
                    app.gateway.maximum=16
                    events=ApplicationCommunicator(app,self.scope(path='/ws/v1/events'))
                    await events.send_input(dict(type='websocket.connect'))
                    self.assertEqual((await events.receive_output(3)).get('code'),4429)
                    await events.wait(3)
                finally:
                    await asyncio.gather(*(self.eof(ws) for ws in sockets))
        async_to_sync(run)()

    def test_sixteen_current_generations_drain_concurrently_before_database_close(self):
        async def run():
            async with self.root() as (app,daemon):
                sockets=[]
                readers=[]
                async def readiness(ws,nonce,external):
                    while True:
                        reply=await ws.receive_output(5)
                        if reply['type']=='websocket.close': return
                        probe=json.loads(reply['text'])
                        self.assertEqual(probe['type'],'probe')
                        await self.send_wire(ws,'peer_ready_again',connection_nonce=nonce,
                            probe_id=probe['probe_id'],external_session_id=external)
                try:
                    # Только эта проверка изолирует teardown от protected polling;
                    # настоящие bytes/provider/UID проверяются отдельными gates.
                    with patch.object(app.gateway.delivery,'take_delivery',return_value=None):
                        for number in range(16):
                            presence=await app.runtime.database(self.metadata.register,self.actor,
                                client_id=self.client.pk,project_id=self.project.pk,
                                external_session_id='teardown-'+str(number),display_name='same label')
                            ws=ApplicationCommunicator(app,self.scope(path='/ws/v1/inbox/'+str(presence.id)))
                            sockets.append(ws)
                            await ws.send_input(dict(type='websocket.connect'))
                            self.assertEqual((await ws.receive_output(3))['type'],'websocket.accept')
                            await self.send_wire(ws,'peer_ready',external_session_id=presence.external_session_id)
                            bound=await self.receive_wire(ws)
                            await self.send_wire(ws,'bound_received',connection_nonce=bound['connection_nonce'])
                            readers.append(asyncio.create_task(readiness(ws,bound['connection_nonce'],presence.external_session_id)))
                        self.assertEqual(len(app.gateway.delivery._peers),16)
                        life=ApplicationCommunicator(app,dict(type='lifespan'))
                        started=asyncio.get_running_loop().time()
                        await life.send_input(dict(type='lifespan.shutdown'))
                        self.assertEqual((await life.receive_output(10))['type'],'lifespan.shutdown.complete')
                        self.assertLess(asyncio.get_running_loop().time()-started,10)
                        await asyncio.gather(*(ws.wait(3) for ws in sockets))
                        self.assertFalse(app.gateway.protected.receivers)
                        self.assertFalse(app.gateway.delivery._peers)
                        self.assertTrue(app.runtime._closed)
                        self.assertFalse(await daemon.runtime.database(lambda:m.ConnectionLease.objects.filter(connected=True).exclude(endpoint_id=self.sender_endpoint.id).exists()))
                        await asyncio.gather(*readers)
                finally:
                    for reader in readers:
                        if not reader.done(): reader.cancel()
                    await asyncio.gather(*readers,return_exceptions=True)
                    await asyncio.gather(*(self.eof(ws) for ws in sockets),return_exceptions=True)
        async_to_sync(run)()

    def test_protected_backpressure_closes_with_safe_reason_without_claim(self):
        from mop.server.controller.runtime import Backpressure
        async def run():
            async with self.root() as (app,_):
                with patch.object(app.gateway.delivery,'take_delivery',side_effect=Backpressure('private protected take capacity exhausted')):
                    ws,_=await self.bind(app)
                    reply=await ws.receive_output(3)
                    self.assertEqual(reply['code'],4429)
                    self.assertEqual(reply['reason'],'private protected take capacity exhausted')
                    await ws.wait(3)
                self.assertFalse(await app.runtime.database(m.MessageDelivery.objects.exists))
        async_to_sync(run)()

    def test_listener_has_finite_uncompressed_private_frames(self):
        from mop.server.controller.runtime import serve
        with override_settings(ALLOWED_HOSTS=list(settings.ALLOWED_HOSTS)), \
                patch('mop.server.controller.runtime.validate_asgi_startup'), patch('uvicorn.run') as run:
            serve('127.0.0.1',8443)
        self.assertEqual(run.call_args.kwargs.get('ws_max_size'),401408)
        self.assertIs(run.call_args.kwargs.get('ws_per_message_deflate'),False)
        self.assertEqual(run.call_args.kwargs.get('ws_max_queue'),16)
        self.assertIs(run.call_args.kwargs.get('access_log'),False)

    def test_close_send_budget_includes_busy_writer_lock(self):
        from mop.server.realtime.private import PrivateInbox
        async def run():
            async with self.root() as (app,_):
                consumer=PrivateInbox(gateway=app.gateway)
                await consumer.send_lock.acquire()
                closing=asyncio.create_task(consumer.finish(1012))
                try:
                    done,_=await asyncio.wait((closing,),timeout=2.3)
                    self.assertIn(closing,done,'WS close waits outside its two-second budget')
                    self.assertTrue(consumer._closing)
                    self.assertTrue(consumer.stopped.is_set())
                finally:
                    consumer.send_lock.release()
                    if not closing.done(): closing.cancel()
                    await asyncio.gather(closing,return_exceptions=True)
        async_to_sync(run)()

    def test_cancelled_partial_startup_still_owns_gateway_and_runtime_cleanup(self):
        from mop.server.controller.asgi import ControllerApplication
        async def run():
            runtime=Runtime()
            entered,closed=asyncio.Event(),asyncio.Event()
            class Gateway:
                async def start(inner):
                    entered.set()
                    await asyncio.Event().wait()
                async def aclose(inner):
                    self.assertTrue(runtime.accepting)
                    await runtime.database(lambda:None)
                    closed.set()
            app=ControllerApplication(runtime=runtime,startup=lambda:None,realtime_gateway=Gateway())
            life=ApplicationCommunicator(app,dict(type='lifespan'))
            await life.send_input(dict(type='lifespan.startup'))
            await entered.wait()
            life.future.cancel()
            await asyncio.gather(life.future,return_exceptions=True)
            try:
                self.assertTrue(closed.is_set())
                self.assertTrue(runtime._closed)
            finally:
                await runtime.aclose()
        async_to_sync(run)()

    def test_shutdown_closes_gateway_before_admitted_http_drain(self):
        from mop.server.controller.asgi import ControllerApplication
        async def run():
            runtime=Runtime()
            admitted,release,closed=asyncio.Event(),asyncio.Event(),asyncio.Event()
            async def http(scope,receive,send):
                admitted.set()
                await release.wait()
                await send(dict(type='http.response.start',status=200,headers=[]))
                await send(dict(type='http.response.body',body=b''))
            class Gateway:
                async def aclose(inner):
                    self.assertTrue(runtime.accepting)
                    closed.set()
            app=ControllerApplication(runtime=runtime,http=http,realtime_gateway=Gateway())
            app.started=True
            request=ApplicationCommunicator(app,dict(type='http',path='/',method='GET',headers=[]))
            await request.send_input(dict(type='http.request',body=b''))
            await admitted.wait()
            life=ApplicationCommunicator(app,dict(type='lifespan'))
            await life.send_input(dict(type='lifespan.shutdown'))
            try:
                async with asyncio.timeout(1):
                    await closed.wait()
                self.assertFalse(app.started)
                self.assertFalse(runtime._closed)
            finally:
                release.set()
                await request.wait(3)
                await life.receive_output(3)
                await life.wait(3)
        async_to_sync(run)()

    def test_root_shutdown_deadline_retains_unreturned_cleanup_owner(self):
        from mop.server.controller.asgi import ControllerApplication
        async def run():
            runtime=Runtime()
            entered,release,closed=asyncio.Event(),asyncio.Event(),asyncio.Event()
            class Gateway:
                async def aclose(inner):
                    entered.set()
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            continue
                    self.assertTrue(runtime.accepting)
                    closed.set()
            app=ControllerApplication(runtime=runtime,realtime_gateway=Gateway())
            app.started=True
            life=ApplicationCommunicator(app,dict(type='lifespan'))
            await life.send_input(dict(type='lifespan.shutdown'))
            await entered.wait()
            output=asyncio.create_task(life.output_queue.get())
            try:
                done,_=await asyncio.wait((output,),timeout=10.8)
                self.assertIn(output,done,'root shutdown waits past its ten-second budget')
                result=output.result()
                self.assertEqual(result['type'],'lifespan.shutdown.failed')
                self.assertIn('incomplete',result['message'])
                self.assertFalse(runtime._closed)
                self.assertFalse(closed.is_set())
                self.assertTrue(runtime.accepting)
            finally:
                release.set()
                output.cancel()
                await asyncio.gather(output,return_exceptions=True)
                await life.wait(3)
                # Позднее завершение всё ещё принадлежит root, не сборщику GC.
                for _ in range(200):
                    if runtime._closed: break
                    await asyncio.sleep(.01)
                await runtime.aclose()
            self.assertTrue(closed.is_set())
        async_to_sync(run)()

    def test_effect_disable_closes_idle_receiver_within_five_seconds(self):
        from mop.server.controller.models import Installation
        entered,release=Event(),Event()
        def blocked():
            entered.set();release.wait(9)
        self.after_decrypt=blocked
        async def run():
            async with self.root() as (app,daemon):
                ws,_=await self.bind(app)
                from mop.common.credential_client import CredentialClient
                await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                try:
                    for _ in range(200):
                        if entered.is_set(): break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.is_set())
                    await app.runtime.database(lambda:Installation.objects.update(effects_enabled=False))
                    # Probe не задерживает независимый authority close.
                    deadline=asyncio.get_running_loop().time()+5
                    while True:
                        reply=await ws.receive_output(max(.01,deadline-asyncio.get_running_loop().time()))
                        if reply['type']=='websocket.close':
                            self.assertEqual(reply['code'],4403)
                            break
                        value=json.loads(reply['text'])
                        self.assertEqual(value['type'],'probe')
                finally:
                    release.set()
                    await self.eof(ws)
        try:
            async_to_sync(run)()
        finally:
            release.set()

    def test_idle_backlog_is_checked_before_protected_claim(self):
        async def run():
            async with self.root() as (app, daemon):
                ws, nonce=await self.bind(app)
                try:
                    for _ in range(100):
                        if app.gateway.delivery._peers: break
                        await asyncio.sleep(.01)
                    context=next(iter(app.gateway.delivery._peers))
                    app.gateway.delivery._idle_pending[context]={uuid4():uuid4() for _ in range(64)}
                    from mop.common.credential_client import CredentialClient
                    sent=await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                    await asyncio.sleep(.35)
                    self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'queued')
                finally:
                    await self.eof(ws)
        async_to_sync(run)()

    def test_actual_verified_https_and_wss_default_root(self):
        import aiohttp, uvicorn, logging
        from mop.server.controller.runtime import serve
        cert,key = Path(self.directory.name)/'tls.crt', Path(self.directory.name)/'tls.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
            '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        async def run():
            async with self.root() as (app, daemon):
                listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.setblocking(False)
                port=listener.getsockname()[1]
                with patch('mop.server.controller.runtime.validate_asgi_startup'), patch('uvicorn.run') as launch:
                    serve('127.0.0.1',port,hostnames=('localhost','127.0.0.1'),tls_cert=str(cert),tls_key=str(key))
                config=uvicorn.Config(app,**launch.call_args.kwargs)
                server=uvicorn.Server(config)
                records=[]
                class Capture(logging.Handler):
                    def emit(inner,record): records.append(record.getMessage())
                capture=Capture()
                logger=logging.getLogger('uvicorn.error')
                logger.addHandler(capture)
                marker='private-query-'+uuid4().hex
                task=asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    for _ in range(100):
                        if server.started: break
                        await asyncio.sleep(.02)
                    self.assertTrue(server.started)
                    tls=ssl.create_default_context(cafile=str(cert))
                    origin=f'https://localhost:{port}'
                    async with aiohttp.ClientSession(trust_env=False,connector=aiohttp.TCPConnector(ssl=tls)) as client:
                        async with client.get(origin+'/api/v1/addresses/'+str(self.endpoint.id),
                                headers={'Authorization':'Token '+self.auth.value}) as response:
                            self.assertEqual(response.status,200,await response.text())
                        with self.assertRaises(aiohttp.WSServerHandshakeError):
                            await client.ws_connect(origin.replace('https:','wss:')+'/ws/v1/inbox/'+str(self.endpoint.id)+'?nonce='+marker,
                                headers={'Authorization':'Token '+self.auth.value,'Origin':origin},compress=0)
                        async with client.ws_connect(origin.replace('https:','wss:')+'/ws/v1/inbox/'+str(self.endpoint.id),
                                headers={'Authorization':'Token '+self.auth.value,'Origin':origin},compress=0) as ws:
                            await ws.send_json(dict(schema_version=1,type='peer_ready',external_session_id=self.endpoint.external_session_id))
                            bound=await ws.receive_json(timeout=3)
                            await ws.send_json(dict(schema_version=1,type='bound_received',connection_nonce=bound['connection_nonce']))
                            from mop.common.credential_client import CredentialClient
                            sent=await daemon.runtime.blocking(self.send,CredentialClient(self.path,server_uid=os.geteuid()))
                            delivery=await ws.receive_json(timeout=3)
                            self.assertEqual(delivery['frame']['id'],str(sent.id))
                            # EOF после plaintext не является acceptance и не разрешает replay.
                    for _ in range(200):
                        if not app.gateway.active: break
                        await asyncio.sleep(.01)
                    self.assertEqual(app.gateway.active,0)
                    self.assertEqual(await app.runtime.database(lambda:m.MessageDelivery.objects.get(pk=sent.id).state),'unknown')
                    self.assertEqual(await app.runtime.database(m.DeliveryReceipt.objects.count),0)
                finally:
                    server.should_exit=True
                    await asyncio.wait_for(task,7)
                    listener.close()
                    logger.removeHandler(capture)
                for secret in (marker,self.auth.value,self.values['text']):
                    self.assertFalse(any(secret in record for record in records),'listener logged private input')
        async_to_sync(run)()
