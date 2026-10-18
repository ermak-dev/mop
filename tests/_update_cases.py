"""Admission update (#625), настоящий PostgreSQL и protected credential socket.

HYPOTHESIS: claimed prepare существует лишь в seeded фикстуре; HTTP не может
построить immutable план и занять общую capacity до credential I/O.
SPEC: default fresh=false, exact source/owner/policy/continuation, новый grant
и incarnation без cutover/rotation. Потерянный ответ -- unknown с fences,
повтор читает журнал и никогда не повторяет prepare.
SOLUTION: immutable admission и claimed prepare commit до protected credential I/O;
request-local credential не сохраняется, replay/recovery только читают proof.
RESULT: настоящий RED 404 вместо 503; отдельные RED на revoke после commit,
crash-recovery, потерю exact claim и отсутствие capture; затем 30/30
PostgreSQL 17 и 2/2 UID gate.
Проверяется admission до PREPARED, не физическая замена.
STATUS: FIXED — see #625 (только admission/preparation).
"""
import json
from datetime import timedelta
from uuid import UUID, uuid4
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from _replacement_cases import ReplacementPreparation


@override_settings(ALLOWED_HOSTS=['testserver'])
class UpdateAdmission(TransactionTestCase):
    serialized_rollback = True
    setUp = ReplacementPreparation.setUp
    ready, body, submit = ReplacementPreparation.ready, ReplacementPreparation.body, ReplacementPreparation.submit
    launch, step = ReplacementPreparation.launch, ReplacementPreparation.step
    machine = ReplacementPreparation.machine
    through_readiness, activator = ReplacementPreparation.through_readiness, ReplacementPreparation.activator

    def source(self):
        from mop.server.sessions.models import Assignment
        from mop.server.access.models import Grant
        operation, target, _ = self.launch()
        self.ops.activations.activator = self.activator()
        self.through_readiness(operation)
        self.ops.activations.run(operation)
        self.old = Assignment.objects.get(pk=target)
        self.old_grant = Grant.objects.select_related('intent').get(pk=self.old.grant_id)
        return target

    def envelope(self, **params):
        return dict(schema_version=1, epoch=str(self.epoch), request_id=str(uuid4()),
            incarnation=str(self.old.current_incarnation_id), expected_revision=str(self.old.revision),
            not_after=(timezone.now()+timedelta(minutes=2)).isoformat(), params=params)

    def update(self, body, client=None):
        return (client or self.client).post('/api/v1/puppets/'+str(self.old.pk)+'/commands/update',
            body, content_type='application/json', secure=True)

    def test_real_update_route_refuses_missing_capability_before_effects(self):
        from mop.server.operations.models import Operation
        from mop.server.access.models import Grant
        self.source()
        before = (Operation.objects.count(), Grant.objects.count())
        result = self.update(self.envelope())
        self.assertEqual(result.status_code, 503, result.content)
        self.assertEqual((Operation.objects.count(), Grant.objects.count()), before)

    def support(self):
        from mop.server.operations.replacement import SourcePin, REPLACEMENT_CAPABILITIES
        from mop.server.controller.machinebinding import Targets
        target = Targets(self.grants).current(self.old.pk)
        origin = self.project.origin
        clone, continuation = uuid4(), uuid4()
        class Support:
            capabilities = REPLACEMENT_CAPABILITIES
            def capture(inner, source, *, fresh, origin):
                self.assertEqual(source, target)
                return SourcePin(source, origin, 'refs/heads/work', 'e'*40, clone,
                                 None if fresh else continuation)
        return Support()

    def compose(self, issuer=None, support=True):
        from mop.server.controller.updatebinding import service
        from mop.server.controller.credentials import CredentialApplication
        from mop.server.controller.models import Installation, RestoreGuard
        from mop.common.credential_client import CredentialClient
        from mop.common.credentials import GrantReceipt
        installation = Installation.objects.get(pk=1)
        app = CredentialApplication(self.grants.secrets.provider,
            dict(identity=installation.identity, epoch=str(installation.epoch), root_digest=installation.root_digest,
                 effects_enabled=True), str(RestoreGuard.objects.get(pk=1).generation))
        self.prepare_calls = []
        class Issuer:
            def prepare_replacement(inner, credential, operation_id):
                from django.db import connection
                self.assertFalse(connection.in_atomic_block, 'credential I/O must follow admission COMMIT')
                self.prepare_calls.append((credential.kind, operation_id))
                from mop.common.credentials import Command
                result = app.execute(credential, Command.PREPARE_REPLACEMENT, dict(operation_id=operation_id))
                return CredentialClient._result(GrantReceipt, result)
        self.update_ops = service(grants=self.grants, issuer=issuer or Issuer(), scheduler=self.scheduler,
                                  support=self.support() if support else None)
        self.addCleanup(patch.stopall)
        patch('mop.server.controller.operationviews.update_api', lambda: self.update_ops).start()
        return app

    def prepared(self, *, fresh=False):
        self.source()
        self.compose()
        body = self.envelope(fresh=fresh)
        response = self.update(body)
        self.assertEqual(response.status_code, 202, response.content)
        return body, response.json()['operation']

    def test_prepare_pins_exact_policy_owner_actor_token_and_preserves_source(self):
        from rest_framework.authtoken.models import Token
        from mop.server.sessions.models import Assignment, ExecutionIncarnation
        from mop.server.operations.models import Operation, ReplacementBinding, ReplacementPlan, RestartBinding, Step
        from mop.server.access.models import Grant, CredentialBinding, AgentProfile
        self.source()
        before = Assignment.objects.values().get(pk=self.old.pk)
        token = Token.objects.get(user_id=self.old_grant.subject_id).key
        count = CredentialBinding.objects.count()
        self.compose()
        body = self.envelope()
        result = self.update(body)
        self.assertEqual(result.status_code, 202, result.content)
        public = result.json()['operation']
        row = Operation.objects.get(pk=public['id'])
        binding = ReplacementBinding.objects.get(operation=row)
        plan = ReplacementPlan.objects.get(operation=row)
        destination = RestartBinding.objects.get(operation=row)
        self.assertEqual((row.action, row.state, row.code), ('update', 'running', 'replacement_prepared'))
        self.assertEqual(Step.objects.get(operation=row, name='prepare_launch').outcome, 'succeeded')
        self.assertTrue(public['resources_held'])
        self.assertEqual(Assignment.objects.values().get(pk=self.old.pk), before)
        self.assertFalse(ExecutionIncarnation.objects.filter(pk=row.incarnation_id).exists())
        self.assertNotEqual(row.incarnation_id, self.old.current_incarnation_id)
        self.assertNotEqual(destination.grant_id, self.old.grant_id)
        grant = Grant.objects.select_related('intent').get(pk=destination.grant_id)
        self.assertEqual((grant.subject_id, grant.state), (self.old_grant.subject_id, 'reserved'))
        self.assertIsNotNone(grant.credential_version_id)
        self.assertEqual((grant.intent.policy_approval_id, grant.intent.policy_version_id, grant.intent.policy_digest),
            (self.old_grant.intent.policy_approval_id, self.old_grant.intent.policy_version_id, self.old_grant.intent.policy_digest))
        self.assertEqual(grant.intent.expires_at, min(self.old.expires_at, self.old_grant.intent.expires_at))
        self.assertEqual(Token.objects.get(user_id=grant.subject_id).key, token)
        self.assertEqual(CredentialBinding.objects.count(), count)
        self.assertEqual(AgentProfile.objects.get(target_id=self.old.pk).agent_id, grant.subject_id)
        self.assertEqual((plan.fresh, plan.origin, plan.source_commit), (False, self.project.origin, 'e'*40))
        self.assertIsNotNone(plan.continuation_ref)
        self.assertEqual((plan.destination_incarnation_id, plan.destination_grant_id), (row.incarnation_id, destination.grant_id))
        self.assertEqual((binding.source_revision, destination.generation, row.expected_revision),
                         (self.old.revision, self.old.generation+1, self.old.revision+1))
        self.assertEqual(public['source']['incarnation'], str(self.old.current_incarnation_id))
        self.assertEqual(public['destination']['incarnation'], str(row.incarnation_id))
        self.assertNotIn(str(plan.continuation_ref), json.dumps(public))
        with self.assertRaises(ValueError):
            ReplacementPlan.objects.filter(operation=row).update(source_commit='f'*40)
        with self.assertRaises(ValueError):
            plan.save()
        again = self.update(body)
        self.assertEqual(again.json()['operation']['id'], str(row.id))
        self.assertEqual(len(self.prepare_calls), 1)
        self.assertEqual(self.update({**body, 'params': {'fresh': True}}).status_code, 409)

    def test_fresh_clean_dialogue_is_explicit_and_does_not_choose_a_continuation(self):
        from mop.server.operations.models import ReplacementPlan
        _, operation = self.prepared(fresh=True)
        self.assertTrue(ReplacementPlan.objects.get(operation_id=operation['id']).fresh)
        self.assertIsNone(ReplacementPlan.objects.get(operation_id=operation['id']).continuation_ref)

    def test_invalid_or_cross_project_source_is_refused_before_credential_io(self):
        from mop.server.operations.models import Operation
        self.source()
        self.compose()
        before = Operation.objects.count()
        for params in ({'fresh': 1}, {'origin': ''}, {'origin': '-x'}, {'origin': 'x'*2049},
                       {'origin': 'https://elsewhere.invalid/vertical.git'}, {'force': True},
                       {'source_ref': 'refs/heads/other'}, {'origin': None}):
            with self.subTest(params=params):
                result = self.update(self.envelope(**params))
                self.assertIn(result.status_code, (400, 409), result.content)
                self.assertEqual(Operation.objects.count(), before)
                self.assertEqual(self.prepare_calls, [])

    def test_missing_continuation_and_missing_support_refuse_before_journal(self):
        from mop.server.operations.models import Operation
        self.source()
        self.compose(support=False)
        before = Operation.objects.count()
        result = self.update(self.envelope())
        self.assertEqual(result.status_code, 503, result.content)
        self.assertEqual(Operation.objects.count(), before)
        self.assertEqual(self.prepare_calls, [])
        self.update_ops.updates.support = self.support()
        capture = self.update_ops.updates.support.capture
        from dataclasses import replace
        self.update_ops.updates.support.capture = lambda *args, **kwargs: replace(capture(*args, **kwargs), continuation_ref=None)
        result = self.update(self.envelope())
        self.assertEqual(result.status_code, 503, result.content)
        self.assertEqual(Operation.objects.count(), before)

    def test_declared_capabilities_without_the_public_port_refuse_before_journal(self):
        from types import SimpleNamespace
        from mop.server.operations.models import Operation
        from mop.server.operations.replacement import REPLACEMENT_CAPABILITIES
        self.source()
        self.compose()
        before = Operation.objects.count()
        self.update_ops.updates.support = SimpleNamespace(capabilities=REPLACEMENT_CAPABILITIES)
        response = self.update(self.envelope())
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(Operation.objects.count(), before)
        self.assertEqual(self.prepare_calls, [])

    def test_unknown_preparation_is_durable_replay_only_and_metadata_recovery_never_resubmits(self):
        from mop.server.operations.models import Operation, ResourceClaim, Step
        self.source()
        self.compose()
        original = self.update_ops.updates.issuer.prepare_replacement
        def lose(*args):
            original(*args)
            raise OSError('synthetic reply loss including private-token')
        self.update_ops.updates.issuer.prepare_replacement = lose
        body = self.envelope()
        response = self.update(body)
        self.assertEqual(response.status_code, 202, response.content)
        operation = response.json()['operation']
        self.assertEqual(operation['state'], 'unknown')
        self.assertNotIn('private-token', response.content.decode())
        row = Operation.objects.get(pk=operation['id'])
        self.assertEqual(ResourceClaim.objects.filter(operation=row, released_at=None).count(), 4)
        self.assertEqual(self.update(body).json()['operation']['id'], str(row.id))
        self.assertEqual(len(self.prepare_calls), 1)
        # Read-only recovery may confirm prep, never schedule remaining effects.
        recovered = self.update_ops.updates.reconcile(self.actor, row.id)
        self.assertEqual(recovered['state'], 'unknown')
        self.assertEqual(Step.objects.get(operation=row, name='prepare_launch').outcome, 'succeeded')
        self.assertTrue(recovered['resources_held'])
        self.assertEqual(len(self.prepare_calls), 1)

    def test_competing_update_and_assignment_release_are_fenced(self):
        from mop.server.controller.runtime import assignments_service
        from mop.server.sessions.models import Assignment
        self.prepared()
        other = self.update(self.envelope())
        self.assertEqual(other.status_code, 409, other.content)
        with self.assertRaises(ValueError):
            assignments_service().release(self.actor, self.old.pk, expected_revision=self.old.revision)
        self.assertEqual(Assignment.objects.get(pk=self.old.pk).current_incarnation_id, self.old.current_incarnation_id)

    def test_concurrent_same_key_replays_claim_while_credential_io_is_in_flight(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import connections
        from django.test import Client
        from mop.server.operations.models import Operation, ResourceClaim
        self.source()
        self.compose()
        body = self.envelope()
        entered, release = Event(), Event()
        original = self.update_ops.updates.issuer.prepare_replacement
        def waiting(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError('owned test did not release preparation')
            return original(*args)
        self.update_ops.updates.issuer.prepare_replacement = waiting
        def first():
            try:
                return self.update(body, Client(HTTP_AUTHORIZATION='Token '+self.key))
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first)
            try:
                self.assertTrue(entered.wait(10), 'first admission must commit before external wait')
                replay = self.update(body)
                self.assertEqual(replay.status_code, 202, replay.content)
                self.assertEqual(replay.json()['operation']['state'], 'running')
                self.assertEqual(self.update({**body, 'params': {'fresh': True}}).status_code, 409)
                competing = self.update(self.envelope())
                self.assertEqual(competing.status_code, 409, competing.content)
                self.assertEqual(Operation.objects.filter(action='update').count(), 1)
                self.assertEqual(ResourceClaim.objects.filter(operation__action='update', released_at=None).count(), 4)
            finally:
                release.set()
            result = pending.result(timeout=10)
        self.assertEqual(result.status_code, 202, result.content)
        self.assertEqual(result.json()['operation']['id'], replay.json()['operation']['id'])
        self.assertEqual(result.json()['operation']['code'], 'replacement_prepared')
        self.assertEqual(len(self.prepare_calls), 1)

    def test_right_revoked_after_credential_commit_cannot_confirm_prepared(self):
        from _grant_cases import permit_actions
        from mop.server.access import models as am
        self.source()
        self.compose()
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        am.ProjectMembership.objects.get_or_create(user=self.user, project=self.project,
            source=am.IdentitySource.objects.get(name='local'))
        permit_actions(self.user, 'operations.view_operation', 'operations.recover_operation',
            'execution.update_puppet', 'execution.create_puppet', 'access.issue_grant',
            'access.view_role', 'preparation.approve_bootstrap')
        original = self.update_ops.updates.issuer.prepare_replacement
        def revoke(*args):
            result = original(*args)
            from django.contrib.auth.models import Permission
            self.user.user_permissions.remove(Permission.objects.get(content_type__app_label='execution', codename='update_puppet'))
            return result
        self.update_ops.updates.issuer.prepare_replacement = revoke
        result = self.update(self.envelope())
        self.assertEqual(result.status_code, 202, result.content)
        operation = result.json()['operation']
        self.assertEqual(operation['state'], 'unknown')
        self.assertTrue(operation['resources_held'])

    def test_crashed_prepare_recovery_cannot_resume_physical_work(self):
        from mop.server.operations.models import Operation
        self.source()
        self.compose()
        original = self.update_ops.updates.issuer.prepare_replacement
        def crash(*args):
            original(*args)
            raise SystemExit('synthetic process death before result commit')
        self.update_ops.updates.issuer.prepare_replacement = crash
        body = self.envelope()
        with self.assertRaises(SystemExit):
            self.update(body)
        row = Operation.objects.get(action='update')
        self.assertEqual(row.state, 'running')
        recovered = self.update_ops.updates.reconcile(self.actor, row.id)
        self.assertEqual(recovered['state'], 'unknown')
        self.assertTrue(recovered['resources_held'])
        self.assertEqual(recovered['steps'][0]['outcome'], 'succeeded')
        self.assertEqual(len(self.prepare_calls), 1)

    def test_result_without_its_exact_claim_cannot_confirm_prepared(self):
        from mop.server.operations.models import ResourceClaim
        self.source()
        self.compose()
        original = self.update_ops.updates.issuer.prepare_replacement
        def release(*args):
            result = original(*args)
            ResourceClaim.objects.filter(operation_id=args[1], resource_id='target:'+str(self.old.pk)).update(
                released_at=timezone.now())
            return result
        self.update_ops.updates.issuer.prepare_replacement = release
        result = self.update(self.envelope())
        self.assertEqual(result.status_code, 202, result.content)
        self.assertEqual(result.json()['operation']['state'], 'unknown')
        self.assertNotEqual(result.json()['operation']['code'], 'replacement_prepared')

    def test_update_schema_describes_default_fresh_and_bounded_origin(self):
        from mop.server.controller.openapi import schema
        document = schema()
        operation = document['paths']['/api/v1/puppets/{target_id}/commands/update']['post']
        body = operation['requestBody']['content']['application/json']['schema']
        envelope = document['components']['schemas'][body['$ref'].rsplit('/', 1)[1]]
        params = envelope['properties']['params']
        if '$ref' in params:
            params = document['components']['schemas'][params['$ref'].rsplit('/', 1)[1]]
        self.assertEqual(params['properties']['fresh']['default'], False)
        self.assertEqual(params['properties']['origin']['maxLength'], 2048)

    def test_provider_wait_source_change_never_claims_prepared(self):
        from mop.server.sessions.models import Assignment
        self.source()
        self.compose()
        provider = self.grants.secrets.provider
        original = type(provider).encrypt
        def change(instance, *args, **kwargs):
            envelope = original(instance, *args, **kwargs)
            Assignment.objects.filter(pk=self.old.pk).update(revision=self.old.revision+1)
            return envelope
        with patch.object(type(provider), 'encrypt', change):
            result = self.update(self.envelope())
        self.assertEqual(result.status_code, 200, result.content)
        operation = result.json()['operation']
        # Credential-служба доказанно отказала и уже закончила вызов: source
        # остался старым, нового credential нет, физического dispatch не было.
        self.assertEqual(operation['state'], 'failed')
        self.assertNotEqual(operation['code'], 'replacement_prepared')

    def test_worker_cannot_discover_or_execute_human_prepare(self):
        _, operation = self.prepared()
        # Production discovery не знает human preparation lane. Следующую
        # scheduler полосу parent свяжет отдельно, не generic node executor.
        with patch.object(self.update_ops.updates.issuer, 'prepare_replacement', side_effect=AssertionError('worker prepare')):
            self.assertNotIn(UUID(operation['id']), self.update_ops.pending())
            self.assertIsNone(self.update_ops.claim(UUID(operation['id'])))
            with self.assertRaises(ValueError):
                self.update_ops.pending(handlers=('prepare_launch',))

    def test_http_preparation_crosses_real_protected_credential_socket(self):
        import asyncio
        import os
        import tempfile
        from pathlib import Path
        from asgiref.sync import async_to_sync
        from mop.common.credential_client import CredentialClient
        from mop.server.controller.credentials import CredentialService
        from mop.server.controller.runtime import Runtime
        from mop.server.operations.models import Step
        self.source()
        application = self.compose()
        body = self.envelope()
        with tempfile.TemporaryDirectory(prefix='mop-update-') as directory:
            path = str(Path(directory)/'credential.sock')
            async def exercise():
                runtime = Runtime(database_limit=2, queue_limit=0)
                server = CredentialService(path, application=application, runtime=runtime,
                    allowed_uids=(os.geteuid(),), timeout=3)
                await server.start()
                try:
                    self.update_ops.updates.issuer = CredentialClient(path, server_uid=os.geteuid(), timeout=5)
                    return await asyncio.to_thread(self.update, body)
                finally:
                    await server.aclose()
                    await runtime.aclose()
            result = async_to_sync(exercise)()
        self.assertEqual(result.status_code, 202, result.content)
        self.assertEqual(result.json()['operation']['code'], 'replacement_prepared')
        self.assertEqual(Step.objects.get(operation_id=result.json()['operation']['id'], name='prepare_launch').outcome,
                         'succeeded')

    def test_missing_explicit_permission_denies_before_journal_and_credential(self):
        from _grant_cases import permit_actions
        from mop.server.access import models as am
        from mop.server.operations.models import Operation
        self.source()
        self.compose()
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        am.ProjectMembership.objects.get_or_create(user=self.user, project=self.project,
            source=am.IdentitySource.objects.get(name='local'))
        permissions = ('operations.view_operation', 'execution.update_puppet', 'execution.create_puppet',
                       'access.issue_grant', 'access.view_role', 'preparation.approve_bootstrap')
        before = Operation.objects.count()
        for missing in ('execution.update_puppet', 'execution.create_puppet', 'access.issue_grant'):
            self.user.user_permissions.clear()
            permit_actions(self.user, *(action for action in permissions if action != missing))
            with self.subTest(missing=missing):
                result = self.update(self.envelope())
                self.assertEqual(result.status_code, 403, result.content)
                self.assertEqual(Operation.objects.count(), before)
                self.assertEqual(self.prepare_calls, [])

    def test_deadline_revision_and_source_enrollment_are_checked_before_io(self):
        from mop.server.operations.models import Operation, ScheduledPuppet
        self.source()
        self.compose()
        before = Operation.objects.count()
        for change in ({'expected_revision': '1'}, {'incarnation': str(uuid4())},
                       {'not_after': (timezone.now()-timedelta(seconds=1)).isoformat()},
                       {'not_after': (timezone.now()+timedelta(minutes=6)).isoformat()}):
            with self.subTest(change=change):
                result = self.update({**self.envelope(), **change})
                self.assertEqual(result.status_code, 409, result.content)
                self.assertEqual(Operation.objects.count(), before)
        ScheduledPuppet.objects.filter(target_id=self.old.pk).update(incarnation_id=uuid4())
        result = self.update(self.envelope())
        self.assertEqual(result.status_code, 409, result.content)
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(Operation.objects.count(), before)

    def test_unknown_without_a_grant_never_becomes_a_fresh_prepare(self):
        from mop.server.operations.models import Step
        self.source()
        self.compose()
        def unknown(*args):
            self.prepare_calls.append(('lost', args[1]))
            raise TimeoutError('no proof of dispatch')
        self.update_ops.updates.issuer.prepare_replacement = unknown
        body = self.envelope()
        result = self.update(body)
        self.assertEqual(result.status_code, 202, result.content)
        operation = result.json()['operation']
        self.assertEqual(operation['state'], 'unknown')
        self.assertEqual(self.update(body).json()['operation']['id'], operation['id'])
        recovered = self.update_ops.updates.reconcile(self.actor, UUID(operation['id']))
        self.assertEqual(recovered['state'], 'unknown')
        self.assertTrue(recovered['resources_held'])
        self.assertEqual(Step.objects.get(operation_id=operation['id'], name='prepare_launch').outcome, 'unknown')
        self.assertEqual(len(self.prepare_calls), 1)

    def test_explicit_credential_refusal_is_durable_and_releases_fences_without_retry(self):
        from mop.common.credentials import CredentialRefused
        self.source()
        self.compose()
        def refused(*args):
            self.prepare_calls.append(('refused', args[1]))
            raise CredentialRefused('approved source revoked')
        self.update_ops.updates.issuer.prepare_replacement = refused
        body = self.envelope()
        result = self.update(body)
        self.assertEqual(result.status_code, 200, result.content)
        operation = result.json()['operation']
        self.assertEqual(operation['state'], 'failed')
        self.assertIn('approved source revoked', operation['reason'])
        self.assertFalse(operation['resources_held'])
        self.assertEqual(self.update(body).json()['operation']['id'], operation['id'])
        self.assertEqual(len(self.prepare_calls), 1)

    def test_pending_create_uses_the_same_admission_capacity(self):
        from mop.server.operations.models import CreateReservation, Operation
        self.source()
        self.compose()
        CreateReservation.objects.create(actor=self.user, request_id=uuid4(), digest='b'*64,
            expires_at=timezone.now()+timedelta(days=1), epoch=self.epoch, project=self.project)
        self.update_ops.queue_limit = 1
        before = Operation.objects.count()
        result = self.update(self.envelope())
        self.assertEqual(result.status_code, 429, result.content)
        self.assertEqual(Operation.objects.count(), before)
        self.assertEqual(self.prepare_calls, [])
