"""Адресная выдача #669 через настоящий socket, Access и публичные owner APIs.

HYPOTHESIS: unbound gate не выдаёт даже точное текущее назначение; одних
SecretBinding, machine token и role metadata недостаточно для безопасной выдачи.
Fixture явно засеивает active grant и подтверждённые machine receipts: это тест
потребления persisted proof, не доказательство настоящего запуска/activation.
RED: PG17 — current assignment/addressed composition is not bound и service
permissions exceed enrollment allowlist. PG16 достиг того же unbound отказа;
в первом таком прогоне затем упал teardown transport deadline, не feature proof.
SOLUTION: request-scoped gate связывает публичные SQL-only Assignment, Grants,
Secrets queries; exact snapshots сравниваются после crypto, не передаются с wire.
RESULT: PG16/17 — 66/66 credential cases, включая отдельный UID, concurrent SQL
fence и native restore bundle/preparation/grant/project values. Широкие subject suites
не объявлены зелёными: прежний counterfeit sessions permission в Preparation и
PG16 teardown deadline/последующие setup ошибки оставлены своим владельцам.
STATUS: FIXED — see #669 (addressed proof; activation/retirement/installation не связаны).
"""
import asyncio
import os
from datetime import timedelta
from uuid import uuid4

from asgiref.sync import async_to_sync
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

import _credential_cases as existing


