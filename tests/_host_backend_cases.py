"""HYPOTHESIS #745: generic restarted retires incarnations even for HTTP reconnect.
SPEC: HOST discovery is read-only; empty-only CAS never retires any incarnation,
producer or attempt. Server UUID is registry state, not physical backend proof.
Actual PostgreSQL + HTTPS; no synthetic native acceptance claimed here.
SOLUTION: separate empty-only HOST CAS and read-only inventory, not restarted.
RESULT: four actual HTTP 404 assertion RED; 53 PG17/HTTPS epoch checks GREEN.
STATUS: FIXED — see #745
"""
import asyncio
import json
import ssl
from uuid import uuid4

from django.test import TransactionTestCase, override_settings
import _control_client_cases as discovery


@override_settings(ALLOWED_HOSTS=['localhost', 'testserver'])
class HostBackendEpochs(TransactionTestCase):
    serialized_rollback = True
    setUp = discovery.ExecutorDiscovery.setUp
    https = discovery.ExecutorDiscovery.https
    expected = discovery.ExecutorDiscovery.expected

    def request(self, origin, cert, *, bind=None, token=None):
        import http.client
        from urllib.parse import urlsplit
        address = urlsplit(origin)
        connection = http.client.HTTPSConnection(address.hostname, address.port,
            context=ssl.create_default_context(cafile=str(cert)), timeout=5)
        path = '/api/v1/machine/executor/host'
        try:
            if bind is None:
                connection.request('GET', path+'?schema_version=1&node_id='+str(self.node.pk),
                    headers={'Authorization': 'Token '+(token or self.machine_key)})
            else:
                connection.request('POST', path+'/bind', json.dumps(bind),
                    headers={'Authorization': 'Token '+(token or self.machine_key),
                             'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read()
            body = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw[:512].decode(errors='replace')
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def binding(self, **change):
        return dict(schema_version=1, node_id=str(self.node.pk), epoch=str(self.epoch),
                    previous_epoch=str(self.boot), new_epoch=str(uuid4()), **change)

    def test_host_discovery_is_authenticated_complete_readonly_and_registry_not_physical_proof(self):
        from mop.server.sessions.models import NodeExecutor
        before = list(NodeExecutor.objects.values())
        with self.https() as (server, origin, cert, _):
            status, headers, body = self.request(origin, cert)
            self.assertEqual(status, 200, body)
            self.assertEqual(body, dict(**self.expected(), empty=True))
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(self.request(origin, cert, token='0'*40)[0], 401)
            async def check():
                from mop.node.http_execution import HttpsTransport, MachineClient
                client = MachineClient(HttpsTransport(origin, self.machine_key,
                    tls=ssl.create_default_context(cafile=str(cert))))
                try:
                    self.assertEqual(await client.host_executor_state(str(self.node.pk)), body)
                finally:
                    await client.aclose()
            asyncio.run(check())
            self.assertEqual([method for method, _ in server.calls], ['GET']*3)
        self.assertEqual(list(NodeExecutor.objects.values()), before)

    def test_empty_first_host_binding_is_one_cas_not_generic_retirement(self):
        from mop.server.sessions.models import NodeExecutor, ExecutionIncarnation, AssignmentAttempt
        value = self.binding()
        with self.https() as (server, origin, cert, _):
            async def bind():
                from mop.node.http_execution import HttpsTransport, MachineClient
                client = MachineClient(HttpsTransport(origin, self.machine_key,
                    tls=ssl.create_default_context(cafile=str(cert))))
                try:
                    await client.host_bound(str(self.node.pk), str(self.epoch), str(self.boot), value['new_epoch'])
                finally:
                    await client.aclose()
            asyncio.run(bind())
            registered = list(NodeExecutor.objects.values())
            status, _, body = self.request(origin, cert)
            self.assertEqual(status, 200, body)
            self.assertEqual(body, dict(**self.expected(executor=value['new_epoch']), empty=True))
            # Потерянный report не повторяет effect; server CAS может подтвердить
            # тот же уже committed nonce без второго registered_at.
            self.assertEqual(self.request(origin, cert, bind=value)[0], 200)
            self.assertEqual(list(NodeExecutor.objects.values()), registered)
            self.assertEqual(self.request(origin, cert, bind=self.binding())[0], 409)
        self.assertEqual(ExecutionIncarnation.objects.count(), 0)
        self.assertEqual(AssignmentAttempt.objects.count(), 0)

    def test_changed_backend_never_retires_active_sql_incarnation_before_empty_cas(self):
        from mop.server.sessions.models import Assignment, ExecutionIncarnation, NodeExecutor
        from django.utils import timezone
        from datetime import timedelta
        row = Assignment.objects.create(node=self.node, project=self.project, role='worker',
            bundle_digest='a'*64, grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        inc = ExecutionIncarnation.objects.create(assignment=row, node=self.node, executor_epoch=self.boot)
        row.current_incarnation = inc
        row.save(update_fields=('current_incarnation',))
        before = (list(Assignment.objects.values()), list(ExecutionIncarnation.objects.values()),
                  list(NodeExecutor.objects.values()))
        with self.https() as (server, origin, cert, _):
            status, _, body = self.request(origin, cert)
            self.assertEqual(status, 200, body)
            self.assertEqual(body, dict(**self.expected(), empty=False))
            # Даже вызывающий в обход node bridge не получает generic restarted.
            status, _, body = self.request(origin, cert, bind=self.binding())
            self.assertEqual(status, 409, body)
            self.assertEqual(self.request(origin, cert)[2]['executor_epoch'], str(self.boot))
        self.assertEqual((list(Assignment.objects.values()), list(ExecutionIncarnation.objects.values()),
                          list(NodeExecutor.objects.values())), before)

    def test_host_bind_refuses_restore_epoch_stale_node_and_extraneous_authority(self):
        from mop.server.sessions.models import NodeExecutor
        from mop.server.controller.models import RestoreGuard
        before = list(NodeExecutor.objects.values())
        with self.https() as (_, origin, cert, _):
            for change, expected in (({'epoch': str(uuid4())}, 409),
                    ({'previous_epoch': str(uuid4())}, 409), ({'node_id': str(uuid4())}, 403),
                    ({'backend_proof': {}}, 400), ({'schema_version': True}, 400)):
                status, _, body = self.request(origin, cert, bind={**self.binding(), **change})
                self.assertEqual(status, expected, body)
            RestoreGuard.objects.update(state='restoring')
            self.assertEqual(self.request(origin, cert)[0], 503)
            self.assertEqual(self.request(origin, cert, bind=self.binding())[0], 503)
        self.assertEqual(list(NodeExecutor.objects.values()), before)
