"""Purpose current view на actual DRF/private socket/PostgreSQL + verified TLS.

HYPOTHESIS: current view ошибочно запрашивает native-only work/launch.
SPEC: exact claimed confirm_ready, finite initial schema без role/consumer bytes,
immutable pins, единственный native ACK; apply_identity не обходит purpose.
Native snapshot здесь контролируемый, не actual native CLI acceptance.
SOLUTION: отдельные strict read_initial_launch/typed approval без bundle;
post-launch apply_identity честно unbound, Registration.matches неизменен.
RESULT: RED — actual claimed readiness получает HTTP 403 native purpose;
после node-only fix 4/4 GREEN: настоящий TLS + private socket/AEAD + PG17,
exact pins/schema/claim token и отзыв credential после response проверены.
STATUS: FIXED — see #733; root explicit LaunchSupport, не production startup.
"""
import asyncio
from dataclasses import replace
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase, override_settings

from _launch_delivery_cases import LaunchDelivery
from _producer_cases import Producers
from mop.driver.launch import RegisteredLayout, Registration
from mop.node.body_launch import SessionRuntime
from mop.node.http_execution import Admission, HttpsTransport, MachineClient, Refused, Unavailable, Work
from mop.node.native_observer import NativeIdentity
from mop.node.producer_registration import ProducerRegistrationClient
from mop.node.producer_views import CurrentLaunchAuthority


