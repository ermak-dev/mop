"""Замена инкарнации (#625): узкие контракты до сборки update.

HYPOTHESIS: nonzero register принимает один индекс без точного predecessor,
а чтение старой/чужой Meta после потерянного POST объявляет отказ. Поздний
POST тогда может примениться уже после снятия fences.
SOLUTION: typed predecessor для nonzero CAS; только preflight/атомарный 409
доказывают отказ, readback после возможного dispatch оставляет unknown.
Проверяется PostgreSQL gate и owned HTTP peer, не настоящий Nomad.
RESULT: RED -- два поведенческих отказа и отсутствие predecessor API; GREEN --
15/15 replacement/create registration, Python 3.12 и PostgreSQL 17.
STATUS: FIXED — see #625
"""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from django.test import TransactionTestCase
from django.utils import timezone

from _register_peer import RegisterPeer
from mop.server.operations.puppet import RestartTarget
from mop.server.operations.scheduler import (JobPin, NomadStopScheduler, RegisterTicket,
                                              addressed_metadata)


class ReplacementRegistration(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.peer = RegisterPeer()
        self.addCleanup(self.peer.close)
        self.scheduler = NomadStopScheduler(origin=self.peer.origin, scheduler_id=uuid4(),
                                            token='fixture-only', timeout=.2)
        self.epoch, self.restore = uuid4(), uuid4()
        self.source = RestartTarget(uuid4(), uuid4(), uuid4(), uuid4(), uuid4(),
                                    1, 'worker', 'a'*64, uuid4(), 2, 2)
        self.destination = replace(self.source, incarnation_id=uuid4(), grant_id=uuid4(), generation=3, revision=3)
        self.ticket = RegisterTicket(self.epoch, self.destination, self.scheduler.scheduler_id,
            str(self.source.target_id), 'default', self.restore, 0,
            {'ID': str(self.source.target_id), 'Meta': addressed_metadata(self.epoch, self.restore, self.destination)},
            uuid4(), uuid4(), timezone.now()+timedelta(minutes=1))
        self.pin = JobPin(self.epoch, self.source, self.scheduler.scheduler_id,
                         self.ticket.job_id, 'default', 7, self.restore)

    def old_job(self, **changes):
        value = {'ID': self.ticket.job_id, 'Namespace': 'default', 'JobModifyIndex': 7,
                 'Meta': addressed_metadata(self.epoch, self.restore, self.source), 'Stop': False}
        value.update(changes)
        return value

    def replacement(self):
        return replace(self.ticket, expected_index=7, predecessor=self.pin)

    def test_nonzero_index_without_exact_predecessor_is_not_authority(self):
        with self.assertRaises(ValueError):
            replace(self.ticket, expected_index=7)

    def test_replacement_has_one_nonzero_conditional_post(self):
        self.peer.jobs[self.ticket.job_id] = self.old_job()
        proof = self.scheduler.register_job(self.replacement())
        self.assertEqual((proof.outcome, proof.modify_index), ('succeeded', 8))
        self.assertEqual([(p['EnforceIndex'], p['JobModifyIndex']) for p in self.peer.posts], [(True, 7)])
        self.assertEqual(self.peer.jobs[self.ticket.job_id]['Meta'], self.ticket.job['Meta'])

    def test_foreign_predecessor_at_the_same_index_is_refused_before_post(self):
        self.peer.jobs[self.ticket.job_id] = self.old_job(Meta={**self.old_job()['Meta'], 'mop.grant_id': str(uuid4())})
        self.assertEqual(self.scheduler.register_job(self.replacement()).outcome, 'failed')
        self.assertEqual(self.peer.posts, [])

    def test_predecessor_identity_must_match_the_conditional_registration(self):
        for changes in ({'modify_index': 8}, {'scheduler_id': uuid4()}, {'namespace': 'other'},
                        {'job_id': 'foreign'}, {'restore_generation': uuid4()}, {'epoch': uuid4()},
                        {'target': replace(self.source, target_id=uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.ticket, expected_index=7, predecessor=replace(self.pin, **changes))

    def test_create_index_zero_never_accepts_a_predecessor_or_overwrites_a_job(self):
        with self.assertRaises(ValueError):
            replace(self.ticket, predecessor=self.pin)
        self.peer.jobs[self.ticket.job_id] = self.old_job()
        self.assertEqual(self.scheduler.register_job(self.ticket).outcome, 'failed')
        self.assertEqual(self.peer.posts, [])

    def test_foreign_readback_after_possible_dispatch_is_unknown_not_failed(self):
        self.peer.jobs[self.ticket.job_id] = self.old_job()
        self.assertEqual(self.scheduler.inspect_register(self.ticket).outcome, 'unknown')

    def test_delayed_post_old_readback_cannot_release_the_replacement_fence(self):
        ticket = self.replacement()
        self.peer.jobs[ticket.job_id] = self.old_job()
        self.peer.defer_post = True
        self.peer.lose_post = True
        self.assertEqual(self.scheduler.register_job(ticket).outcome, 'unknown')
        self.assertTrue(self.peer.post_arrived.is_set(), 'the POST must really have reached the owned peer')
        self.assertEqual(self.scheduler.inspect_register(ticket).outcome, 'unknown')
        self.assertEqual(self.peer.jobs[ticket.job_id]['JobModifyIndex'], 7)
        self.peer.release_post.set()
        self.assertTrue(self.peer.post_applied.wait(2), 'the same original POST must apply late')
        self.assertEqual(self.scheduler.inspect_register(ticket).outcome, 'succeeded')
        self.assertEqual(len(self.peer.posts), 1)


# HYPOTHESIS: generic provision справедливо отвергает будущую инкарнацию,
# а generic reserve выбирает latest approval. Замена требует отдельного
# credential use case, читающего только claimed prepare_launch общего журнала.
# SPEC: новые grant/incarnation, прежние policy/Actor/token/Assignment до
# подтверждённого cutover. Worker не получает human credential или crypto.
# Фикстура создаёт claimed журнал явно; это не admission/physical acceptance.
# SOLUTION: immutable source/policy pins, locator-only protected command и
# request-local authority; source/creator/subject/claims повторяются после crypto.
# RESULT: RED — unsupported command и finite action constraint; GREEN — 8/8
# PostgreSQL 17/Python 3.12. Admission/cutover/recycle ещё не реализованы.
# STATUS: FIXED — see #625 (только prospective credential preparation)
class ReplacementPreparation(TransactionTestCase):
    serialized_rollback = True
    from _restart_vertical_cases import RestartVertical
    from _create_cases import CreateVertical, CreateRegistration, CreateMachineSteps, CreateActivation
    setUp = RestartVertical.setUp
    ready, body, submit = CreateVertical.ready, CreateVertical.body, CreateVertical.submit
    launch, step = CreateRegistration.launch, CreateRegistration.step
    machine = CreateMachineSteps.machine
    through_readiness, activator = CreateActivation.through_readiness, CreateActivation.activator

    def fixture(self):
        from django.apps import apps
        from mop.common.credentials import Credential
        from mop.server.access.models import Grant
        from mop.server.controller.credentials import CredentialApplication
        from mop.server.controller.models import Installation, RestoreGuard
        from mop.server.operations import models as om
        from mop.server.operations.service import _binding
        from mop.server.sessions.models import Assignment
        operation, target, _ = self.launch()
        self.ops.activations.activator = self.activator()
        self.through_readiness(operation)
        self.ops.activations.run(operation)
        self.source = Assignment.objects.get(pk=target)
        self.source_grant = Grant.objects.select_related('intent').get(pk=self.source.grant_id)
        self.assertEqual(self.source_grant.state, 'active')
        installation = Installation.objects.get(pk=1)
        self.application = CredentialApplication(self.grants.secrets.provider,
            dict(identity=installation.identity, epoch=str(installation.epoch), root_digest=installation.root_digest,
                 effects_enabled=installation.effects_enabled), str(RestoreGuard.objects.get(pk=1).generation))
        self.auth = Credential('token', self.key)
        self.operation = om.Operation.objects.create(actor=self.user, actor_binding=_binding(self.actor),
            request_id=uuid4(), digest='b'*64, epoch=self.epoch,
            restore_generation=RestoreGuard.objects.get(pk=1).generation, action='update', state='running',
            node=self.node, target_kind='puppet', target_id=self.source.id, incarnation_id=uuid4(),
            expected_revision=self.source.revision+1, not_after=timezone.now()+timedelta(minutes=2))
        self.operation.projects.set((self.project.pk,))
        self.new_grant_id = uuid4()
        om.RestartBinding.objects.create(operation=self.operation, project=self.project, executor_epoch=self.boot,
            owner_id=self.user.pk, generation=self.source.generation+1, role=self.source.role,
            bundle_digest=self.source.bundle_digest, grant_id=self.new_grant_id)
        intent = self.source_grant.intent
        binding_model = apps.get_model('operations', 'ReplacementBinding')
        self.binding = binding_model.objects.create(operation=self.operation,
            source_incarnation_id=self.source.current_incarnation_id, source_grant_id=self.source.grant_id,
            source_revision=self.source.revision, source_generation=self.source.generation,
            source_expires_at=self.source.expires_at, expires_at=min(self.source.expires_at, intent.expires_at),
            definition_id=intent.definition_id, policy_approval_id=intent.policy_approval_id,
            policy_version_id=intent.policy_version_id, policy_digest=intent.policy_digest)
        for ordinal, name in enumerate(('prepare_launch', 'scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant'), 1):
            values = dict(operation=self.operation, ordinal=ordinal, name=name)
            if ordinal == 1:
                token = uuid4()
                values.update(token=token, attempt_id=token, started_at=timezone.now(), dispatched_at=timezone.now(),
                              lease_until=timezone.now()+timedelta(seconds=30))
            step = om.Step.objects.create(**values)
            if ordinal == 1:
                self.preparation_step = step
        for key in ('node:'+str(self.node.pk), 'project:'+str(self.project.pk),
                    'puppet:'+str(self.source.pk), 'target:'+str(self.source.pk)):
            om.Resource.objects.get_or_create(key=key)
            om.ResourceClaim.objects.create(resource_id=key, operation=self.operation,
                step=self.preparation_step, token=self.preparation_step.token)
        return self.operation

    def prepare(self, *, credential=None):
        from mop.common.credentials import parse_request
        auth = credential or self.auth
        _, command, args = parse_request(dict(version=1, auth=dict(kind=auth.kind, value=auth.value),
            command='prepare_replacement', args=dict(operation_id=str(self.operation.pk))))
        return self.application.execute(auth, command, args)

    def test_credential_protocol_accepts_only_a_canonical_operation_locator(self):
        from mop.common.credentials import parse_request, CredentialError
        auth = dict(kind='token', value='a'*40)
        value = dict(version=1, auth=auth, command='prepare_replacement', args=dict(operation_id=str(uuid4())))
        _, _, args = parse_request(value)
        self.assertEqual(set(args), {'operation_id'})
        for extra in ('source_grant_id', 'grant_id', 'policy_version_id', 'incarnation', 'expires_at', 'force'):
            with self.subTest(extra=extra), self.assertRaises(CredentialError):
                parse_request({**value, 'args': {**value['args'], extra: str(uuid4())}})

    def test_preparation_preserves_exact_policy_actor_token_and_current_source(self):
        from rest_framework.authtoken.models import Token
        from mop.server.access import models as am
        from mop.server.sessions.models import Assignment
        self.fixture()
        before = Assignment.objects.values().get(pk=self.source.pk)
        subject = self.source_grant.subject_id
        token = Token.objects.get(user_id=subject).key
        credentials = tuple(am.CredentialBinding.objects.filter(token_user_id=subject).values_list('id', flat=True))
        # Новый latest не вправе подменять policy source гранта.
        newer = self.preparation.publish(self.actor, project_id=self.project.pk, tasks=b'- debug: msg=new\n',
            variables=b'answer: 43\n', provenance='git:replacement', commit='c'*40, secret_refs=())
        self.preparation.approve(self.actor, newer.id)
        result = self.prepare()
        row = am.Grant.objects.select_related('intent').get(pk=result['id'])
        self.assertEqual((row.id, row.subject_id, row.state), (self.new_grant_id, subject, 'reserved'))
        self.assertIsNotNone(row.credential_version_id)
        self.assertNotEqual(row.id, self.source.grant_id)
        self.assertEqual((row.intent.incarnation, row.intent.target_id),
                         (self.operation.incarnation_id, self.source.pk))
        self.assertEqual((row.intent.policy_approval_id, row.intent.policy_version_id, row.intent.policy_digest),
                         (self.binding.policy_approval_id, self.binding.policy_version_id, self.binding.policy_digest))
        self.assertEqual(row.intent.expires_at, self.binding.expires_at)
        self.assertEqual(row.intent.definition_id, self.source_grant.intent.definition_id)
        self.assertEqual(Assignment.objects.values().get(pk=self.source.pk), before)
        self.assertEqual(Token.objects.get(user_id=subject).key, token)
        self.assertEqual(tuple(am.CredentialBinding.objects.filter(token_user_id=subject).values_list('id', flat=True)), credentials)
        self.assertEqual(am.AgentProfile.objects.get(target_id=self.source.pk).agent_id, subject)
        self.assertEqual(self.prepare(), result, 'same claimed preparation must not rotate or re-provision')

    def test_generic_provision_does_not_gain_the_prospective_binding_exception(self):
        self.fixture()
        self.prepare()
        from mop.server.access.models import Grant
        row = Grant.objects.get(pk=self.new_grant_id)
        # Удалять retained credential ради теста не нужно: ordinary gate
        # проверяется отдельно на том же prospective scope.
        from mop.server.secrets.services import RetentionScope
        with self.assertRaisesRegex(PermissionError, 'binding changed'):
            self.grants.secrets.authority.check(self.actor,
                RetentionScope(self.project.pk, self.user.pk, row.id, 'grant'))

    def test_missing_issue_or_create_or_update_right_denies_before_grant_or_crypto(self):
        from unittest.mock import patch
        from _grant_cases import permit_actions
        from mop.server.access import models as am
        self.fixture()
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        am.ProjectMembership.objects.get_or_create(user=self.user, project=self.project,
            source=am.IdentitySource.objects.get(name='local'))
        actions = ('operations.view_operation', 'access.view_role', 'preparation.approve_bootstrap',
                   'access.issue_grant', 'execution.create_puppet', 'execution.update_puppet')
        for missing in ('access.issue_grant', 'execution.create_puppet', 'execution.update_puppet'):
            self.user.user_permissions.clear()
            permit_actions(self.user, *(action for action in actions if action != missing))
            with self.subTest(missing=missing), patch.object(type(self.grants.secrets.provider), 'encrypt',
                    side_effect=AssertionError('crypto before explicit rights')), self.assertRaises(PermissionError):
                self.prepare()
            self.assertFalse(am.Grant.objects.filter(pk=self.new_grant_id).exists())

    def test_foreign_actor_machine_or_missing_claim_cannot_prepare(self):
        from mop.common.credentials import Credential
        from mop.server.access import services as auth
        from mop.server.operations.models import ResourceClaim
        self.fixture()
        machine = Credential('token', auth.enroll_service('not-a-human', self.node.pk, project_ids=(self.project.pk,),
            permissions=('execution.poll_work',)))
        with self.assertRaises(PermissionError):
            self.prepare(credential=machine)
        ResourceClaim.objects.filter(operation=self.operation, resource_id='target:'+str(self.source.pk)).update(
            released_at=timezone.now())
        with self.assertRaises(PermissionError):
            self.prepare()

    def test_expired_or_completed_prepare_stage_has_no_authority(self):
        from mop.server.operations.models import Step
        self.fixture()
        Step.objects.filter(pk=self.preparation_step.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(PermissionError):
            self.prepare()
        Step.objects.filter(pk=self.preparation_step.pk).update(lease_until=timezone.now()+timedelta(seconds=30),
            outcome='succeeded', quiescent_at=timezone.now())
        with self.assertRaises(PermissionError):
            self.prepare()

    def test_provider_wait_rechecks_source_and_never_commits_a_stale_credential(self):
        from unittest.mock import patch
        from mop.server.sessions.models import Assignment
        from mop.server.access.models import Grant
        self.fixture()
        provider = self.grants.secrets.provider
        original = type(provider).encrypt
        def change(instance, *args, **kwargs):
            encrypted = original(instance, *args, **kwargs)
            Assignment.objects.filter(pk=self.source.pk).update(revision=self.source.revision+1)
            return encrypted
        with patch.object(type(provider), 'encrypt', change), self.assertRaises(PermissionError):
            self.prepare()
        row = Grant.objects.get(pk=self.new_grant_id)
        self.assertEqual(row.state, 'reserved')
        self.assertIsNone(row.credential_version_id)

    def test_exact_pinned_approval_revocation_is_not_replaced_by_latest(self):
        from mop.server.access.models import Grant
        self.fixture()
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        with self.assertRaisesRegex(PermissionError, 'revoked'):
            self.prepare()
        self.assertFalse(Grant.objects.filter(pk=self.new_grant_id).exists())
