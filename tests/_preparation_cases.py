"""Bootstrap #636: реальные ORM-байты, approval, fencing и restart readback."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
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
# RED: PG16, test-only 1db27e30: ValueError not raised при переписывании
# approved digest и approval. Import/setup/RestoreGuard ошибки red не считались.
# Дополнительный red: PG16 — raw redactor exception, revoked receipt и
# unknown -> refused; PG17 — missing step подтверждал success, старый approval
# сбрасывал gave_up после прогона другой revision.
# RESULT: immutable encrypted bytes и SQL-only approval port; полный ordinal
# plan проверяется на readback, budget адресован approval/target/incarnation.
# STATUS: FIXED — see #636. Runtime/node cutover и live run — зависимые задачи.


class PreparationFixture(TransactionTestCase):
    # После flush восстанавливаем migration seeds (RestoreGuard), не создаём
    # runtime repair/fallback и не трогаем restore fencing в production.
    serialized_rollback = True

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

    def new_run(self, approval=None, **kw):
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

    def test_restart_readback_and_bytes_from_an_independent_process(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        from rest_framework.authtoken.models import Token
        run = self.success(self.new_run())
        code = '''
import json, os, sys, uuid
sys.path.insert(0, sys.argv[1])
from mop.server.controller.runtime import configure
configure()
from django.db import connection, connections
connections.databases['default']['NAME'] = sys.argv[2]
from mop.server.access import services as auth
from mop.server.preparation.services import Preparation
from mop.server.secrets.crypto import Keyring, XChaCha20Provider
data = json.loads(sys.stdin.read())
class Gate:
    def check(self, actor, binding, action):
        if str(binding.run_id) != data['run'] or str(binding.incarnation) != data['incarnation']:
            raise PermissionError('restart fixture binding mismatch')
try:
    actor = auth.token_context(data['token'])
    api = Preparation(provider=XChaCha20Provider(Keyring('test', {'test': b'p' * 32})),
                      authorizer=auth.Access(), gate=Gate())
    info = api.get_run(actor, uuid.UUID(data['run']))
    readback = api.readback(actor, info.binding)
    content = api.payload(actor, info.binding)
    print(json.dumps({'pid': os.getpid(), 'state': info.state, 'version': info.version,
                      'confirmed': readback.confirmed, 'tasks': content.tasks.decode(),
                      'variables': content.variables.decode()}))
finally:
    connections.close_all()
'''
        root = str(Path(__file__).resolve().parents[1])
        child = subprocess.run([sys.executable, '-I', '-c', code, root, connection.settings_dict['NAME']],
            input=json.dumps({'run': str(run.id), 'incarnation': str(self.incarnation),
                              'token': Token.objects.get(user=self.user).key}),
            capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        import os
        self.assertNotEqual(result['pid'], os.getpid())
        self.assertEqual((result['state'], result['version'], result['confirmed']), ('succeeded', run.version, True))
        self.assertEqual(result['tasks'], '- debug: msg=hello\r\n')
        self.assertEqual(result['variables'], 'answer: 42\n')

    def test_schema_is_repeatable_and_protected_bytes_are_not_plaintext_columns(self):
        from django.core.management import call_command
        from mop.server.secrets.models import SecretVersion
        call_command('migrate', verbosity=0, interactive=False)
        call_command('makemigrations', check=True, dry_run=True, verbosity=0)
        revision, _ = self.approved()
        version = SecretVersion.objects.get(pk=revision.content_version_id)
        self.assertNotIn(b'answer: 42', bytes(version.envelope))
        self.assertNotIn(b'debug: msg=hello', bytes(version.envelope))
        for provider in (None, XChaCha20Provider(Keyring('test', {'test': b'w' * 32}))):
            with self.assertRaises(ValueError):
                self.s.Preparation(provider=provider, authorizer=auth.Access()).read_revision(self.actor, revision.id)

    def test_malformed_collections_and_redactor_exceptions_are_safe(self):
        with self.assertRaises(ValueError):
            self.publish(secret_refs=({},))
        with self.assertRaises(ValueError):
            self.new_run(steps=([],))
    def test_redactor_exception_cannot_expose_raw_output_or_update_progress(self):
        class Broken:
            def sanitize(inner, raw):
                raise RuntimeError('private-token in untrusted redactor exception')
        run = self.begin(self.new_run())
        self.api.redactor = Broken()
        with self.assertRaises(ValueError) as raised:
            self.api.progress(self.actor, run.binding, 0, percent=10,
                              log=b'private-token', expected_version=run.version)
        self.assertNotIn('private-token', str(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(self.api.get_run(self.actor, run.id).version, run.version)

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

    def test_secret_references_are_relational_pinned_scoped_and_never_values(self):
        store = SecretStore(provider=self.provider, authorizer=auth.Access())
        secret = store.create(self.actor, project_id=self.project.pk, name='BOOTSTRAP_SECRET',
                              kind='variable', purpose='project', value=b'protected-variable')
        revision, approval = self.approved(secret_refs=(secret.active_version_id,))
        self.assertEqual(self.api.read_revision(self.actor, revision.id).secret_refs, (secret.active_version_id,))
        self.assertEqual(m.BootstrapSecretReference.objects.get(revision_id=revision.id).version_id, secret.active_version_id)
        store.revoke(self.actor, secret.id, expected_revision=secret.revision)
        with self.assertRaises((ValueError, PermissionError)):
            self.api.payload(self.actor, self.new_run(approval).binding)

    def test_unavailable_host_preserves_failure_budget_without_claiming_unknown_success(self):
        run = self.result(self.begin(self.new_run()), outcome='unavailable')
        self.assertEqual((run.state, run.failures), ('failed', 0))
        self.assertFalse(self.api.readback(self.actor, run.binding).confirmed)

    def test_new_revision_or_incarnation_does_not_inherit_old_success(self):
        revision, approval = self.approved()
        run = self.success(self.new_run(approval))
        self.assertEqual(run.state, 'succeeded')
        self.assertTrue(self.api.readback(self.actor, run.binding).confirmed)
        other, other_approval = self.approved(tasks=b'new bytes')
        fresh = self.new_run(other_approval)
        self.assertFalse(self.api.readback(self.actor, fresh.binding).confirmed)
        self.assertFalse(self.api.readback(self.actor, replace(run.binding, revision_id=other.id)).confirmed)
        self.assertFalse(self.api.readback(self.actor, replace(run.binding, incarnation=uuid.uuid4())).confirmed)
        self.assertFalse(self.api.readback(self.actor, replace(run.binding, run_id=uuid.uuid4())).confirmed)
        self.assertNotEqual(other.id, revision.id)

    def test_partial_or_foreign_run_and_stale_versions_cannot_record(self):
        run = self.new_run()
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
        run = self.new_run(failure_limit=2)
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

    def test_new_request_cannot_reset_gave_up_or_uncertain_target(self):
        run = self.result(self.begin(self.new_run(failure_limit=1)), outcome='failed')
        with self.assertRaises(self.s.Conflict):
            self.new_run(approval=self.api.get_approval(self.actor, run.binding.approval_id))
        other, approval = self.approved(tasks=b'new reviewed policy')
        fresh = self.begin(self.new_run(approval))
        fresh = self.api.mark_unknown(self.actor, fresh.binding, expected_version=fresh.version)
        with self.assertRaises(self.s.Conflict):
            self.new_run(approval)
        self.assertEqual(m.BootstrapRun.objects.count(), 2)

    def test_older_approval_cannot_reset_budget_after_a_different_revision_run(self):
        _, approval = self.approved()
        gave_up = self.result(self.begin(self.new_run(approval, failure_limit=1)), outcome='failed')
        self.assertEqual(gave_up.state, 'gave_up')
        _, changed = self.approved(tasks=b'replacement')
        self.success(self.new_run(changed))
        with self.assertRaises(self.s.Conflict):
            self.new_run(approval, failure_limit=1)

    def test_missing_required_step_cannot_confirm_a_successful_run(self):
        run = self.success(self.new_run())
        m.BootstrapStep.objects.filter(run_id=run.id, ordinal=1).delete()
        self.assertFalse(self.api.readback(self.actor, run.binding).confirmed)

    def test_empty_steps_and_oversized_raw_output_are_rejected_without_mutation(self):
        for steps in ((), ('duplicate', 'duplicate'), ('x',) * 65):
            with self.assertRaises(ValueError):
                self.new_run(steps=steps)
        run = self.begin(self.new_run())
        with self.assertRaises(ValueError):
            self.api.progress(self.actor, run.binding, 0, percent=1, log=b'x' * 65537,
                              expected_version=run.version)
        self.assertEqual(self.api.get_run(self.actor, run.id).version, run.version)

    def test_lost_reply_is_readback_not_automatic_repeat_of_unknown_effect(self):
        run = self.begin(self.new_run())
        run = self.api.mark_unknown(self.actor, run.binding, expected_version=run.version)
        self.assertEqual(run.state, 'unknown')
        with self.assertRaises(self.s.Conflict):
            self.begin(run)
        self.assertFalse(self.api.readback(self.actor, run.binding).confirmed)
        with self.assertRaises(self.s.Conflict):
            self.api.finish(self.actor, run.binding, expected_version=run.version)

    def test_expired_running_step_becomes_unknown_not_failed_or_success(self):
        run = self.begin(self.new_run())
        m.BootstrapRun.objects.filter(pk=run.id).update(deadline=timezone.now() - timedelta(seconds=1))
        read = self.api.readback(self.actor, run.binding)
        self.assertEqual(read.run.state, 'unknown')
        self.assertEqual(read.run.failures, 0)
        self.assertFalse(read.confirmed)

    def test_step_claim_cas_has_one_winner_on_independent_connections(self):
        from django.db import connections
        run = self.new_run()
        barrier = Barrier(2)
        def claim(_):
            try:
                barrier.wait(timeout=20)
                return self.begin(run).version
            except self.s.Conflict:
                return 'conflict'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, (0, 1)))
        self.assertEqual(outcomes.count('conflict'), 1)
        self.assertIn(2, outcomes)
        self.assertEqual(self.api.steps(self.actor, run.binding)[0].attempt, 1)

    def test_refusal_is_not_failure_or_automatic_retry(self):
        run = self.result(self.begin(self.new_run()), outcome='refused')
        self.assertEqual((run.state, run.failures), ('refused', 0))
        with self.assertRaises(self.s.Conflict):
            self.begin(run)

    def test_revocation_after_long_effect_prevents_readback_and_payload(self):
        revision, approval = self.approved()
        run = self.success(self.new_run(approval))
        self.api.revoke(self.actor, approval.id)
        with self.assertRaises(PermissionError):
            self.api.payload(self.actor, run.binding)
        with self.assertRaises(PermissionError):
            self.api.readback(self.actor, run.binding)
        self.assertEqual(m.BootstrapApproval.objects.get(pk=approval.id).revision_id, revision.id)

    def test_result_after_approval_revocation_is_retained_as_refused_not_success(self):
        _, approval = self.approved()
        run = self.begin(self.new_run(approval))
        self.api.revoke(self.actor, approval.id)
        # Текущий authenticated receipt writer остаётся владельцем exact run;
        # отзыв запрещает новые эффекты/выдачу, но не стирает факт их завершения.
        run = self.result(run)
        self.assertEqual(run.state, 'refused')
        self.assertIn('revoked', run.reason)
        self.assertEqual(m.BootstrapStep.objects.get(run_id=run.id, ordinal=0).state, 'succeeded')
        with self.assertRaises(PermissionError):
            self.api.payload(self.actor, run.binding)

    def test_revocation_does_not_turn_unknown_effect_into_retryable_refusal(self):
        _, approval = self.approved()
        run = self.begin(self.new_run(approval))
        self.api.revoke(self.actor, approval.id)
        run = self.result(run, outcome='unknown')
        self.assertEqual(run.state, 'unknown')
        self.assertIsNone(run.finished_at)
        self.assertEqual(run.failures, 0)

    def test_current_authority_rechecked_before_returning_private_result(self):
        run = self.success(self.new_run())
        self.gate.allowed = False
        with self.assertRaises(PermissionError):
            self.api.readback(self.actor, run.binding)
        self.gate.allowed = True
        self.user.user_permissions.clear()
        with self.assertRaises(PermissionError):
            self.api.payload(self.actor, run.binding)

    def test_sanitized_log_retention_and_progress_are_bounded_private(self):
        run = self.begin(self.new_run())
        run = self.api.progress(self.actor, run.binding, 0, percent=75,
            log=b'z' * 6000 + b'private-token\x1b[31m\n', expected_version=run.version)
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
        run = self.begin(self.new_run(approval))
        with transaction.atomic():
            with self.assertRaises(ValueError):
                self.api.progress(self.actor, run.binding, 0, percent=10, log=b'output', expected_version=run.version)

    def test_identical_expired_request_recovers_run_without_effect_admission(self):
        # HYPOTHESIS: admission deadline проверяется до durable request lookup.
        # RED: PG17, 31 green + ValueError bounded future bootstrap deadline required.
        # SOLUTION: существующий intent проверяется с readback authority до admission.
        # RESULT: retry читает run без нового effect; deadline и unknown guards остаются.
        # STATUS: FIXED — see #636.
        from unittest.mock import patch
        _, approval = self.approved()
        request = uuid.uuid4()
        deadline = timezone.now() + timedelta(minutes=5)
        run = self.begin(self.new_run(approval, request_id=request, deadline=deadline))
        run = self.api.mark_unknown(self.actor, run.binding, expected_version=run.version)
        with patch.object(self.s.timezone, 'now', return_value=deadline + timedelta(seconds=1)):
            self.gate.calls.clear()
            self.assertEqual(self.new_run(approval, request_id=request, deadline=deadline), run)
            self.assertEqual([action for _, action, _ in self.gate.calls], ['readback'])
            with self.assertRaises(self.s.Conflict):
                self.begin(run)
            with self.assertRaises(self.s.Conflict):
                self.new_run(approval, request_id=request, deadline=deadline + timedelta(minutes=1))
            with self.assertRaises(ValueError):
                self.new_run(approval, deadline=deadline)
            self.gate.allowed = False
            with self.assertRaises(PermissionError):
                self.new_run(approval, request_id=request, deadline=deadline)
            self.gate.allowed = True
            self.api.revoke(self.actor, approval.id)
            with self.assertRaises(PermissionError):
                self.new_run(approval, request_id=request, deadline=deadline)
        self.assertEqual(m.BootstrapRun.objects.count(), 1)
        self.assertEqual(m.BootstrapRun.objects.get(pk=run.binding.run_id).state, 'unknown')

    def test_replay_same_request_preserves_approval_and_payload_conflicts(self):
        _, approval = self.approved()
        request = uuid.uuid4()
        deadline = timezone.now() + timedelta(minutes=5)
        run = self.new_run(approval, request_id=request, deadline=deadline)
        self.assertEqual(self.new_run(approval, request_id=request, deadline=deadline), run)
        _, new = self.approved(tasks=b'new')
        with self.assertRaises(self.s.Conflict):
            self.new_run(new, request_id=request, deadline=deadline)
        self.assertEqual(m.BootstrapRun.objects.count(), 1)

    def test_real_delegated_child_intent_inherits_first_approval_across_retry(self):
        import json
        from django.contrib.contenttypes.models import ContentType
        from rest_framework.authtoken.models import Token
        from mop.server.access.grants import Grants
        from mop.server.access.delegation import RoleBinding
        revision, approval = self.approved()
        for action in ('access.approve_role', 'access.view_role', 'access.issue_grant'):
            app, code = action.split('.')
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        ct, _ = ContentType.objects.get_or_create(app_label='sessions', model='bootstrap_fixture')
        permission, _ = Permission.objects.get_or_create(content_type=ct, codename='create_puppet')
        self.user.user_permissions.add(permission)
        query = self.api.approval_query()
        class Approvals:
            calls = 0
            def pin(inner, binding):
                inner.calls += 1
                return query.pin(binding)
            def validate(inner, proof, binding):
                return query.validate(proof, binding)
        class Bindings:
            current_value = None
            def current(inner, project_id, target_id):
                return inner.current_value
            def activation(inner, grant_id):
                return inner.current_value
        approvals, bindings = Approvals(), Bindings()
        grants = Grants(provider=self.provider, approvals=approvals, bindings=bindings)
        def bundle(role, spawn):
            raw = json.dumps({'soul': role, 'version': 1, 'runtime': {'driver': 'any', 'receives_dispatch': True},
                'capabilities': {'node_verbs': ['send', 'state'], 'cluster_verbs': ['add', 'roster'],
                                 'dispatch': False, 'spawn': spawn}, 'prompt': 'soul.md'}).encode()
            row = grants.publish_bundle(self.actor, project_id=self.project.pk, manifest=raw,
                                        prompt=b'private prompt', provenance='test source')
            grants.approve_bundle(self.actor, row.id)
            return row
        parent_bundle, child_bundle = bundle('parent', ['child']), bundle('child', [])
        parent = grants.reserve(self.actor, project_id=self.project.pk, bundle_id=parent_bundle.id,
            target_id=uuid.uuid4(), incarnation=uuid.uuid4(), request_id=uuid.uuid4(),
            expires_at=timezone.now() + timedelta(hours=1))
        grants.provision(self.actor, parent.id, expected_revision=parent.revision)
        parent = grants.get(self.actor, parent.id)
        bindings.current_value = RoleBinding(parent.project_id, parent.target_id, parent.incarnation,
            parent.role, parent.digest, parent.id, self.user.pk)
        grants.activate(parent.id, expected_revision=parent.revision)
        key = Token.objects.get(user__principal__grant__id=parent.id).key
        delegated = auth.token_context(key, grants=grants)
        newer, _ = self.approved(tasks=b'new snapshot after first parent intent')
        args = dict(project_id=self.project.pk, bundle_id=child_bundle.id, target_id=uuid.uuid4(),
            incarnation=uuid.uuid4(), request_id=uuid.uuid4(), expires_at=timezone.now() + timedelta(hours=1))
        child = grants.reserve(delegated, **args)
        retry = grants.reserve(delegated, **args)
        self.assertEqual(child, retry)
        self.assertEqual((child.policy.approval_id, child.policy.version_id, child.policy.digest),
                         (approval.id, revision.content_version_id, revision.digest))
        self.assertNotEqual(child.policy.digest, newer.digest)
        self.assertEqual(approvals.calls, 1)
        with self.assertRaises(PermissionError):
            self.api.publish(delegated, project_id=self.project.pk, tasks=b'own policy', variables=b'',
                             provenance='forbidden role upload', commit='b' * 40)

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
        self.user.user_permissions.clear()
        with self.assertRaises(PermissionError):
            query.validate(proof, binding)
        permission = Permission.objects.get(content_type__app_label='preparation', codename='approve_bootstrap')
        self.user.user_permissions.add(permission)
        self.api.revoke(self.actor, approval.id)
        with self.assertRaises(PermissionError):
            query.validate(proof, binding)