@override_settings(ALLOWED_HOSTS=['127.0.0.1', 'testserver'])
class CurrentViewPurpose(TransactionTestCase):
    serialized_rollback = True
    setUp = LaunchDelivery.setUp
    reserved_grant = LaunchDelivery.reserved_grant
    grant_assignment = LaunchDelivery.grant_assignment
    exercise = LaunchDelivery.exercise
    run_delivery = LaunchDelivery.run_delivery
    fixture = Producers.fixture
    url = Producers.url

    def check_current(self, exercise):
        self.fixture()

        def counts():
            from mop.server.sessions.producer_models import SessionProducer, CurrentProducer
            from mop.server.sessions.models import ReadinessReceipt
            try:
                return (SessionProducer.objects.count(), CurrentProducer.objects.get().revision,
                        ReadinessReceipt.objects.count())
            finally:
                connections.close_all()

        async def body(socket_client, socket_server):
            import uvicorn
            from django.core.asgi import get_asgi_application
            with tempfile.TemporaryDirectory(prefix='m733-', dir='/tmp') as directory:
                root = Path(directory)
                root.chmod(0o700)
                cert, key = root/'cert.pem', root/'key.pem'
                generated = await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509',
                    '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', str(key), '-out', str(cert)],
                    capture_output=True, timeout=10)
                self.assertEqual(generated.returncode, 0)
                key.chmod(0o600)
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen(8)
                origin = 'https://127.0.0.1:'+str(listener.getsockname()[1])
                https = uvicorn.Server(uvicorn.Config(get_asgi_application(), lifespan='off',
                    log_level='error', access_log=False, ssl_certfile=str(cert), ssl_keyfile=str(key)))
                server_task = asyncio.create_task(https.serve(sockets=[listener]))
                machine = MachineClient(HttpsTransport(origin, self.machine_key,
                    tls=ssl.create_default_context(cafile=str(cert))))
                runtime = None
                calls = []
                request = machine.transport.request
                async def recorded(method, path, *args, **kwargs):
                    calls.append(path)
                    return await request(method, path, *args, **kwargs)
                machine.transport.request = recorded
                try:
                    async with asyncio.timeout(5):
                        while not https.started:
                            if server_task.done():
                                await server_task
                            await asyncio.sleep(.01)
                    native = Work.parse(self.metadata)
                    registration = Registration(native, str(self.pin.bootstrap_revision),
                        self.pin.bootstrap_digest, self.pin.image_digest, self.assignment.display_name)
                    layout = RegisteredLayout(root, registration)
                    layout.runtime.mkdir(parents=True, mode=0o700)
                    producer = ProducerRegistrationClient(machine, registration,
                        expected_prior=None, expected_revision=0)
                    class NoHooks:
                        async def ingest(inner, row):
                            raise AssertionError('current view cannot fabricate readiness hooks')
                    runtime = SessionRuntime(producer, NoHooks())
                    runtime.prepare(layout, path=layout.runtime/'hook.sock')
                    identity = NativeIdentity('%0', 123, str(uuid4())+':123', 'owned-session',
                        root/'config', (1, 2), (3, 4), (5, 6), (7, 8))
                    with patch('mop.node.native_observer._snapshot', return_value=identity):
                        await runtime.attach()
                        native_proof, ack = runtime._proof, producer.acknowledgement
                        self.assertIsNotNone(ack)
                        native_admission = Admission(native, str(self.attempt.request_id),
                            str(self.attempt.id), self.attempt.revision, self.attempt.lease_until)
                        await machine.result(native_admission, 'succeeded', 'native-started')
                        current, = await machine.poll(native.node_id, native.executor_epoch, 1)
                        self.assertEqual(current.action, 'confirm_ready')
                        admission = await machine.claim(current, str(uuid4()))
                        current_registration = replace(registration, work=current)
                        self.assertFalse(registration.matches(current))
                        # Actual server response: role bytes остаются native-only.
                        with self.assertRaises(Refused):
                            await machine.read_launch(current.wire())
                        payload = await machine.transport.request('POST', 'work/initial-launch',
                            payload=current.wire())
                        self.assertIsNone(payload['role'])
                        self.assertIsNone(payload['bootstrap'])
                        self.assertEqual(payload['consumers'], [])
                        self.assertNotIn('prompt', payload)
                        before = await asyncio.to_thread(counts)
                        self.assertEqual(before, (1, 1, 0))
                        calls.clear()
                        await exercise(machine, runtime, current_registration, admission, calls, payload)
                        self.assertEqual(producer.acknowledgement, ack)
                        self.assertEqual(runtime._proof, native_proof)
                        self.assertEqual(await asyncio.to_thread(counts), before)
                        self.assertNotIn('work/launch', calls)
                        self.assertNotIn('session-producers', calls)
                finally:
                    if runtime is not None:
                        await runtime.aclose()
                    await machine.aclose()
                    https.should_exit = True
                    await asyncio.wait_for(server_task, 5)
                    listener.close()
        # Explicit root composition: production default оставляет create handler
        # unbound; связываем настоящий LaunchSupport, не подменяя HTTP policy.
        with patch('mop.server.controller.machineviews.execution', self.execution):
            self.run_delivery(body)

    def test_claimed_readiness_uses_actual_initial_launch_without_reissuing_native_bytes_or_ack(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            target, inputs = await view.resolve(registration.work)
            proof = await view.proof(registration.work)
            self.assertEqual(target.session_proof, proof)
            self.assertEqual(replace(proof, registration=runtime._layout.registration), runtime._proof)
            self.assertEqual((inputs.git_name, inputs.git_email), ('credential-user', 'owner@example.invalid'))
            self.assertTrue(proof.registration.matches(registration.work))
            self.assertFalse(proof.registration.matches(runtime._layout.registration.work))
            self.assertIn('work/initial-launch', calls)
            self.assertGreaterEqual(calls.count('work/claim'), 2)
        self.check_current(exercise)

    def test_actual_current_claim_recheck_rejects_changed_request_or_durable_token(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            with self.assertRaises(Refused):
                await runtime.current_view(CurrentLaunchAuthority(machine, registration,
                    replace(admission, request_id=str(uuid4()))))
            from mop.server.operations.models import Step
            def change_token():
                try:
                    Step.objects.filter(pk=registration.work.step_id).update(token=uuid4())
                finally:
                    connections.close_all()
            await asyncio.to_thread(change_token)
            with self.assertRaises(Refused):
                await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
        self.check_current(exercise)

    def test_actual_response_is_strictly_parsed_and_launch_pins_are_exact(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            request = machine.transport.request
            changes = ({'bootstrap_revision': str(uuid4())}, {'bootstrap_digest': 'f'*64},
                {'image_digest': 'f'*64}, {'phase': 'native_exec'}, {'schema_version': True},
                {'role': {'manifest': {}, 'prompt': ''}}, {'owner_claim': 'caller'},
                {'consumers': [{'purpose': 'model_credential'}]},
                {'work': {**registration.work.wire(), 'owner_id': '999'}})
            for change in changes:
                async def changed(method, path, *args, **kwargs):
                    value = await request(method, path, *args, **kwargs)
                    return {**value, **change} if path == 'work/initial-launch' else value
                with self.subTest(field=next(iter(change))), \
                        patch.object(machine.transport, 'request', side_effect=changed), self.assertRaises(Refused):
                    await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            # Независимый последний recheck нельзя заменить pre-read approval.
            entered, release = asyncio.Event(), asyncio.Event()
            async def paused(method, path, *args, **kwargs):
                value = await request(method, path, *args, **kwargs)
                if path == 'work/initial-launch':
                    entered.set()
                    await release.wait()
                return value
            def revoke():
                try:
                    get_user_model().objects.get(pk=self.machine.user_id).auth_token.delete()
                finally:
                    connections.close_all()
            with patch.object(machine.transport, 'request', side_effect=paused):
                task = asyncio.create_task(runtime.current_view(CurrentLaunchAuthority(machine, registration, admission)))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    await asyncio.to_thread(revoke)
                    release.set()
                    with self.assertRaises(Refused):
                        await task
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
        self.check_current(exercise)

    def test_postlaunch_identity_refuses_unsupported_purpose_before_native_byte_endpoint(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            identity_work = replace(registration.work, action='apply_identity')
            with self.assertRaisesRegex((Refused, Unavailable), 'identity|purpose'):
                await runtime.current_view(CurrentLaunchAuthority(machine,
                    replace(registration, work=identity_work), replace(admission, work=identity_work)))
        self.check_current(exercise)
