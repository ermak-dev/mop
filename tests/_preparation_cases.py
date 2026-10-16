"""Bootstrap #636: реальные ORM-байты, approval, fencing и restart readback."""
from dataclasses import replace
from datetime import timedelta
import importlib
import importlib.util
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from mop.server.access import services as auth, models as access
from mop.server.access.delegation import PolicyBinding
from mop.server.catalog.models import Project
from mop.server.controller.runtime import initialize
from mop.server.preparation import models as m
from mop.server.secrets.crypto import Keyring, XChaCha20Provider
from mop.server.secrets.services import SecretStore

# HYPOTHESIS: изменяемая ORM-строка может подменить одобренные bytes и старый
# результат может подтвердить другое incarnation/version без адресного readback.
# SOLUTION: immutable encrypted revisions/approval, CAS run/step и локальные ports.
# RED: записывается после настоящего PG-прогона, до реализации services/guards.


class PreparationFixture(TransactionTestCase):
    def setUp(self):
        initialize()
        self.project = Project.objects.create(name='alpha', origin='https://example.invalid/alpha.git')
        self.foreign = Project.objects.create(name='beta', origin='https://example.invalid/beta.git')
        self.user = get_user_model().objects.create_user('bootstrap-owner', password='test-only-password')
        auth.bind_local_identity(self.user.pk)
        source = access.IdentitySource.objects.get(name='local')
        access.ProjectMembership.objects.create(user=self.user, project=self.project, source=source)
        for action in ('preparation.approve_bootstrap', 'catalog.view_project', 'secrets.manage_project_secret'):
            app, code = action.split('.')
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        self.actor = auth.token_context(auth.issue_token(self.user.pk))
        self.provider = XChaCha20Provider(Keyring('test', {'test': b'p' * 32}))

    def bare_revision(self):
        secret = SecretStore(provider=self.provider, authorizer=auth.Access()).create(self.actor,
            project_id=self.project.pk, name='schema-fixture', kind='credential', purpose='policy', value=b'fixture-only')
        return m.BootstrapRevision.objects.create(project=self.project, owner=self.user, number=1,
            content_version_id=secret.active_version_id, digest='a' * 64,
            provenance='fixture', commit='a' * 40, task_size=1, vars_size=1)


class PreparationSchema(PreparationFixture):
    def test_approved_revision_cannot_be_rewritten_through_queryset(self):
        revision = self.bare_revision()
        m.BootstrapApproval.objects.create(revision=revision, human=self.user)
        with self.assertRaises(ValueError, msg='approved task digest must remain immutable'):
            m.BootstrapRevision.objects.filter(pk=revision.pk).update(digest='b' * 64)
        self.assertEqual(m.BootstrapRevision.objects.get(pk=revision.pk).digest, 'a' * 64)

    def test_approval_cannot_be_repointed_saved_deleted_or_bulk_changed(self):
        revision = self.bare_revision()
        approval = m.BootstrapApproval.objects.create(revision=revision, human=self.user)
        for operation in (lambda: approval.save(), lambda: approval.delete(),
                          lambda: m.BootstrapApproval.objects.filter(pk=approval.pk).delete(),
                          lambda: m.BootstrapApproval.objects.filter(pk=approval.pk).update(human=self.user),
                          lambda: m.BootstrapApproval.objects.bulk_update([approval], ['human'])):
            with self.assertRaises(ValueError):
                operation()


class Gate:
    """Тестовый assigned-target порт; никаких эффектов или доверия target JSON."""
    def __init__(self):
        self.allowed, self.calls = True, []

    def check(self, actor, binding, action):
        self.calls.append((binding, action, connection.in_atomic_block))
        if not self.allowed:
            raise PermissionError('runtime binding is unavailable')


class Redactor:
    def sanitize(self, raw):
        if connection.in_atomic_block:
            raise AssertionError('redactor called inside SQL')
        return raw.replace(b'private-token', b'[redacted]').decode('utf-8', errors='replace')


