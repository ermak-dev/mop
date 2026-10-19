"""Client acceptance #748: реальный create/producer/model/activation/socket/Token.

Явная Django Group — fixture только CLIENT RBAC, не fresh provisioning #747.
Scheduler/node adapters owned, не physical HOST acceptance #745/#746.
HYPOTHESIS: пустой native MCP не мешает readiness; только post-active context
и настоящий TLS catalogue публикуют tools. Отзыв группы проверяется live.
SOLUTION: один SDK stdio процесс проходит реальные producer/model/readiness,
activation и protected context issuance; catalogue/tool calls идут по verified TLS.
RESULT: PostgreSQL 17/DRF acceptance проходит; до activation tools пусты,
после activation появляются, после Group/Token revoke исчезают без смены PID.
STATUS: FIXED — see #748
"""
import asyncio
import os
from uuid import uuid4

from django.contrib.auth.models import Group, Permission
from django.test import Client, TransactionTestCase, override_settings
from rest_framework.authtoken.models import Token

import _credential_cases as sockets
import _create_cases as create
import _management_context_cases as contexts
from _control_fixture import own_tls
from _control_client_cases import DjangoTLS
from _body_mcp_fixture import Stdio, publish, expiry


@override_settings(ALLOWED_HOSTS=['localhost', 'testserver'])
class ActivatedBody(TransactionTestCase):
    serialized_rollback = True
    setUp = sockets.CreateSocketActivationCases.setUp
    ready = sockets.CreateSocketActivationCases.ready
    body = sockets.CreateSocketActivationCases.body
    submit = sockets.CreateSocketActivationCases.submit
    exercise = sockets.CredentialSocketCases.exercise
    launch = create.CreateRegistration.launch
    machine = create.CreateActivation.machine
    post = contexts.ManagementContext.post
    run_delivery = contexts.ManagementContext.run_delivery

    def test_same_stdio_pid_binds_only_after_real_activation_and_loses_group_permissions(self):
        from mop.common.credentials import Credential
        from mop.common.credential_client import GrantActivator
        from mop.common.telemetry import SessionScope, Observation
        from mop.server.access import services as auth
        from mop.server.access.models import Grant
        from mop.server.sessions.models import Assignment
        from mop.server.sessions.producer_models import SessionProducer
        from mop.server.controller.models import Installation, RestoreGuard
        operation, target, _ = self.launch()
        create.CreateActivation.through_readiness(self, operation, ready=False)
        self.assignment = Assignment.objects.get(pk=target)
        producer = SessionProducer.objects.get(currentproducer__incarnation_id=self.assignment.current_incarnation_id)
        self.scope = SessionScope(producer.epoch, producer.assignment_id, producer.incarnation_id, producer.node_id,
            producer.executor_epoch, producer.session_id, producer.producer_epoch).wire()
        self.request = dict(schema_version=1, scope=self.scope, generation=str(self.assignment.generation))
        machine_user = Token.objects.get(key=self._executor_key).user
        for name in ('access.read_management_context', 'execution.ingest_telemetry'):
            app, code = name.split('.')
            machine_user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        self.machine_auth = Credential('token', self._executor_key)
        grant = Grant.objects.select_related('subject').get(pk=self.assignment.grant_id)
        canonical = Token.objects.get(user_id=grant.subject_id).key
        group = Group.objects.create(name='body-client-read-fixture')
        group.permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_project'))
        grant.subject.groups.add(group)
        activator = auth.enroll_service('body-activation-worker', self.node.pk,
            project_ids=(self.project.pk,), permissions=('access.activate_grant',), use_case='grant-activator')
        installation = Installation.objects.get(pk=1)
        restore = RestoreGuard.objects.get(pk=1)
        with own_tls(DjangoTLS) as (https, origin, cert, directory):
            https.calls = []; https.drop_post = https.drop_get = False
            https.after = lambda method, response: None
            native = Stdio(self, directory, token=canonical)
            native.initialize()
            pid = native.process.pid
            self.assertEqual(native.rpc(2, 'tools/list')['tools'], [])
            # Producer ACK уже принят; модель/readiness/activation ещё не готовы.
            publish(native.boot, dict(schema_version=1, request=self.request,
                installation_id=installation.identity, restore_generation=str(restore.generation),
                project_id=str(self.project.pk), grant_id=str(grant.id), expires_at=expiry(),
                origin=origin, ca_pem=cert.read_text(), body_uid=os.geteuid()))
            self.assertEqual(native.rpc(3, 'tools/list')['tools'], [])
            self.assertEqual(https.calls, [])
            observed = Observation(SessionScope.parse(self.scope), 1, 'PostModelSwitch', None,
                'fixture-native-model', None, None, 0)
            response = Client(HTTP_AUTHORIZATION='Token '+self._executor_key).post('/api/v1/machine/telemetry',
                observed.wire(), content_type='application/json', secure=True)
            self.assertEqual(response.status_code, 200, response.content)
            service, actor = self.machine()
            work, = service.poll(actor, self.node.pk, executor_epoch=self.boot)
            self.assertEqual(work.action, 'confirm_ready')
            attempt = service.claim(actor, work, request_id=uuid4())
            service.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence='ready')

            async def accepted(client, server):
                refused = await asyncio.to_thread(self.post)
                self.assertEqual(refused.status_code, 403, refused.content)
                self.ops.activations.activator = GrantActivator(client, Credential('token', activator))
                result = await server.runtime.database(self.ops.activations.run, operation)
                self.assertEqual(result['state'], 'succeeded', result['reason'])
                response = await asyncio.to_thread(self.post)
                self.assertEqual(response.status_code, 200, response.content)
                value = response.json()
                self.assertEqual(value['token'], canonical)
                self.assertNotEqual(value['token'], self._executor_key)
                self.assertEqual(value['installation_id'], installation.identity)
                self.assertEqual(value['restore_generation'], str(restore.generation))
                from mop.client import control as api
                query = api.Client(api.ServerContext(origin, canonical, str(cert)))
                current = await asyncio.to_thread(query.capabilities,
                    api.CapabilityScope(project_id=str(self.project.pk)))
                self.assertIsNone(current.identity.node_id,
                    'canonical node metadata is not the physical runtime node')
                publish(native.secret, value)
                await asyncio.to_thread(native.changed)
                tools = (await asyncio.to_thread(native.rpc, 4, 'tools/list'))['tools']
                self.assertIn('project_list', {tool['name'] for tool in tools})
                reply = await asyncio.to_thread(native.rpc, 5, 'tools/call', dict(name='project_list'))
                self.assertFalse(reply.get('isError', False), reply)
                await server.runtime.database(grant.subject.groups.remove, group.pk)
                tools = (await asyncio.to_thread(native.rpc, 6, 'tools/list'))['tools']
                self.assertNotIn('project_list', {tool['name'] for tool in tools})
                reply = await asyncio.to_thread(native.rpc, 7, 'tools/call', dict(name='project_list'))
                self.assertTrue(reply['isError'])
                await server.runtime.database(Token.objects.filter(user_id=grant.subject_id).delete)
                self.assertEqual((await asyncio.to_thread(native.rpc, 8, 'tools/list'))['tools'], [])
            self.run_delivery(accepted, provider=self.grants.secrets.provider)
            self.assertEqual(native.process.pid, pid)
            native.close()