class AddressedCredentialCases(TransactionTestCase):
    serialized_rollback = True
    setUp = existing.CredentialSocketCases.setUp
    exercise = existing.CredentialSocketCases.exercise
    run_socket = existing.CredentialSocketCases.run_socket
    reserved_grant = existing.CredentialSocketCases.reserved_grant
    grant_assignment = existing.CredentialSocketCases.grant_assignment

    def enroll(self, name, node_id, **kwargs):
        from django.contrib.auth import get_user_model
        token = self.access.enroll_service(name, node_id, **kwargs)
        return get_user_model().objects.get(username=name), token

    def delivery(self, *, receipt_outcome='succeeded'):
        from mop.server.access.models import Grant
        from mop.server.sessions import models as execution
        from mop.server.controller.models import Installation
        # Только собственная test installation: действующий пул не используется.
        Installation.objects.filter(pk=1).update(effects_enabled=True)
        reserved = self.reserved_grant(secret_names=('DELIVERY',))
        self.assignment = self.grant_assignment(reserved)
        with override_settings(BOOTSTRAP=self.bootstrap):
            self.app = self.service.application()
        actor = self.access.token_context(self.token)
        provisioned = self.app.grants.provision(actor, reserved.id, expected_revision=reserved.revision)
        # Только synthetic setup: production activation остаётся отдельным proof.
        Grant._base_manager.filter(pk=reserved.id).update(state='active', revision=provisioned.revision + 1)
        self.grant_id = reserved.id
        for code in ('deliver_secret',):
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label='secrets', codename=code))
        user, token = self.enroll('delivery-node', self.assignment.node_id,
            project_ids=(self.project.id,), permissions=('execution.poll_work',))
        user.user_permissions.add(Permission.objects.get(content_type__app_label='secrets', codename='deliver_secret'))
        self.machine_user = user
        self.machine_auth = self.Auth('token', token)
        self.machine = self.access.token_context(token)
        self.attempts = []
        now = timezone.now()
        for model, action, evidence in ((execution.IdentityApplication, 'apply_identity', 'identity-applied'),
                                        (execution.ReadinessReceipt, 'confirm_ready', 'ready')):
            attempt = execution.AssignmentAttempt.objects.create(assignment=self.assignment,
                incarnation_id=self.assignment.current_incarnation_id, principal_id=self.machine.principal_id,
                request_id=uuid4(), epoch=Installation.objects.get(pk=1).epoch, operation_id=uuid4(), step_id=uuid4(),
                executor_epoch=self.assignment.current_incarnation.executor_epoch, owner=self.user,
                generation=self.assignment.generation, assignment_revision=self.assignment.revision,
                role=reserved.role, bundle_digest=reserved.digest, grant_id=reserved.id, action=action,
                not_after=now + timedelta(minutes=5), lease_until=now + timedelta(minutes=1), state='succeeded', revision=2)
            receipt = execution.ResultReceipt.objects.create(attempt=attempt, expected_revision=1,
                resulting_revision=2, outcome=receipt_outcome, evidence=evidence)
            model.objects.create(attempt=attempt, receipt=receipt)
            self.attempts.append(attempt)
        secret = self.app.store.create(actor, project_id=self.project.id, name='DELIVERY', kind='variable',
            purpose='project', value=b'exact-addressed-value')
        self.secret = secret
        self.binding = self.app.store.bind(actor, secret.id, expected_revision=secret.revision,
            node_id=self.assignment.node_id, role=reserved.role, digest=reserved.digest,
            incarnation=reserved.incarnation, grant_id=reserved.id, assignment_id=self.assignment.id)
        return self.assignment.id

    def issue(self, client, *, auth=None):
        # Первоначальный red использует raw finite wire: missing client method не
        # должен скрыть реальное отсутствие server composition.
        from mop.common import credentials as p
        value = client._invoke(auth or self.machine_auth, p.Command.ADDRESSED, {'target_id': str(self.assignment.id)})
        return tuple(client._result(p.DeliveredSecret, row) for row in value)

    def test_exact_node_assignment_and_sealed_allowlist_deliver_only_selected_values(self):
        target = self.delivery()
        async def body(client, server):
            rows = await asyncio.to_thread(self.issue, client)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].binding_id, rows[0].version_id, rows[0].value),
                (self.binding.id, self.secret.active_version_id, b'exact-addressed-value'))
            self.assertNotIn('exact-addressed-value', repr(rows))
        self.run_socket(body)
        self.assertEqual(target, self.assignment.id)

    def assert_delivery_refused(self, change):
        self.delivery()
        change()
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.issue, client)
        self.run_socket(body)

    # HYPOTHESIS: read-committed projection может смениться между receipt query
    # и выдачей post-provider SQL verdict даже при совпавшей RoleBinding.
    # RED: PG16/17 — TimeoutError not raised: конкурентный UPDATE уже committed.
    # SOLUTION: публичный target(lock=True) держит Assignment до SQL commit;
    # отдельные crypto-wait checks требуют освобождения lock перед provider.
    # RESULT: PG16/17 — lock check и crypto-wait checks green в 66/66.
    # STATUS: FIXED — see #669.
    def test_current_credential_proof_holds_assignment_until_sql_verdict_commits(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        from threading import Event
        from django.db import connections, transaction
        from mop.server.controller.models import Installation
        from mop.server.sessions.models import Assignment
        from mop.server.sessions.services import Assignments
        self.delivery()
        entered = Event()
        def reassign():
            try:
                entered.set()
                return Assignment.objects.filter(pk=self.assignment.id).update(revision=2)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                proof = Assignments().credential_proof(self.machine, self.assignment.id,
                    epoch=Installation.objects.get(pk=1).epoch)
                future = pool.submit(reassign)
                self.assertTrue(entered.wait(2))
                with self.assertRaises(TimeoutError):
                    future.result(.5)
                self.assertEqual(proof.binding.revision, 1)
            self.assertEqual(future.result(3), 1)
        self.assertEqual(Assignment.objects.get(pk=self.assignment.pk).revision, 2)

    def test_service_deliver_permission_is_enrollable_but_not_human_authority(self):
        self.delivery()
        user, token = self.enroll('another-node-token', self.assignment.node_id,
            project_ids=(self.project.id,), permissions=('secrets.deliver_secret',))
        actor = self.access.token_context(token)
        with self.assertRaises(PermissionError):
            self.access.Access().authorize(actor, 'access.issue_grant', project_ids=(self.project.id,),
                node_ids=(self.assignment.node_id,))

    def test_confirmed_effect_receipt_survives_attempt_lease_expiry_without_proving_retirement(self):
        from mop.server.sessions.models import AssignmentAttempt
        self.delivery()
        past = timezone.now() - timedelta(minutes=1)
        AssignmentAttempt.objects.filter(pk__in=[row.pk for row in self.attempts]).update(
            lease_until=past, not_after=past)
        async def body(client, server):
            rows = await asyncio.to_thread(self.issue, client)
            self.assertEqual(rows[0].value, b'exact-addressed-value')
            for query in (lambda: server.application.grants.bindings.activation(self.grant_id),
                          lambda: server.application.grants.bindings.retirement(self.grant_id, 3)):
                with self.assertRaises(PermissionError):
                    await server.runtime.database(query)
        self.run_socket(body)

    def test_missing_ready_receipt_refuses(self):
        from mop.server.sessions.models import AssignmentAttempt
        self.assert_delivery_refused(lambda: AssignmentAttempt.objects.filter(pk=self.attempts[1].pk).update(state='unknown'))

    def test_stale_assignment_revision_receipt_refuses(self):
        from mop.server.sessions.models import AssignmentAttempt
        self.assert_delivery_refused(lambda: AssignmentAttempt.objects.filter(pk=self.attempts[0].pk).update(assignment_revision=9))

    def test_wrong_executor_epoch_receipt_refuses(self):
        from mop.server.sessions.models import AssignmentAttempt
        self.assert_delivery_refused(lambda: AssignmentAttempt.objects.filter(pk=self.attempts[0].pk).update(executor_epoch=uuid4()))

    def test_failed_result_cannot_back_successful_attempt_identity_or_readiness(self):
        self.delivery(receipt_outcome='failed')
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.issue, client)
        self.run_socket(body)

    def test_wrong_action_receipt_refuses(self):
        from mop.server.sessions.models import AssignmentAttempt
        self.assert_delivery_refused(lambda: AssignmentAttempt.objects.filter(pk=self.attempts[0].pk).update(action='send_message'))

    def test_wrong_owner_receipt_refuses(self):
        from django.contrib.auth import get_user_model
        from mop.server.sessions.models import AssignmentAttempt
        self.assert_delivery_refused(lambda: AssignmentAttempt.objects.filter(pk=self.attempts[0].pk).update(
            owner=get_user_model().objects.create_user('other-receipt-owner')))

    def test_wrong_installation_epoch_receipt_refuses(self):
        from mop.server.sessions.models import AssignmentAttempt
        self.assert_delivery_refused(lambda: AssignmentAttempt.objects.filter(pk=self.attempts[1].pk).update(epoch=uuid4()))

    def test_human_permission_never_substitutes_enrolled_node(self):
        self.delivery()
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.issue, client, auth=self.auth)
        self.run_socket(body)

    def test_node_permission_revoked_refuses(self):
        self.assert_delivery_refused(lambda: self.machine_user.user_permissions.clear())

    def test_owner_permission_revoked_refuses(self):
        self.assert_delivery_refused(lambda: self.user.user_permissions.remove(
            Permission.objects.get(content_type__app_label='secrets', codename='deliver_secret')))

    def test_node_project_membership_removed_refuses(self):
        from mop.server.catalog.models import NodeProject
        self.assert_delivery_refused(lambda: NodeProject.objects.filter(node_id=self.assignment.node_id).delete())

    def test_service_project_enrollment_removed_refuses(self):
        from mop.server.access.models import ProjectMembership
        self.assert_delivery_refused(lambda: ProjectMembership.objects.filter(user=self.machine_user).delete())

    def test_revoked_machine_token_refuses(self):
        from rest_framework.authtoken.models import Token
        self.assert_delivery_refused(lambda: Token.objects.filter(user=self.machine_user).delete())

    def test_owner_disabled_refuses(self):
        from django.contrib.auth import get_user_model
        self.assert_delivery_refused(lambda: get_user_model().objects.filter(pk=self.user.pk).update(is_active=False))

    def test_unrelated_node_token_refuses(self):
        from mop.server.catalog.models import Node, NodeProject
        self.delivery()
        node = Node.objects.create(name='unrelated-node', driver='host', ssh_host='unrelated.invalid',
            control_origin='https://unrelated.invalid', lan_address='10.0.0.9', bootstrap_address='unrelated.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        user, token = self.enroll('unrelated-service', node.id,
            project_ids=(self.project.id,), permissions=('execution.poll_work',))
        user.user_permissions.add(Permission.objects.get(content_type__app_label='secrets', codename='deliver_secret'))
        auth = self.Auth('token', token)
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.issue, client, auth=auth)
        self.run_socket(body)

    def test_assignment_generation_changed_refuses(self):
        from mop.server.sessions.models import Assignment
        self.assert_delivery_refused(lambda: Assignment.objects.filter(pk=self.assignment.pk).update(generation=2))

    def test_expired_assignment_refuses_even_with_successful_receipts(self):
        from mop.server.sessions.models import Assignment
        self.assert_delivery_refused(lambda: Assignment.objects.filter(pk=self.assignment.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)))

    def test_installation_effects_disabled_refuses_even_current_assignment(self):
        from mop.server.controller.models import Installation
        self.assert_delivery_refused(lambda: Installation.objects.filter(pk=1).update(effects_enabled=False))

    def test_revoked_grant_refuses(self):
        from mop.server.access.models import Grant
        self.assert_delivery_refused(lambda: Grant._base_manager.filter(pk=self.grant_id).update(state='revoked'))

    def test_inactive_incarnation_refuses(self):
        from mop.server.sessions.models import ExecutionIncarnation
        self.assert_delivery_refused(lambda: ExecutionIncarnation.objects.filter(pk=self.assignment.current_incarnation_id).update(accepting=False))

    def test_missing_required_secret_refuses(self):
        self.assert_delivery_refused(lambda: self.app.store.retire_binding(self.access.token_context(self.token),
            self.binding.id, expected_revision=self.secret.revision))

    def test_binding_cannot_expand_sealed_role_allowlist(self):
        self.delivery()
        actor = self.access.token_context(self.token)
        extra = self.app.store.create(actor, project_id=self.project.id, name='EXTRA', kind='variable',
            purpose='project', value=b'never-export-extra')
        self.app.store.bind(actor, extra.id, expected_revision=extra.revision, node_id=self.assignment.node_id,
            role=self.assignment.role, digest=self.assignment.bundle_digest,
            incarnation=self.assignment.current_incarnation_id, grant_id=self.grant_id, assignment_id=self.assignment.id)
        async def body(client, server):
            rows = await asyncio.to_thread(self.issue, client)
            self.assertEqual([row.name for row in rows], ['DELIVERY'])
        self.run_socket(body)

    def test_revision_change_during_crypto_discards_plaintext(self):
        from mop.server.sessions.models import Assignment
        self.wait_change(lambda: Assignment.objects.filter(pk=self.assignment.pk).update(revision=2))

    def test_grant_revision_change_during_crypto_discards_plaintext(self):
        from django.db.models import F
        from mop.server.access.models import Grant
        self.wait_change(lambda: Grant._base_manager.filter(pk=self.grant_id).update(revision=F('revision') + 1))

    def test_secret_version_changed_during_crypto_discards_plaintext(self):
        self.wait_change(lambda: self.app.store.update(self.access.token_context(self.token), self.secret.id,
            b'next-value', expected_revision=self.secret.revision))

    def test_machine_token_revoked_during_crypto_discards_plaintext(self):
        from rest_framework.authtoken.models import Token
        self.wait_change(lambda: Token.objects.filter(user=self.machine_user).delete())

    def test_permission_revoked_during_crypto_discards_plaintext(self):
        self.wait_change(lambda: self.machine_user.user_permissions.clear())

    def wait_change(self, change):
        self.delivery()
        real = self.app.store.provider
        calls = []
        class Changing:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                self.assertTrue(connection.get_autocommit())
                value = real.decrypt(envelope, context)
                calls.append(1)
                change()
                return value
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.issue, client)
        self.run_socket(body, provider=Changing())
        self.assertEqual(len(calls), 1, 'must exercise actual decrypt then revalidation, not fail before crypto')

    def test_native_restore_authenticates_bundle_preparation_grant_and_project_versions(self):
        import tempfile
        from pathlib import Path
        from _controller_restore_cases import RestoreSafety
        from mop.server.controller import backup
        from mop.server.controller.models import Installation
        from mop.server.secrets.models import SecretVersion
        from mop.server.secrets.crypto import Envelope
        from mop.server.secrets.services import context
        self.delivery()
        versions = tuple(SecretVersion.objects.select_related('secret__installation').order_by('id'))
        self.assertGreaterEqual(len(versions), 4)
        self.assertTrue({'policy', 'grant', 'project'} <= {row.secret.purpose for row in versions})
        expected = {row.id: self.app.store.provider.decrypt(Envelope.from_bytes(bytes(row.envelope)),
            context(row.secret, row)) for row in versions}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'all-retained.dump'
            backup.backup(path)
            with RestoreSafety.target(self) as target:
                backup.restore(path, using=target)
                self.assertFalse(Installation.objects.using(target).get(pk=1).effects_enabled)
                recovered = tuple(SecretVersion.objects.using(target).select_related('secret__installation'))
                self.assertEqual({row.id for row in recovered}, set(expected))
                for row in recovered:
                    self.assertEqual(self.app.store.provider.decrypt(Envelope.from_bytes(bytes(row.envelope)),
                        context(row.secret, row)), expected[row.id])

    # test_addressed_delivery_uses_actual_frontend_uid_without_rights_inference
    # перенесён без изменения утверждений в узкий root gate #674
    # (_credential_uid_gate.AddressedCredentialUids): общий непривилегированный
    # прогон не может его проверить, и молчаливый пропуск читался зелёным.