class PreparationCases(PreparationFixture):
    def setUp(self):
        super().setUp()
        self.assertIsNotNone(importlib.util.find_spec('mop.server.preparation.services'),
                             'bootstrap data/approval application service is missing')
        self.s = importlib.import_module('mop.server.preparation.services')
        self.gate = Gate()
        self.api = self.s.Preparation(provider=self.provider, authorizer=auth.Access(), gate=self.gate, redactor=Redactor())
        self.target, self.incarnation = uuid.uuid4(), uuid.uuid4()

    def publish(self, **kw):
        args = dict(project_id=self.project.pk, tasks=b'- debug: msg=hello\r\n', variables=b'answer: 42\n',
                    provenance='git:example.invalid/alpha', commit='a' * 40, secret_refs=())
        args.update(kw)
        return self.api.publish(self.actor, **args)

    def approved(self, **kw):
        revision = self.publish(**kw)
        return revision, self.api.approve(self.actor, revision.id)

    def run(self, approval=None, **kw):
        if approval is None:
            _, approval = self.approved()
        args = dict(approval_id=approval.id, target_id=self.target, incarnation=self.incarnation,
                    request_id=uuid.uuid4(), deadline=timezone.now() + timedelta(minutes=10),
                    steps=('prepare', 'apply'), failure_limit=3)
        args.update(kw)
        return self.api.create_run(self.actor, **args)

    def begin(self, run, ordinal=0):
        return self.api.begin_step(self.actor, run.binding, ordinal, expected_version=run.version)

    def result(self, run, ordinal=0, outcome='succeeded', **kw):
        return self.api.record_step(self.actor, run.binding, ordinal, outcome=outcome,
                                    expected_version=run.version, **kw)

    def success(self, run):
        for ordinal in (0, 1):
            run = self.result(self.begin(run, ordinal), ordinal)
        return self.api.finish(self.actor, run.binding, expected_version=run.version)

    def test_exact_bytes_digest_provenance_and_restart_without_author_client(self):
        revision, approval = self.approved()
        self.assertNotIn('answer: 42', repr(revision))
        read = self.api.read_revision(self.actor, revision.id)
        self.assertEqual(read.tasks, b'- debug: msg=hello\r\n')
        self.assertEqual(read.variables, b'answer: 42\n')
        self.assertEqual((read.provenance, read.commit), ('git:example.invalid/alpha', 'a' * 40))
        again = self.s.Preparation(provider=self.provider, authorizer=auth.Access(), gate=self.gate)
        self.assertEqual(again.read_revision(self.actor, revision.id), read)
        changed = self.publish(tasks=read.tasks + b'\n')
        self.assertNotEqual(changed.digest, revision.digest)
        self.assertNotEqual(changed.content_version_id, revision.content_version_id)
        self.assertEqual(self.api.approve(self.actor, revision.id), approval)

    def test_default_auth_and_delivery_gate_are_closed(self):
        with self.assertRaises(PermissionError):
            self.s.Preparation(provider=self.provider).publish(self.actor, project_id=self.project.pk,
                tasks=b'a', variables=b'b', provenance='source', commit='a' * 40)
        revision, approval = self.approved()
        with self.assertRaises(PermissionError):
            self.s.Preparation(provider=self.provider, authorizer=auth.Access()).create_run(self.actor,
                approval_id=approval.id, target_id=self.target, incarnation=self.incarnation,
                request_id=uuid.uuid4(), deadline=timezone.now() + timedelta(minutes=1), steps=('one',))
        with self.assertRaises(PermissionError):
            self.publish(project_id=self.foreign.pk)

    def test_content_bounds_types_provenance_and_secret_reference_scope(self):
        for kw in ({'tasks': b'x' * 262145}, {'variables': b'x' * 131073}, {'tasks': 'not bytes'},
                   {'provenance': ''}, {'commit': 'branch-not-commit'}, {'secret_refs': (uuid.uuid4(),)}):
            with self.assertRaises((ValueError, PermissionError)):
                self.publish(**kw)
        self.assertEqual(m.BootstrapRevision.objects.count(), 0)

    def test_new_revision_or_incarnation_does_not_inherit_old_success(self):
        revision, approval = self.approved()
        run = self.success(self.run(approval))
        self.assertEqual(run.state, 'succeeded')
        self.assertTrue(self.api.readback(self.actor, run.binding).confirmed)
        other, other_approval = self.approved(tasks=b'new bytes')
        fresh = self.run(other_approval)
        self.assertFalse(self.api.readback(self.actor, fresh.binding).confirmed)
        self.assertFalse(self.api.readback(self.actor, replace(run.binding, revision_id=other.id)).confirmed)
        self.assertFalse(self.api.readback(self.actor, replace(run.binding, incarnation=uuid.uuid4())).confirmed)
        self.assertFalse(self.api.readback(self.actor, replace(run.binding, run_id=uuid.uuid4())).confirmed)
        self.assertNotEqual(other.id, revision.id)

    def test_partial_or_foreign_run_and_stale_versions_cannot_record(self):
        run = self.run()
        with self.assertRaises(self.s.Conflict):
            self.api.finish(self.actor, run.binding, expected_version=run.version)
        running = self.begin(run)
        with self.assertRaises(self.s.Conflict):
            self.result(run)
        with self.assertRaises((ValueError, self.s.Conflict)):
            self.result(replace(running, binding=replace(run.binding, incarnation=uuid.uuid4())))
        with self.assertRaises((ValueError, self.s.Conflict)):
            self.result(running, ordinal=1)
        with self.assertRaises(ValueError):
            self.api.readback(self.actor, str(run.id))
        self.assertEqual(self.api.get_run(self.actor, run.id).version, running.version)

    def test_retry_failure_counters_and_gave_up_are_durable(self):
        run = self.run(failure_limit=2)
        run = self.result(self.begin(run), outcome='failed', log=b'private-token failure')
        self.assertEqual((run.failures, run.state), (1, 'running'))
        run = self.result(self.begin(run), outcome='failed')
        self.assertEqual((run.failures, run.state), (2, 'gave_up'))
        self.assertIsNotNone(run.finished_at)
        with self.assertRaises(self.s.Conflict):
            self.begin(run)
        again = self.s.Preparation(provider=self.provider, authorizer=auth.Access(), gate=self.gate)
        self.assertEqual(again.get_run(self.actor, run.id), run)
        step = again.steps(self.actor, run.binding)[0]
        self.assertEqual((step.failures, step.attempt), (2, 2))

    def test_lost_reply_is_readback_not_automatic_repeat_of_unknown_effect(self):
        run = self.begin(self.run())
        run = self.api.mark_unknown(self.actor, run.binding, expected_version=run.version)
        self.assertEqual(run.state, 'unknown')
        with self.assertRaises(self.s.Conflict):
            self.begin(run)
        self.assertFalse(self.api.readback(self.actor, run.binding).confirmed)
        with self.assertRaises(self.s.Conflict):
            self.api.finish(self.actor, run.binding, expected_version=run.version)

    def test_expired_running_step_becomes_unknown_not_failed_or_success(self):
        run = self.begin(self.run())
        m.BootstrapRun.objects.filter(pk=run.id).update(deadline=timezone.now() - timedelta(seconds=1))
        read = self.api.readback(self.actor, run.binding)
        self.assertEqual(read.run.state, 'unknown')
        self.assertEqual(read.run.failures, 0)
        self.assertFalse(read.confirmed)

    def test_refusal_is_not_failure_or_automatic_retry(self):
        run = self.result(self.begin(self.run()), outcome='refused')
        self.assertEqual((run.state, run.failures), ('refused', 0))
        with self.assertRaises(self.s.Conflict):
            self.begin(run)

    def test_revocation_after_long_effect_prevents_readback_and_payload(self):
        revision, approval = self.approved()
        run = self.success(self.run(approval))
        self.api.revoke(self.actor, approval.id)
        with self.assertRaises(PermissionError):
            self.api.payload(self.actor, run.binding)
        with self.assertRaises(PermissionError):
            self.api.readback(self.actor, run.binding)
        self.assertEqual(m.BootstrapApproval.objects.get(pk=approval.id).revision_id, revision.id)

    def test_current_authority_rechecked_before_returning_private_result(self):
        run = self.success(self.run())
        self.gate.allowed = False
        with self.assertRaises(PermissionError):
            self.api.readback(self.actor, run.binding)
        self.gate.allowed = True
        self.user.user_permissions.clear()
        with self.assertRaises(PermissionError):
            self.api.payload(self.actor, run.binding)

    def test_sanitized_log_retention_and_progress_are_bounded_private(self):
        run = self.begin(self.run())
        run = self.api.progress(self.actor, run.binding, 0, percent=75,
            log=b'private-token\x1b[31m\n' + b'z' * 6000, expected_version=run.version)
        step = self.api.steps(self.actor, run.binding)[0]
        self.assertLessEqual(len(step.log.encode()), 4096)
        self.assertNotIn('private-token', step.log)
        self.assertNotIn('\x1b', step.log)
        self.assertEqual(step.progress, 75)
        self.assertNotIn(step.log, repr(step))
        with self.assertRaises(ValueError):
            self.api.progress(self.actor, run.binding, 0, percent=101, expected_version=run.version)
        with self.assertRaises(PermissionError):
            self.s.Preparation(provider=self.provider, authorizer=auth.Access(), gate=self.gate).progress(
                self.actor, run.binding, 0, percent=80, log=b'unreviewed stdout', expected_version=run.version)

    def test_private_provider_and_redaction_never_run_inside_atomic(self):
        revision, approval = self.approved()
        with transaction.atomic():
            with self.assertRaises(ValueError):
                self.api.read_revision(self.actor, revision.id)
            with self.assertRaises(ValueError):
                self.publish()
        run = self.begin(self.run(approval))
        with transaction.atomic():
            with self.assertRaises(ValueError):
                self.api.progress(self.actor, run.binding, 0, percent=10, log=b'output', expected_version=run.version)

    def test_replay_same_request_preserves_approval_and_payload_conflicts(self):
        _, approval = self.approved()
        request = uuid.uuid4()
        deadline = timezone.now() + timedelta(minutes=5)
        run = self.run(approval, request_id=request, deadline=deadline)
        self.assertEqual(self.run(approval, request_id=request, deadline=deadline), run)
        _, new = self.approved(tasks=b'new')
        with self.assertRaises(self.s.Conflict):
            self.run(new, request_id=request, deadline=deadline)
        self.assertEqual(m.BootstrapRun.objects.count(), 1)

    def test_grants_approval_query_pins_exact_revision_and_validates_old_not_latest(self):
        revision, approval = self.approved()
        query = self.api.approval_query()
        binding = PolicyBinding(uuid.uuid4(), self.user.pk, self.actor.principal_id, self.project.pk,
                                uuid.uuid4(), 'worker', 'b' * 64, None, self.target, self.incarnation)
        proof = query.pin(binding)
        self.assertEqual((proof.approval_id, proof.version_id, proof.digest),
                         (approval.id, revision.content_version_id, revision.digest))
        newer, _ = self.approved(tasks=b'new human policy')
        self.assertNotEqual(query.pin(binding).digest, proof.digest)
        self.assertIsNone(query.validate(proof, binding))
        with self.assertRaises(PermissionError):
            query.validate(proof, replace(binding, owner_id=self.user.pk + 1))
        with self.assertRaises(PermissionError):
            query.validate(replace(proof, digest='c' * 64), binding)
        self.api.revoke(self.actor, approval.id)
        with self.assertRaises(PermissionError):
            query.validate(proof, binding)
