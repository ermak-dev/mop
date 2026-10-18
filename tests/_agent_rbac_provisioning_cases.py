"""Явное первое применение обычных Django Groups к агенту (#747).

HYPOTHESIS: fresh create проходит семь reference-фаз и активирует Grant, но
canonical AgentToken видит actions=[]: registration правильно не выдаёт прав,
а отдельного авторизованного provisioning выбранного baseline нет.
SPEC: approved immutable recipe выбирается до I/O и применяется один раз в
final compose после provisioned reserved Grant; receipt — прошлое, не desired
state. Обычные Groups и live issuer/ancestry остаются authority.
STATUS: RED — see #747.
"""
from uuid import uuid4

from django.test import Client, TransactionTestCase, override_settings

from _create_cases import CreateVertical, CreateActivation, native_ack
from _initial_create_cases import InitialCreatePhases


@override_settings(ALLOWED_HOSTS=['testserver'])
class AgentRBACProvisioning(TransactionTestCase):
    serialized_rollback = True
    setUp = CreateVertical.setUp
    ready, body, submit = CreateVertical.ready, CreateVertical.body, CreateVertical.submit
    machine, planned, claim_next = InitialCreatePhases.machine, InitialCreatePhases.planned, InitialCreatePhases.claim_next
    protected_submit, credential_app = InitialCreatePhases.protected_submit, InitialCreatePhases.credential_app

    def configured(self):
        from _register_peer import RegisterPeer
        from mop.server.operations.scheduler import NomadStopScheduler
        peer = RegisterPeer()
        self.addCleanup(peer.close)
        self.ready(scheduler=NomadStopScheduler(origin=peer.origin, scheduler_id=uuid4(), token='fixture-only', timeout=2))

    def create(self):
        from mop.server.operations.models import Operation
        response = self.protected_submit(self.body(), lambda: None)
        self.assertEqual(response.status_code, 202, response.content)
        return Operation.objects.get(pk=response.json()['operation']['id'])

    def activate(self, row):
        execution, actor = self.machine()
        self.planned(row, execution, actor)
        self.assertIsNotNone(self.ops.launches.run(row.id))
        for action, evidence in (('native_exec', 'native-started'), ('confirm_ready', 'ready')):
            work, attempt = self.claim_next(execution, actor, row, action)
            if action == 'native_exec':
                native_ack(self, work, self.machine_key)
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence=evidence)
        self.ops.activations.activator = CreateActivation.activator(self)
        self.assertIsNotNone(self.ops.activations.run(row.id))
        row.refresh_from_db()
        self.assertEqual(row.state, 'succeeded', row.reason)
        from mop.server.access.models import Grant
        grant = Grant.objects.get(pk=row.restartbinding.grant_id)
        self.assertEqual(grant.state, 'active')
        self.assertEqual(tuple(row.step_set.order_by('ordinal').values_list('outcome', flat=True)), ('succeeded',)*7)
        return grant

    def capabilities(self, subject_id):
        from rest_framework.authtoken.models import Token
        key = Token.objects.get(user_id=subject_id).key
        response = Client(HTTP_AUTHORIZATION='Token '+key).get('/api/v1/capabilities',
            {'project_id': str(self.project.pk)}, secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['actions']

    def test_fresh_create_active_canonical_token_has_explicit_small_group_baseline(self):
        self.configured()
        row = self.create()
        grant = self.activate(row)
        self.assertIn('catalog.view_pool', self.capabilities(grant.subject_id),
            'fresh active agent has no subject RBAC without explicit provisioning')
