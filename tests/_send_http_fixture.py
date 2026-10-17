"""Собственный HTTPS стенд: production NodeHTTP и явные fixture-only receipts."""
import asyncio
from multiprocessing import get_context
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time


class RegisteredMessageSupport:
    def __init__(self, target_id, incarnation_id):
        self.pin = (target_id, incarnation_id)

    def require(self, value):
        if (value.target_id, value.incarnation_id) != self.pin:
            raise PermissionError('unregistered message fixture target')


def composition(database, bootstrap, target_id, incarnation_id, proof):
    # Spawn создаёт собственный DB transport; fork наследует guardian IPC backend.
    from _operation_process import _configure
    _configure(database, bootstrap)
    from _grant_cases import ApprovalFixture
    from mop.server.access.grants import Grants
    from mop.server.controller.machinebinding import AssignmentBindings, operations, compose
    from mop.server.controller.messagebinding import MessageCipher
    from mop.server.secrets.crypto import XChaCha20Provider, Keyring
    provider = XChaCha20Provider(Keyring('test', {'test':b'm'*32}))
    grants = Grants(provider=provider, approvals=ApprovalFixture(proof), bindings=AssignmentBindings())
    cipher = MessageCipher(provider, 'isolated-ci', digest_key=b'd'*32)
    options = dict(grants=grants, message_cipher=cipher,
        message_support=RegisteredMessageSupport(target_id, incarnation_id))
    return operations(**options), compose(**options)


def submit_process(binding, actor, command, ready, results):
    operations, _ = composition(*binding)
    from django.db import connections
    from mop.server.operations.service import Refused
    try:
        ready.wait(timeout=15)
        try:
            value = operations.submit(actor, command)
            results.put(('accepted', value['id']))
        except Refused as error:
            results.put(('refused', error.code))
    finally:
        connections.close_all()


def claim_process(binding, actor, work, ready, results):
    _, execution = composition(*binding)
    from django.db import connections
    from mop.server.sessions.services import Conflict
    from uuid import uuid4
    try:
        ready.wait(timeout=15)
        try:
            execution.claim(actor, work, request_id=uuid4())
            results.put('claimed')
        except Conflict:
            results.put('conflict')
    finally:
        connections.close_all()


def serve(listener, cert, key, operations, execution):
    from django.conf import settings
    from django.db import connections
    connections.close_all()
    from mop.server.controller import operationviews, machineviews
    operationviews.message_api = lambda: operations
    operationviews.operations_api = lambda: operations
    machineviews.execution = execution
    settings.ALLOWED_HOSTS = ['localhost', '127.0.0.1']
    from django.core.asgi import get_asgi_application
    import uvicorn
    uvicorn.Server(uvicorn.Config(get_asgi_application(), lifespan='off', log_level='error',
        ssl_certfile=str(cert), ssl_keyfile=str(key))).run(sockets=[listener])


def run(case):
    from django.db import connections
    with tempfile.TemporaryDirectory(prefix='mop-send-https-') as directory:
        root = Path(directory)
        cert, key = root/'cert.pem', root/'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0)); listener.listen(16)
        origin = 'https://localhost:'+str(listener.getsockname()[1])
        connections.close_all()
        process = get_context('fork').Process(target=serve, args=(listener,cert,key,case.ops,case.service))
        process.start()
        try:
            asyncio.run(vertical(case, origin, cert))
        finally:
            process.terminate(); process.join(10)
            if process.is_alive():
                process.kill(); process.join(5)
            listener.close()
        if process.is_alive():
            raise AssertionError('owned HTTPS fixture did not stop')


async def vertical(case, origin, cert):
    import aiohttp
    from uuid import uuid4
    from mop.node.http_execution import MachineClient, HttpsTransport
    tls = ssl.create_default_context(cafile=str(cert))
    client = MachineClient(HttpsTransport(origin, case.machine_key, tls=tls, timeout=5))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls), trust_env=False,
            headers={'Authorization':'Token '+case.key}) as human:
        try:
            # Только ожидание собственного HTTPS listener, без повторения POST.
            deadline = time.monotonic()+10
            while True:
                try:
                    async with human.get(origin+'/api/v1/health') as response:
                        await response.read()
                    break
                except aiohttp.ClientError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(.05)
            body = case.envelope()
            async with human.post(origin+'/api/v1/messages', json=body) as response:
                result = await response.json()
                case.assertEqual(response.status, 202, result)
            operation_id = result['operation']['id']
            actions = []
            for expected in ('apply_identity', 'send_message'):
                work, = await client.poll(str(case.node.id), str(case.boot), 1)
                case.assertEqual(work.action, expected)
                admission = await client.claim(work, str(uuid4()))
                value = await client.action_input(work)
                await client.recheck(admission)
                actions.append(work.action)
                if expected == 'send_message':
                    case.assertEqual(value['message'], body['params']['message'])
                    case.assertEqual(value['operation_request_id'], body['request_id'])
                    evidence = 'delivered'
                else:
                    case.assertEqual(value['git_email'], 'owner@example.invalid')
                    evidence = 'identity-applied'
                # Явная controlled fixture квитанция: не production success adapter.
                await client.result(admission, 'succeeded', evidence)
            case.assertEqual(actions, ['apply_identity','send_message'])
            case.assertEqual(await client.poll(str(case.node.id), str(case.boot), 1), ())
            async with human.get(origin+'/api/v1/operations/'+operation_id) as response:
                result = await response.json()
                case.assertEqual(response.status, 200, result)
                case.assertEqual(result['operation']['state'], 'succeeded')
                case.assertFalse(result['operation']['resources_held'])
        finally:
            await client.aclose()
