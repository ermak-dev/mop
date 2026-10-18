"""Настоящая HTTPS -> PostgreSQL -> tmux/Unix socket вертикаль новой цели."""
import asyncio
from multiprocessing import get_context
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
from uuid import UUID, uuid4

from django.db import connections
from django.test import TransactionTestCase, override_settings
import _restart_vertical_cases as vertical
from _restart_fixture import https_server, new_layout


# FEATURE: physical restart подтверждается концом старого scope и новой app ready.
# HYPOTHESIS: отсутствует addressed private input seam, поэтому metadata недостаточно.
# SPEC: настоящий TLS verify, реальная БД и новый tmux; без старого пула/root keys.
# RED: отсутствовал addressed input; затем реальный accepted reply не терялся.
# SOLUTION: настоящий HTTPS stream fault после COMMIT; текущий owner input,
#           kernel peer PID/process group и read-only nonce ACK приложения.
# RESULT: PG16/17 — настоящий restart и controller outage/restart без replay;
#         old scope ended, новая app жива, receipts/outbox committed или unknown.
# STATUS: FIXED — see #625/#639/#640
@override_settings(ALLOWED_HOSTS=['testserver','localhost','127.0.0.1'])
class NativeRestart(TransactionTestCase):
    serialized_rollback = True
    def setUp(self):
        vertical.RestartVertical.setUp(self)
        from mop.server.controller.models import Installation
        self.installation_id = Installation.objects.get().identity

    fixture = vertical.RestartVertical.fixture
    body = vertical.RestartVertical.body

    def test_https_machine_executor_replaces_scope_applies_owner_and_commits_receipt(self):
        self.fixture()
        with tempfile.TemporaryDirectory(prefix='mop-native-restart-') as directory:
            root = Path(directory)
            cert, key = root/'cert.pem', root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                '-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
                '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True, capture_output=True)
            layout = new_layout(root, self.target)
            listener = socket.socket()
            listener.bind(('127.0.0.1',0)); listener.listen(16)
            origin = 'https://localhost:'+str(listener.getsockname()[1])
            connections.close_all()
            server = get_context('fork').Process(target=https_server, args=(listener,cert,key,self.grants,layout))
            server.start()
            try:
                asyncio.run(self.physical_vertical(origin,cert,layout))
                from mop.server.operations import models as ops
                from mop.server.sessions import models as sessions
                self.assertEqual(ops.Operation.objects.get(pk=self.proved_operation_id).state,'succeeded')
                self.assertEqual(ops.EffectReceipt.objects.filter(step__operation_id=self.proved_operation_id).count(),3)
                self.assertEqual(sessions.ResultReceipt.objects.count(),3)
                self.assertEqual(sessions.IdentityApplication.objects.count(),1)
                self.assertEqual(sessions.ReadinessReceipt.objects.count(),1)
                self.assertEqual(ops.OutboxEvent.objects.filter(operation_id=self.proved_operation_id).count(),5)
            finally:
                server.terminate(); server.join(10)
                if server.is_alive():
                    server.kill(); server.join(5)
                listener.close()
                subprocess.run(['tmux','-S',str(root/'tmux.sock'),'kill-server'],check=False,capture_output=True)

    def test_controller_outage_loses_receipt_and_new_executor_fences_late_result(self):
        self.fixture()
        with tempfile.TemporaryDirectory(prefix='mop-lost-restart-') as directory:
            root = Path(directory)
            cert, key = root/'cert.pem',root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                '-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
                '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,capture_output=True)
            layout = new_layout(root,self.target)
            listener = socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(16)
            origin = 'https://localhost:'+str(listener.getsockname()[1])
            connections.close_all()
            def launch():
                process = get_context('fork').Process(target=https_server,args=(listener,cert,key,self.grants,layout))
                process.start()
                return process
            server = launch()
            async def stop():
                server.terminate()
                await asyncio.to_thread(server.join,10)
            try:
                asyncio.run(self.physical_vertical(origin,cert,layout,lose_receipt=stop))
                server = launch()
                asyncio.run(self.recovery_after_outage(origin,cert,layout))
                from mop.server.operations import models as ops
                self.assertEqual(ops.Operation.objects.get(pk=self.proved_operation_id).state,'unknown')
                self.assertTrue(ops.ResourceClaim.objects.filter(operation_id=self.proved_operation_id,released_at=None).exists())
                self.assertEqual(ops.EffectReceipt.objects.filter(step__operation_id=self.proved_operation_id).count(),1)
            finally:
                server.terminate(); server.join(10)
                if server.is_alive():
                    server.kill(); server.join(5)
                listener.close()
                subprocess.run(['tmux','-S',str(root/'tmux.sock'),'kill-server'],check=False,capture_output=True)

    async def recovery_after_outage(self, origin, cert, layout):
        from mop.node.http_execution import HttpsTransport, MachineClient, Runtime, Unavailable, Refused
        from mop.node.execution_adapters import LocalExecutor, ProcessRunner
        client = MachineClient(HttpsTransport(origin,self.machine_key,tls=ssl.create_default_context(cafile=str(cert))))
        new_epoch = str(uuid4())
        runtime = Runtime(client,LocalExecutor(ProcessRunner(),layout,replacement=layout,readiness=layout),
            node_id=str(self.node.id),epoch=str(self.epoch),executor_epoch=new_epoch)
        try:
            deadline = time.monotonic()+10
            while True:
                try:
                    self.assertEqual(await client.executor_state(str(self.node.id)), dict(
                        schema_version=1, installation_id=self.installation_id, epoch=str(self.epoch),
                        node_id=str(self.node.id), executor_epoch=str(self.boot)))
                    break
                except Unavailable:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(.05)
            # Настоящий Runtime узнаёт previous epoch, выполняет server CAS,
            # но не выдаёт HTTP ACK за доказательство quiescence старого executor.
            with self.assertRaisesRegex(Unavailable,'quiescence'):
                await runtime.start(None)
            with self.assertRaises(Refused):
                await client.result(self.lost_admission,'succeeded','session-replaced')
            self.assertEqual(await client.poll(str(self.node.id),new_epoch,1),())
            self.assertEqual((await layout.probe())['pid'],layout.new_pid)
        finally:
            await runtime.aclose()

    async def physical_vertical(self, origin, cert, layout, *, lose_receipt=None):
        import aiohttp
        from mop.node.http_execution import HttpsTransport, MachineClient, EffectGuard
        from mop.node.execution_adapters import ProcessRunner, LocalExecutor
        tls = ssl.create_default_context(cafile=str(cert))
        client = MachineClient(HttpsTransport(origin,self.machine_key,tls=tls))
        layout.client = client
        executor = LocalExecutor(ProcessRunner(),layout,replacement=layout,readiness=layout)
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls),
                    headers={'Authorization':'Token '+self.key}) as http:
                # Readiness не угадывается по порту: проверяем HTTPS ответ/сертификат.
                deadline = time.monotonic()+10
                while True:
                    try:
                        async with http.get(origin+'/api/v1/health') as response:
                            self.assertEqual(response.status,200,await response.text())
                            break
                    except aiohttp.ClientConnectionError:
                        if time.monotonic() >= deadline:
                            raise
                        await asyncio.sleep(.05)
                deadline = time.monotonic()+5
                while True:
                    try:
                        before = await layout.probe(); break
                    except (OSError,ConnectionError):
                        if time.monotonic() >= deadline:
                            raise
                        await asyncio.sleep(.02)
                layout.old_pid = before['scope']
                self.assertTrue(before['identity'].startswith('Old Fixture <old@example.invalid> '))
                body = self.body()
                # HTTPS fixture обрывает первый accepted после настоящего COMMIT.
                # Клиент не получает operation ID и не повторяет POST.
                with self.assertRaises(aiohttp.ClientPayloadError):
                    async with http.post(origin+'/api/v1/puppets/'+str(self.target.id)+'/commands/restart',json=body) as response:
                        self.assertEqual(response.status,202)
                        await response.json()
                async with http.get(origin+'/api/v1/operations',params={'epoch':str(self.epoch),'request_id':body['request_id']}) as response:
                    self.assertEqual(response.status,200,await response.text())
                    operation_id = (await response.json())['operation']['id']
                for action,evidence in (('apply_identity','identity-applied'),('restart_session','session-replaced'),('confirm_ready','ready')):
                    work, = await client.poll(str(self.node.id),str(self.boot),1)
                    self.assertEqual(work.action,action)
                    admission = await client.claim(work,str(uuid4()))
                    async def authorize():
                        await client.recheck(admission)
                    def check():
                        from datetime import datetime,timezone
                        if datetime.now(timezone.utc) >= admission.lease_until:
                            raise RuntimeError('fixture admission expired')
                    outcome, proof = await executor.execute(work,EffectGuard(authorize,check))
                    self.assertEqual((outcome,proof),('succeeded',evidence))
                    if action == 'restart_session' and lose_receipt is not None:
                        await lose_receipt()
                        from mop.node.http_execution import Unavailable
                        with self.assertRaises(Unavailable):
                            await client.result(admission,outcome,proof)
                        self.lost_admission = admission
                        self.proved_operation_id = UUID(operation_id)
                        # Недоступность controller не убила новую живую app.
                        self.assertEqual((await layout.probe())['pid'],layout.new_pid)
                        return
                    await client.result(admission,outcome,proof)
                async with http.get(origin+'/api/v1/operations/'+operation_id) as response:
                    self.assertEqual(response.status,200,await response.text())
                    result = (await response.json())['operation']
                    self.assertEqual(result['state'],'succeeded')
                    self.assertFalse(result['resources_held'])
                self.assertNotEqual(layout.old_pid,layout.new_pid)
                # Независимый DB-read проверяется sync после выхода из async ниже.
                self.proved_operation_id = UUID(operation_id)
        finally:
            await executor.aclose()
            await client.aclose()
