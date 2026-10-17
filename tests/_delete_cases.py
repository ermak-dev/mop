"""Удаление папета через общий журнал: условная остановка, purge, закрытие (#625).

HYPOTHESIS: права execution.delete_puppet есть, но маршрута, typed command,
шага scheduler_delete, purge в порте планировщика и закрытия назначения нет.
SPEC: удаление -- условная остановка job нашим токеном и индексом с
физическим подтверждением, затем purge только того job, что остановили мы, и
подтверждение 404. Назначение закрывается навсегда: ни текущим, ни активируемым
оно больше не становится. Клон host-тела остаётся. Отдельное право и запрет
чужого человеческого назначения; потерянный ответ purge -- unknown и сверка
только чтением; сдвинутый индекс -- отказ без записи.
"""
from uuid import UUID

from django.test import TransactionTestCase, override_settings

from _restart_vertical_cases import RestartVertical
from _stop_cases import StopVertical


@override_settings(ALLOWED_HOSTS=['testserver'])
class DeleteVertical(TransactionTestCase):
    serialized_rollback = True
    setUp = RestartVertical.setUp
    fixture = RestartVertical.fixture
    body = RestartVertical.body

    def prepare(self):
        from unittest.mock import patch
        body = StopVertical.prepare(self)
        patch('mop.server.controller.operationviews.delete_api', lambda: self.ops).start()
        return body

    def submit(self, body, client=None):
        return (client or self.client).post('/api/v1/puppets/'+str(self.target.id)+'/commands/delete', body,
            content_type='application/json', secure=True)

    def test_public_delete_admission_is_routed(self):
        self.fixture()
        response = self.submit(self.body(params={}))
        self.assertNotEqual(response.status_code, 404, 'typed delete admission must be routed')

    def test_delete_stops_purges_and_closes_the_assignment_for_good(self):
        body = self.prepare()
        (self.scheduler.clone/'untracked-work').write_text('host clone stays')
        response = self.submit(body)
        self.assertEqual(response.status_code, 202, response.content)
        operation = UUID(response.json()['operation']['id'])
        self.ops.deletes.run(operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertFalse(result['resources_held'])
        state = self.scheduler.request('GET')
        self.assertEqual((state['writes'], state['purges'], state['exists'], state['alive']), (1, 1, False, False))
        self.assertEqual((self.scheduler.clone/'untracked-work').read_text(), 'host clone stays')
        from mop.server.sessions.models import Assignment
        from mop.server.sessions.services import Assignments, Conflict
        row = Assignment.objects.get(pk=self.target.id)
        self.assertIsNotNone(row.deleted_at)
        self.assertIsNone(row.owner_id)
        self.assertIsNone(row.current_incarnation_id)
        self.assertIsNone(Assignments().current(project_id=row.project_id, target_id=row.id))
        # Повтор того же запроса -- тот же результат, без второго purge.
        self.assertEqual(self.submit(body).json()['operation']['id'], str(operation))
        self.ops.deletes.run(operation)
        self.assertEqual(self.scheduler.request('GET')['purges'], 1)

    def test_deleted_assignment_is_never_activated_again(self):
        body = self.prepare()
        operation = UUID(self.submit(body).json()['operation']['id'])
        self.ops.deletes.run(operation)
        from uuid import uuid4
        from mop.server.controller.runtime import assignments_service as Assignments
        from mop.server.sessions.models import Assignment
        from mop.server.sessions.services import Conflict
        from django.db import IntegrityError, transaction
        from mop.server.access.services import Denied
        row = Assignment.objects.get(pk=self.target.id)
        # Владельца нет: активация отказывает ещё на проверке владения.
        with self.assertRaises((Denied, Conflict)):
            Assignments().activate(self.actor, row.id, expected_revision=row.revision, executor_epoch=uuid4())
        # И вернуть владельца удалённому назначению нельзя даже мимо сервисов:
        # закрытость держит ограничение БД.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.filter(pk=row.id).update(owner_id=self.user.pk)

    def test_stop_permission_alone_does_not_grant_delete(self):
        body = self.prepare()
        from django.contrib.auth.models import Permission
        from mop.server.access import models as a
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        a.ProjectMembership.objects.get_or_create(user=self.user, project=self.project,
            source=a.IdentitySource.objects.get(name='local'))
        for code in ('stop_puppet_job', 'view_operation'):
            self.user.user_permissions.add(*Permission.objects.filter(codename=code))
        response = self.submit(body)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.scheduler.request('GET')['purges'], 0)

    def test_lost_purge_reply_is_unknown_and_reconciles_read_only(self):
        body = self.prepare()
        self.scheduler.mode('lose-delete')
        operation = UUID(self.submit(body).json()['operation']['id'])
        self.ops.deletes.run(operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'unknown', result)
        self.assertTrue(result['resources_held'])
        self.ops.deletes.run(operation)
        self.assertEqual(self.scheduler.request('GET')['purges'], 1)
        self.ops.deletes.reconcile(self.actor, operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(self.scheduler.request('GET')['purges'], 1)

    def test_replaced_job_index_is_never_stopped_or_purged(self):
        body = self.prepare()
        self.scheduler.mode('replace')
        operation = UUID(self.submit(body).json()['operation']['id'])
        self.ops.deletes.run(operation)
        result = self.ops.status(self.actor, operation)
        self.assertEqual(result['state'], 'failed', result)
        state = self.scheduler.request('GET')
        self.assertEqual((state['writes'], state['purges'], state['exists']), (0, 0, True))
        from mop.server.sessions.models import Assignment
        self.assertIsNone(Assignment.objects.get(pk=self.target.id).deleted_at)
