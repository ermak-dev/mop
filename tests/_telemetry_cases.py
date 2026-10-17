"""Телеметрия #641: commit, scope и независимые PostgreSQL connections.

HYPOTHESIS: собственные turn/model файлы не дают центральной устойчивости/fencing.
RED: PG16 — отсутствует SessionObservation; затем PG16/17 подтвердили
переатрибуцию одного хода, потерю source при retention и fake current после gap.
SOLUTION: короткий atomic ingest, pinned immutable source и model causal fence.
RESULT: PG16 — 13/13; PG17 combined — 176/176, независимые connections включены.
STATUS: FIXED — see #641; production Root bindings/rollout не проверены.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import Permission
from django.db import close_old_connections, transaction
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone
import _execution_cases as fixture


@override_settings(ALLOWED_HOSTS=['testserver'])
class Telemetry(TransactionTestCase):
    # Data migrations нужны каждому test; post_migrate не восстанавливает их.
    serialized_rollback = True

    def setUp(self):
        self.assertTrue(any(model.__name__ == 'SessionObservation' for model in apps.get_models()),
                        'session telemetry must be installed in execution, not Django sessions')
        fixture.MachineExecution.setUp(self)
        from mop.server.sessions.telemetry import Telemetry as Service
        from mop.common.telemetry import SessionScope, CurrentSession, SourceRef, SourceAttributedTurn
        self.key = self.auth.enroll_service('telemetry', self.node.pk, project_ids=[self.project.pk],
            permissions=['execution.ingest_telemetry'])
        self.actor = self.auth.token_context(self.key)
        self.source = CurrentSession('session-one', uuid4())
        self.scope = SessionScope(self.epoch, self.target.pk, self.inc.id, self.node.pk,
            self.boot, self.source.session_id, self.source.producer_epoch)
        self.initiator = SourceRef(user_id=self.human.pk,
            endpoint_id=uuid4(), session_id=uuid4(), operation_id=uuid4(), message_id=uuid4())
        self.turn = uuid4()
        test = self
        class Current:
            def current(inner, binding):
                return test.source
        class Attribution:
            def resolve(inner, binding, session_id, turn_id):
                return SourceAttributedTurn(turn_id, test.initiator) if turn_id == test.turn else None
        self.service = Service(sessions=Current(), attribution=Attribution(),
            installation=lambda: self.p.AuthorityV1(self.epoch, True))

    def event(self, seq=1, event='Stop', **values):
        from mop.common.telemetry import Observation
        return Observation(self.scope, seq, event, self.turn, values.get('model'),
            values.get('error'), values.get('causal_switch'), values.get('dropped', 0))

    def test_duplicate_and_out_of_order_do_not_regress_projection(self):
        row = self.event(event='StopFailure', error='model_not_found')
        self.assertEqual(self.service.ingest(self.actor, row).sequence, 1)
        self.assertEqual(self.service.ingest(self.actor, row).sequence, 1)
        self.assertEqual(self.m.TurnOutcome.objects.count(), 1)
        with self.assertRaises(self.s.Conflict):
            self.service.ingest(self.actor, self.event(event='Stop'))
        self.service.ingest(self.actor, self.event(3))
        with self.assertRaises(self.s.Conflict):
            self.service.ingest(self.actor, self.event(2))
        observation = self.m.SessionObservation.objects.get()
        self.assertEqual(observation.sequence, 3)
        self.assertTrue(observation.incomplete)
        self.assertEqual(observation.gap_count, 1)

    def test_ack_requires_commit_and_rollback_leaves_no_receipt(self):
        with transaction.atomic():
            with self.assertRaises(self.s.Unavailable):
                self.service.ingest(self.actor, self.event())
        with patch.object(self.m.TurnOutcome.objects, 'create', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.service.ingest(self.actor, self.event())
        self.assertEqual(self.m.SessionObservation.objects.count(), 0)
        self.assertEqual(self.m.TelemetryReceipt.objects.count(), 0)
        self.service.ingest(self.actor, self.event())
        close_old_connections()
        self.assertEqual(self.m.TelemetryReceipt.objects.count(), 1)

    def test_simultaneous_duplicate_is_committed_once(self):
        barrier = Barrier(2)
        def run(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.service.ingest(self.actor, self.event()).sequence
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(list(workers.map(run, range(2))), [1, 1])
        self.assertEqual(self.m.TurnOutcome.objects.count(), 1)

    def test_authoritative_session_not_request_uuid_controls_producer_reset(self):
        self.service.ingest(self.actor, self.event(7))
        for scope in (replace(self.scope, producer_epoch=uuid4()),
                      replace(self.scope, session_id='arbitrary-session'),
                      replace(self.scope, executor_epoch=uuid4())):
            with self.assertRaises(self.s.Conflict):
                self.service.ingest(self.actor, replace(self.event(), scope=scope))
        self.assertEqual(self.m.SessionObservation.objects.get().sequence, 7)
        from mop.common.telemetry import CurrentSession
        self.source = CurrentSession('session-two', uuid4())
        with self.assertRaises(self.s.Conflict):
            self.service.ingest(self.actor, self.event(8))
        self.scope = replace(self.scope, session_id=self.source.session_id,
                             producer_epoch=self.source.producer_epoch)
        self.service.ingest(self.actor, self.event())
        self.assertEqual(self.m.SessionObservation.objects.count(), 2)

    def test_current_assignment_and_live_access_are_rechecked_in_sql(self):
        self.service.ingest(self.actor, self.event())
        from mop.server.access.services import Denied
        self.auth.revoke_token(self.actor)
        with self.assertRaises(Denied):
            self.service.ingest(self.actor, self.event(2))

    def test_wrong_enrolled_node_and_retired_incarnation_cannot_ingest(self):
        with self.assertRaises((self.s.Conflict, self.auth.Denied)):
            self.service.ingest(self.actor, replace(self.event(), scope=replace(self.scope, node_id=uuid4())))
        self.m.ExecutionIncarnation.objects.filter(pk=self.inc.id).update(accepting=False)
        with self.assertRaises(self.s.Conflict):
            self.service.ingest(self.actor, self.event())

    def test_late_status_cannot_replace_confirmed_switch_or_freeze_fake_current_model(self):
        self.service.ingest(self.actor, self.event(event='PostModelSwitch', model='model-b'))
        self.service.ingest(self.actor, self.event(2, 'Statusline', model='model-a'))
        row = self.m.SessionObservation.objects.get()
        self.assertEqual(row.confirmed_model, 'model-b')
        self.assertEqual(row.model_hint, 'model-a')
        self.assertIsNone(self.service.latest(self.actor, self.scope).current_model)
        self.service.ingest(self.actor, self.event(3, 'PostModelSwitch', model='model-c'))
        self.service.ingest(self.actor, self.event(4, 'Statusline', model='model-b'))
        self.assertEqual(self.m.SessionObservation.objects.get().confirmed_model, 'model-c')
        self.assertIsNone(self.service.latest(self.actor, self.scope).current_model)
        # Только доверенный causal fence, не timestamp/arrival, подтверждает свежесть.
        self.service.ingest(self.actor, self.event(5, 'Statusline', model='model-c', causal_switch=3))
        self.assertEqual(self.service.latest(self.actor, self.scope).current_model, 'model-c')

    def test_source_is_immutable_actual_initiator_not_current_owner(self):
        self.service.ingest(self.actor, self.event())
        row = self.m.TurnOutcome.objects.get()
        self.assertEqual(row.initiator_user_id, self.initiator.user_id)
        self.assertEqual(row.initiator_endpoint_id, self.initiator.endpoint_id)
        self.assertEqual(row.operation_id, self.initiator.operation_id)
        self.assertEqual(row.message_id, self.initiator.message_id)
        self.service.ingest(self.actor, replace(self.event(2), turn_id=uuid4()))
        self.assertIsNone(self.m.TurnOutcome.objects.order_by('-sequence').first().initiator_user_id)

    def test_active_turn_source_survives_bounded_model_record_retention(self):
        self.service.retained_records = 1
        self.service.ingest(self.actor, self.event(event='UserPromptSubmit'))
        first = self.initiator
        self.service.ingest(self.actor, self.event(2, 'Statusline', model='a'))
        self.service.ingest(self.actor, self.event(3, 'Statusline', model='a'))
        self.initiator = replace(first, operation_id=uuid4())
        self.service.ingest(self.actor, self.event(4))
        row = self.m.TurnOutcome.objects.order_by('-sequence').first()
        self.assertEqual(row.initiator_user_id, first.user_id)
        self.assertLessEqual(self.m.TurnOutcome.objects.count(), 2)
        self.assertLessEqual(self.m.ModelObservation.objects.count(), 1)

    def test_missing_events_invalidate_current_model_but_preserve_last_confirmation(self):
        self.service.ingest(self.actor, self.event(event='PostModelSwitch', model='model-a'))
        self.service.ingest(self.actor, self.event(3))
        latest = self.service.latest(self.actor, self.scope)
        self.assertTrue(latest.incomplete)
        self.assertEqual(latest.last_confirmed_model, 'model-a')
        self.assertIsNone(latest.current_model, 'the missing event could be a model switch')

    def test_one_turn_cannot_be_reattributed_after_a_later_owner_or_resolver_change(self):
        self.service.ingest(self.actor, self.event(event='UserPromptSubmit'))
        first = self.initiator
        self.initiator = replace(first, operation_id=uuid4())
        self.service.ingest(self.actor, self.event(2))
        row = self.m.TurnOutcome.objects.order_by('-sequence').first()
        self.assertEqual(row.initiator_user_id, first.user_id)
        self.assertEqual(row.operation_id, first.operation_id)

    def test_http_authenticated_body_is_strict_and_private(self):
        client = Client()
        url = '/api/v1/machine/telemetry'
        self.assertIn(client.post(url, {}, content_type='application/json', secure=True).status_code, (401, 403))
        from mop.server.controller import telemetryviews
        with patch.object(telemetryviews, 'telemetry', self.service):
            response = client.post(url, self.event().wire(), content_type='application/json', secure=True,
                                   HTTP_AUTHORIZATION='Token '+self.key)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()['sequence'], '1')
            self.assertEqual(response['Cache-Control'], 'no-store')
            bad = dict(self.event(2).wire(), owner_id=str(self.human.pk))
            self.assertEqual(client.post(url, bad, content_type='application/json', secure=True,
                HTTP_AUTHORIZATION='Token '+self.key).status_code, 400)

    def test_private_history_cursor_is_scoped_resettable_and_not_an_event_log(self):
        self.human.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='read_history'))
        test = self
        class Reader:
            def read(inner, scope, cursor):
                test.assertFalse(transaction.get_connection().in_atomic_block)
                return dict(epoch='external-file-epoch', session_id=scope.session_id, cursor='private-cursor',
                    records=[{'text': 'private chat'}], reset=cursor is None, has_more=False, partial=False)
        self.service.history = Reader()
        page = self.service.read_history(self.owner, self.scope, expected_revision=0)
        self.assertEqual(page['records'], [{'text': 'private chat'}])
        self.assertEqual(self.m.PrivateHistoryCursor.objects.get().cursor, 'private-cursor')
        self.assertEqual(self.m.TelemetryReceipt.objects.count(), 0)
        with self.assertRaises(self.s.Conflict):
            self.service.read_history(self.owner, self.scope, expected_revision=0)
        self.service.reset_history(self.owner, self.scope, expected_revision=1)
        self.assertEqual(self.m.PrivateHistoryCursor.objects.get().cursor, '')
