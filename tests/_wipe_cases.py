"""Сброс клона папета через общий журнал и машинный протокол исполнителя (#625).

HYPOTHESIS: право execution.wipe_puppet есть в едином словаре, но маршрута, typed
command, handler, шага wipe_clone и подтверждения clone-wiped нет.
SPEC: wipe уничтожает несохранённую работу, поэтому: только человек-владелец
назначения и только отдельным правом; физический порт тела обязателен до
admission; живая сессия — отказ узла до эффекта (failed-before-effect), а не
успех; неизвестный исход держит цель и не повторяется.
RED: маршрута нет (404), композиция не знает порт тела wipe_support.
SOLUTION: WipeHandler на общем машинном протоколе restart, шаг wipe_clone,
подтверждение clone-wiped, порт тела обязателен до приёма.
RESULT: PG17 — 9/9; вместе с restart/send/operations/execution 130 OK.
STATUS: FIXED — see #625
"""
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings

from _restart_vertical_cases import RestartVertical
from mop.server.access import models as a, services as auth
from mop.server.controller import operationviews
from mop.server.operations import models as m


@override_settings(ALLOWED_HOSTS=['testserver'])
class WipeVertical(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    fixture = RestartVertical.fixture
    body = RestartVertical.body

    def wipe_body(self, **changes):
        return self.body(**{'params': {}, **changes})

    def post(self, body=None, client=None):
        return (client or self.client).post('/api/v1/puppets/'+str(self.target.id)+'/commands/wipe',
            body or self.wipe_body(), content_type='application/json', secure=True)

    def prepare(self, *, support=True):
        from unittest.mock import patch
        from mop.server.controller.machinebinding import compose, operations
        self.fixture()
        port = self.support if support else None
        self.ops = operations(grants=self.grants, wipe_support=port)
        self.service = compose(grants=self.grants, wipe_support=port)
        patch.object(operationviews, 'wipe_api', lambda: self.ops).start()
        patch.object(operationviews, 'operations_api', lambda: self.ops).start()

    def next(self):
        work, = self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot, limit=1)
        attempt = self.service.claim(self.machine_actor, work, request_id=uuid4())
        return work, attempt

    def finish(self, attempt, outcome, evidence):
        return self.service.result(self.machine_actor, attempt.id, expected_revision=attempt.revision,
            receipt_id=uuid4(), outcome=outcome, evidence=evidence)

    def test_public_wipe_admission_is_routed(self):
        self.fixture()
        response = self.post()
        self.assertNotEqual(response.status_code, 404, 'typed wipe admission must be routed')

    def test_wipe_is_one_wipe_clone_step_of_the_shared_machine_protocol(self):
        self.prepare()
        body = self.wipe_body()
        response = self.post(body)
        self.assertEqual(response.status_code, 202, response.content)
        operation = response.json()['operation']
        self.assertEqual(m.Operation.objects.get().action, 'wipe')
        self.assertEqual(list(m.Step.objects.values_list('name', flat=True)), ['wipe_clone'])
        owner, generation = self.target.owner_id, self.target.generation
        work, attempt = self.next()
        self.assertEqual(work.action, 'wipe_clone')
        self.assertEqual(str(work.operation_id), operation['id'])
        self.finish(attempt, 'succeeded', 'clone-wiped')
        status = self.ops.status(self.actor, work.operation_id)
        self.assertEqual(status['state'], 'succeeded')
        self.assertFalse(status['resources_held'])
        self.assertEqual(status['evidence']['action'], 'wipe_clone')
        self.assertEqual(m.EffectReceipt.objects.count(), 1)
        self.target.refresh_from_db()
        # Сброс клона не переназначает работу и не меняет поколение назначения.
        self.assertEqual((self.target.owner_id, self.target.generation), (owner, generation))
        retry = self.post(body)
        self.assertEqual(retry.json()['operation']['id'], operation['id'])

    def test_a_restart_confirmation_does_not_confirm_a_wipe(self):
        self.prepare()
        self.assertEqual(self.post().status_code, 202)
        _, attempt = self.next()
        with self.assertRaisesRegex(ValueError, 'named action confirmation'):
            self.finish(attempt, 'succeeded', 'session-replaced')

    def test_live_session_refusal_fails_the_operation_without_a_receipt_of_success(self):
        self.prepare()
        self.assertEqual(self.post().status_code, 202)
        work, attempt = self.next()
        self.finish(attempt, 'failed', 'failed-before-effect')
        status = self.ops.status(self.actor, work.operation_id)
        self.assertEqual(status['state'], 'failed')
        self.assertFalse(status['resources_held'])
        self.assertFalse(m.EffectReceipt.objects.get().applied)

    def test_unknown_outcome_holds_the_target_and_is_not_offered_again(self):
        self.prepare()
        self.assertEqual(self.post().status_code, 202)
        work, attempt = self.next()
        self.finish(attempt, 'unknown', 'connection-lost')
        self.assertEqual(self.ops.status(self.actor, work.operation_id)['state'], 'unknown')
        self.assertEqual(self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot, limit=1), ())
        # Забор общий с restart: приём новой команды не блокируется, но её захват
        # исполнителем — да, пока исход прежнего эффекта неизвестен.
        self.assertEqual(self.post(self.wipe_body()).status_code, 202)
        second, = self.service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot, limit=1)
        self.assertNotEqual(second.operation_id, work.operation_id)
        from mop.server.sessions.services import Conflict
        with self.assertRaisesRegex(Conflict, 'unresolved external effect'):
            self.service.claim(self.machine_actor, second, request_id=uuid4())

    def test_another_human_cannot_wipe_the_owners_assignment(self):
        self.prepare()
        other = get_user_model().objects.create_superuser('other', email='other@example.invalid', password='fixture-only')
        other.first_name, other.last_name = 'Other', 'Human'
        other.save()
        source = a.IdentitySource.objects.get(name='local')
        a.LoginIdentity.objects.create(user=other, source=source, external_subject='other')
        client = Client(HTTP_AUTHORIZATION='Token '+auth.issue_token(other.pk))
        response = self.post(client=client)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(m.Operation.objects.count(), 0)

    def test_restart_permission_alone_does_not_grant_wipe(self):
        self.prepare()
        from django.contrib.auth.models import Permission
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        a.ProjectMembership.objects.get_or_create(user=self.user, project=self.project,
            source=a.IdentitySource.objects.get(name='local'))
        for code in ('restart_puppet', 'view_operation'):
            self.user.user_permissions.add(*Permission.objects.filter(codename=code))
        response = self.post()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(m.Operation.objects.count(), 0)

    def test_missing_body_wipe_port_refuses_before_admission(self):
        self.prepare(support=False)
        response = self.post()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(m.Operation.objects.count(), 0)

    def test_wipe_carries_no_force_or_free_form_params(self):
        self.prepare()
        for params in ({'force': True}, {'fresh': False}, {'path': '/'}):
            response = self.post(self.wipe_body(params=params))
            self.assertEqual(response.status_code, 400, (params, response.content))
        self.assertEqual(m.Operation.objects.count(), 0)
