"""Начальная ready-evidence boundary на PostgreSQL и verified HTTPS (#749).

HYPOTHESIS: отсутствует read-only query, любой nonempty model проходит node,
а successful ready result доверяет producer ACK без committed model evidence.
SPEC: exact claimed admission + physical scope, original successful native ACK,
реальный TelemetryReceipt/PostModelSwitch desired model, повторная проверка commit.
SOLUTION: read-only owner query, exact snapshot в successful result и concrete
node Source с selected-model equality/повторным physical observe.
RESULT: первоначальные 4/4 и расширенные 11/11 behavioral RED; 14/14 GREEN
на PG17/private credential socket/verified HTTPS, без retry или новых ACK.
STATUS: FIXED — see #749
Наблюдатель native контролируемый; vendor/physical acceptance здесь не заявляется.
"""
import asyncio
from dataclasses import replace
from uuid import uuid4

from django.db import connections
from django.test import TransactionTestCase, override_settings

from _current_view_purpose_cases import CurrentViewPurpose
from mop.node.body_launch import ReadyConfirmation, ReadyReceipt
from mop.node.http_execution import Refused, Unavailable
from mop.node.producer_views import CurrentLaunchAuthority


@override_settings(ALLOWED_HOSTS=['127.0.0.1', 'testserver'])
class ReadyEvidence(TransactionTestCase):
    serialized_rollback = True
    setUp = CurrentViewPurpose.setUp
    reserved_grant = CurrentViewPurpose.reserved_grant
    grant_assignment = CurrentViewPurpose.grant_assignment
    exercise = CurrentViewPurpose.exercise
    run_delivery = CurrentViewPurpose.run_delivery
    fixture = CurrentViewPurpose.fixture
    url = CurrentViewPurpose.url
    check_current = CurrentViewPurpose.check_current

    async def sql(self, function):
        def run():
            try:
                return function()
            finally:
                connections.close_all()
        return await asyncio.to_thread(run)

    async def telemetry(self, machine, runtime, model=None, *, event='PostModelSwitch', sequence=1, causal_switch=None):
        from django.contrib.auth.models import Permission
        from django.contrib.auth import get_user_model
        from mop.common.telemetry import Observation
        from mop.node.telemetry import TelemetryClient
        # Permission реального enrolled executor, не force_authenticate.
        def authorize():
            get_user_model().objects.get(pk=self.machine.user_id).user_permissions.add(
                Permission.objects.get(content_type__app_label='execution', codename='ingest_telemetry'))
        await self.sql(authorize)
        observation = Observation(runtime._proof.scope, sequence, event, None, model, None, causal_switch, 0)
        await TelemetryClient(machine.transport).ingest(observation)
        from mop.server.sessions.telemetry_models import TelemetryReceipt
        return await self.sql(lambda: TelemetryReceipt.objects.get(sequence=sequence).pk)

    def query_metadata(self, registration, admission, proof):
        return dict(schema_version=1, work=registration.work.wire(),
            admission=dict(attempt_id=admission.attempt_id, request_id=admission.request_id,
                revision=str(admission.revision), lease_until=admission.lease_until.isoformat()),
            scope=proof.scope.wire(), physical=dict(pid=str(proof.pid), process_start=proof.process_start))

    async def raw_query(self, machine, metadata):
        import aiohttp
        transport = machine.transport
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=transport._tls),
                headers={'Authorization': 'Token '+transport._token},
                timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as client:
            async with client.post(transport.origin+'work/ready-receipt', json=metadata,
                    allow_redirects=False) as response:
                return response.status, dict(response.headers), await response.text()

    def test_actual_https_query_returns_existing_committed_model_receipt(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            identity = await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            from mop.server.sessions import models as sm
            from mop.server.sessions.producer_models import SessionProducer, CurrentProducer
            from mop.server.sessions.telemetry_models import SessionObservation, TelemetryReceipt, ModelObservation
            from mop.server.operations import models as om
            def snapshot():
                return tuple(list(model.objects.order_by('pk').values()) for model in
                    (sm.AssignmentAttempt, sm.ResultReceipt, sm.ReadinessReceipt, SessionProducer, CurrentProducer,
                     SessionObservation, TelemetryReceipt, ModelObservation, om.Operation, om.Step, om.ResourceClaim))
            before = await self.sql(snapshot)
            status, headers, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
            self.assertEqual(status, 200, text)
            self.assertEqual(await self.sql(snapshot), before, 'successful query must be read-only too')
            import json
            value = json.loads(text)
            self.assertEqual(value['receipt_id'], str(identity), 'receipt must be the committed database identity')
            self.assertEqual(value['model'], payload['selection']['desired_model'])
            self.assertEqual(value['confirmed_sequence'], '1')
            self.assertEqual(headers.get('Cache-Control'), 'no-store')
            self.assertNotIn(self.machine_key, text)
        self.check_current(exercise)

    def test_nonempty_wrong_model_cannot_confirm_the_desired_model(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            target, _ = await view.resolve(registration.work)
            proof = await view.proof(registration.work)
            class WrongModel:
                desired_model = payload['selection']['desired_model']
                async def ready(inner, work, observed):
                    return ReadyReceipt(registration, proof.scope, proof.pid, proof.process_start,
                        '42', 'definitely-not-the-selected-model')
            self.assertFalse(await ReadyConfirmation(view, WrongModel()).confirmed(registration.work, target),
                'a nonempty model is not equality with the immutable desired selection')
        self.check_current(exercise)

    def test_successful_result_without_committed_model_is_refused(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            with self.assertRaises((Refused, Unavailable), msg='native ACK alone is not ready/model evidence'):
                await machine.result(admission, 'succeeded', 'ready')
        self.check_current(exercise)

    async def query(self, machine, registration, admission, proof):
        import json
        status, headers, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def test_query_without_a_committed_switch_is_not_ready_and_does_not_mint_evidence(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            from mop.server.sessions.telemetry_models import SessionObservation, TelemetryReceipt, ModelObservation
            def snapshot():
                return (list(SessionObservation.objects.values()), list(TelemetryReceipt.objects.values()),
                    list(ModelObservation.objects.values()))
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            before = await self.sql(snapshot)
            status, _, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
            self.assertIn(status, (409, 503), text)
            self.assertEqual(await self.sql(snapshot), before, 'unavailable query must not create observations/receipts')
            await self.telemetry(machine, runtime, payload['selection']['desired_model'], event='Statusline')
            before = await self.sql(snapshot)
            status, _, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
            self.assertIn(status, (409, 503), 'same-model statusline reassertion is not a switch: '+text)
            self.assertEqual(await self.sql(snapshot), before)
        self.check_current(exercise)

    def test_query_refuses_wrong_model_then_allows_a_fresh_switch_after_a_gap(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            await self.telemetry(machine, runtime, 'different-model')
            status, _, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
            self.assertIn(status, (409, 503), text)
            receipt = await self.telemetry(machine, runtime, payload['selection']['desired_model'], sequence=3)
            value = await self.query(machine, registration, admission, proof)
            self.assertEqual((value['model'], value['receipt_id'], value['confirmed_sequence']),
                (payload['selection']['desired_model'], str(receipt), '3'))
            from mop.server.sessions.telemetry_models import SessionObservation
            self.assertTrue(await self.sql(lambda: SessionObservation.objects.get().incomplete),
                'genuine fresh current model does not erase historical incompleteness')
        self.check_current(exercise)

    def test_query_rejects_scope_process_admission_and_extension_substitutions(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            original = self.query_metadata(registration, admission, proof)
            import copy
            cases = [('scope', key, str(uuid4())) for key in
                ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch', 'producer_epoch')]
            cases += [('scope', 'session_id', 'older-session'), ('physical', 'pid', '124'),
                ('physical', 'process_start', str(uuid4())+':124'),
                ('admission', 'attempt_id', str(uuid4())), ('admission', 'request_id', str(uuid4())),
                ('admission', 'revision', '2'), ('admission', 'lease_until', registration.work.not_after.isoformat()),
                ('work', 'owner_id', '999'), ('work', 'step_id', str(uuid4())),
                ('work', 'action', 'native_exec'), ('work', 'generation', '9')]
            for section, field, replacement in cases:
                value = copy.deepcopy(original)
                value[section][field] = replacement
                with self.subTest(section=section, field=field):
                    status, _, text = await self.raw_query(machine, value)
                    self.assertIn(status, (400, 403, 404, 409, 503), text)
            status, _, text = await self.raw_query(machine, {**original, 'owner_authority': 'not-a-capability'})
            self.assertEqual(status, 400, text)
        self.check_current(exercise)

    def test_query_revalidates_current_producer_native_receipt_and_retained_fences(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            from mop.server.sessions import models as sm
            from mop.server.sessions.producer_models import CurrentProducer, SessionProducer
            from mop.server.operations import models as om
            from mop.server.controller.models import Installation, RestoreGuard
            from mop.server.catalog.models import NodeProject
            from django.utils import timezone
            await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            await self.query(machine, registration, admission, proof)
            # SQL corruption/revocation simulations, restored exactly between cases.
            mutations = [(SessionProducer, {}, 'attempt_id', uuid4()),
                (SessionProducer, {}, 'pid', 124), (SessionProducer, {}, 'process_start', str(uuid4())+':124'),
                (SessionProducer, {}, 'executor_user_id', self.user.pk),
                (SessionProducer, {}, 'assignment_revision', 999),
                (sm.ResultReceipt, {'attempt_id': self.attempt.id}, 'outcome', 'unknown'),
                (om.Step, {'pk': registration.work.step_id}, 'token', uuid4()),
                (om.Step, {'pk': registration.work.step_id}, 'lease_until', timezone.now()),
                (om.ResourceClaim, {'resource_id': 'target:'+registration.work.target_id}, 'token', uuid4()),
                (om.ResourceClaim, {'resource_id': 'target:'+registration.work.target_id}, 'released_at', timezone.now()),
                (om.ScheduledPuppet, {}, 'restore_generation', uuid4()),
                (om.ScheduledPuppet, {}, 'modify_index', 999),
                (sm.NodeExecutor, {}, 'executor_epoch', uuid4()),
                (sm.Assignment, {'pk': registration.work.target_id}, 'expires_at', timezone.now()),
                (Installation, {'pk': 1}, 'effects_enabled', False),
                (Installation, {'pk': 1}, 'epoch', uuid4()), (RestoreGuard, {'pk': 1}, 'generation', uuid4())]
            for model, lookup, field, replacement in mutations:
                def change():
                    rows = model.objects.filter(**lookup)
                    old = list(rows.values_list('pk', field))
                    self.assertTrue(old)
                    # Corruption simulation намеренно обходит immutable ORM guard.
                    from django.db.models import QuerySet
                    QuerySet.update(rows, **{field: replacement})
                    return old
                previous = await self.sql(change)
                try:
                    with self.subTest(model=model.__name__, field=field):
                        status, _, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
                        self.assertIn(status, (403, 409, 503), text)
                finally:
                    def restore():
                        for pk, old in previous:
                            from django.db.models import QuerySet
                            QuerySet.update(model.objects.filter(pk=pk), **{field: old})
                    await self.sql(restore)
            def remove_project():
                NodeProject.objects.filter(node_id=self.assignment.node_id, project=self.project).delete()
            await self.sql(remove_project)
            status, _, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
            self.assertIn(status, (403, 409, 503), text)
        self.check_current(exercise)

    def test_query_then_model_change_cannot_commit_the_old_evidence(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            original = await self.query(machine, registration, admission, proof)
            await self.telemetry(machine, runtime, 'changed-after-query', sequence=2)
            with self.assertRaises((Refused, Unavailable)):
                await machine.transport.request('POST', 'work/result', payload=dict(schema_version=1,
                    attempt_id=admission.attempt_id, expected_revision=str(admission.revision), receipt_id=str(uuid4()),
                    outcome='succeeded', evidence='ready', ready_evidence=original))
            from mop.server.sessions.models import ReadinessReceipt
            self.assertFalse(await self.sql(lambda: ReadinessReceipt.objects.exists()))
        self.check_current(exercise)

    def test_concrete_source_exact_model_physical_recheck_and_successful_commit(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            from mop.node import body_launch
            source_type = getattr(body_launch, 'AddressedReadinessSource', None)
            self.assertIsNotNone(source_type, 'production ReadinessSource must use the real machine query')
            identity = await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            authority = CurrentLaunchAuthority(machine, registration, admission)
            view = await runtime.current_view(authority)
            target, _ = await view.resolve(registration.work)
            source = source_type(authority, desired_model=payload['selection']['desired_model'],
                native_attempt_id=str(self.attempt.id))
            confirmation = ReadyConfirmation(view, source)
            self.assertTrue(await confirmation.confirmed(registration.work, target))
            self.assertEqual(confirmation.evidence.receipt_id, identity)
            self.assertIn('work/ready-receipt', calls)
            from mop.server.sessions.telemetry_models import SessionObservation, ModelObservation, TelemetryReceipt
            before = await self.sql(lambda: (list(SessionObservation.objects.values()),
                list(ModelObservation.objects.values()), list(TelemetryReceipt.objects.values())))
            await machine.result(admission, 'succeeded', 'ready', ready_evidence=confirmation.evidence)
            self.assertEqual(await self.sql(lambda: (list(SessionObservation.objects.values()),
                list(ModelObservation.objects.values()), list(TelemetryReceipt.objects.values()))), before)
        self.check_current(exercise, ready_count=1)

    def test_source_rejects_nonempty_wrong_model_and_physical_change_after_query(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            from mop.node import body_launch
            source_type = getattr(body_launch, 'AddressedReadinessSource', None)
            self.assertIsNotNone(source_type, 'production ReadinessSource must be bound')
            await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            authority = CurrentLaunchAuthority(machine, registration, admission)
            view = await runtime.current_view(authority)
            target, _ = await view.resolve(registration.work)
            source = source_type(authority, desired_model='nonempty-but-wrong', native_attempt_id=str(self.attempt.id))
            with self.assertRaises(Refused):
                await ReadyConfirmation(view, source).confirmed(registration.work, target)
            source = source_type(authority, desired_model=payload['selection']['desired_model'], native_attempt_id=str(self.attempt.id))
            from unittest.mock import patch
            proof = await view.proof(registration.work)
            confirmation = ReadyConfirmation(view, source)
            # Query честный, только второе physical observation уже другое.
            with patch.object(view, 'proof', side_effect=[proof, replace(proof, process_start=str(uuid4())+':999')]):
                self.assertFalse(await confirmation.confirmed(registration.work, target))
            self.assertIsNone(confirmation.evidence, 'failed physical recheck cannot export successful result evidence')
        self.check_current(exercise)

    async def result_with(self, machine, admission, value):
        return await machine.transport.request('POST', 'work/result', payload=dict(schema_version=1,
            attempt_id=admission.attempt_id, expected_revision=str(admission.revision), receipt_id=str(uuid4()),
            outcome='succeeded', evidence='ready', ready_evidence=value))

    def test_query_to_result_rechecks_claim_epoch_restore_producer_time_and_permissions(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            from datetime import timedelta
            from django.utils import timezone
            from django.contrib.auth import get_user_model
            from django.contrib.auth.models import Permission
            from mop.server.sessions import models as sm
            from mop.server.sessions.producer_models import CurrentProducer, SessionProducer
            from mop.server.sessions.telemetry_models import SessionObservation
            from mop.server.operations import models as om
            from mop.server.controller.models import Installation, RestoreGuard
            await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            original = await self.query(machine, registration, admission, proof)
            mutations = [(SessionObservation, {}, 'model_received_at', timezone.now()-timedelta(seconds=31)),
                (SessionObservation, {}, 'model_proven', False),
                (SessionObservation, {}, 'revision', 999),
                (SessionProducer, {}, 'attempt_id', uuid4()), (SessionProducer, {}, 'producer_epoch', uuid4()),
                (SessionProducer, {}, 'process_start', str(uuid4())+':999'),
                (CurrentProducer, {}, 'revision', 2),
                (sm.AssignmentAttempt, {'pk': admission.attempt_id}, 'lease_until', timezone.now()),
                (sm.AssignmentAttempt, {'pk': admission.attempt_id}, 'request_id', uuid4()),
                (sm.Assignment, {'pk': registration.work.target_id}, 'expires_at', timezone.now()),
                (sm.NodeExecutor, {}, 'executor_epoch', uuid4()),
                (om.Step, {'pk': registration.work.step_id}, 'token', uuid4()),
                (om.ResourceClaim, {'resource_id': 'target:'+registration.work.target_id}, 'released_at', timezone.now()),
                (om.ResourceClaim, {'resource_id': 'target:'+registration.work.target_id}, 'token', uuid4()),
                (om.ScheduledPuppet, {}, 'restore_generation', uuid4()),
                (Installation, {'pk': 1}, 'epoch', uuid4()), (Installation, {'pk': 1}, 'effects_enabled', False),
                (RestoreGuard, {'pk': 1}, 'generation', uuid4())]
            for model, lookup, field, replacement in mutations:
                def change():
                    rows = model.objects.filter(**lookup)
                    previous = list(rows.values_list('pk', field))
                    self.assertTrue(previous)
                    # Corruption simulation намеренно обходит immutable ORM guard.
                    from django.db.models import QuerySet
                    QuerySet.update(rows, **{field: replacement})
                    return previous
                previous = await self.sql(change)
                try:
                    with self.subTest(model=model.__name__, field=field), self.assertRaises((Refused, Unavailable)):
                        await self.result_with(machine, admission, original)
                    self.assertFalse(await self.sql(lambda: sm.ReadinessReceipt.objects.exists()))
                finally:
                    def restore():
                        for pk, value in previous:
                            from django.db.models import QuerySet
                            QuerySet.update(model.objects.filter(pk=pk), **{field: value})
                    await self.sql(restore)
            def revoke_read():
                user = get_user_model().objects.get(pk=self.machine.user_id)
                user.user_permissions.remove(Permission.objects.get(content_type__app_label='execution', codename='read_launch'))
            await self.sql(revoke_read)
            status, _, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
            self.assertEqual(status, 403, text)
            with self.assertRaises(Refused):
                await self.result_with(machine, admission, original)
        self.check_current(exercise)

    def test_query_then_same_model_reassertion_or_new_switch_cannot_reuse_old_snapshot(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            desired = payload['selection']['desired_model']
            await self.telemetry(machine, runtime, desired)
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            original = await self.query(machine, registration, admission, proof)
            await self.telemetry(machine, runtime, desired, event='Statusline', sequence=2)
            status, _, text = await self.raw_query(machine, self.query_metadata(registration, admission, proof))
            self.assertIn(status, (409, 503), 'uncausal same model must not restore proof: '+text)
            with self.assertRaises((Refused, Unavailable)):
                await self.result_with(machine, admission, original)
            await self.telemetry(machine, runtime, desired, sequence=3)
            fresh = await self.query(machine, registration, admission, proof)
            self.assertNotEqual(fresh['receipt_id'], original['receipt_id'])
            with self.assertRaises((Refused, Unavailable)):
                await self.result_with(machine, admission, original)
        self.check_current(exercise)

    def test_query_requires_current_enrolled_machine_token_not_human_or_other_purpose(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            from mop.server.access import services as auth
            from mop.node.http_execution import HttpsTransport, MachineClient
            await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            view = await runtime.current_view(CurrentLaunchAuthority(machine, registration, admission))
            proof = await view.proof(registration.work)
            metadata = self.query_metadata(registration, admission, proof)
            other = await self.sql(lambda: auth.enroll_service('ready-activation-only', self.assignment.node_id,
                project_ids=(self.project.pk,), permissions=('access.activate_grant',), use_case='grant-activator'))
            def foreign_executors():
                from mop.server.catalog.models import Node, NodeProject
                node = Node.objects.create(name='ready-foreign-node', driver='host',
                    ssh_host='example.invalid', control_origin='https://example.invalid',
                    lan_address='example.invalid', bootstrap_address='example.invalid')
                NodeProject.objects.create(node=node, project=self.project)
                permissions = ('execution.claim_work', 'execution.read_launch')
                wrong_node = auth.enroll_service('ready-wrong-node', node.pk,
                    project_ids=(self.project.pk,), permissions=permissions)
                wrong_project = auth.enroll_service('ready-without-project', self.assignment.node_id,
                    project_ids=(), permissions=permissions)
                return wrong_node, wrong_project
            foreign_keys = await self.sql(foreign_executors)
            for key in (self.token, other, *foreign_keys):
                foreign = MachineClient(HttpsTransport(machine.transport.origin.removesuffix('/api/v1/machine/'),
                    key, tls=machine.transport._tls))
                try:
                    status, _, text = await self.raw_query(foreign, metadata)
                    self.assertIn(status, (403, 404) if key in foreign_keys else (403,), text)
                finally:
                    await foreign.aclose()
            from django.contrib.auth import get_user_model
            await self.sql(lambda: get_user_model().objects.get(pk=self.machine.user_id).auth_token.delete())
            status, _, text = await self.raw_query(machine, metadata)
            self.assertIn(status, (401, 403), text)
        self.check_current(exercise)

    def test_stale_model_cannot_be_committed_as_successful_ready(self):
        async def exercise(machine, runtime, registration, admission, calls, payload):
            from datetime import timedelta
            from django.utils import timezone
            from mop.server.sessions.telemetry_models import SessionObservation
            await self.telemetry(machine, runtime, payload['selection']['desired_model'])
            await self.sql(lambda: SessionObservation.objects.update(model_received_at=timezone.now()-timedelta(seconds=31)))
            with self.assertRaises((Refused, Unavailable), msg='result must revalidate current model freshness'):
                await machine.result(admission, 'succeeded', 'ready')
        self.check_current(exercise)
