"""Явное первое применение обычных Django Groups к агенту (#747).

HYPOTHESIS: fresh create проходит семь reference-фаз и активирует Grant, но
canonical AgentToken видит actions=[]: registration правильно не выдаёт прав,
а отдельного авторизованного provisioning выбранного baseline нет.
SPEC: approved immutable recipe выбирается до I/O и применяется один раз в
final compose после provisioned reserved Grant; receipt — прошлое, не desired
state. Обычные Groups и live issuer/ancestry остаются authority.
SOLUTION: approved bounded Group IDs входят в immutable initial policy; SQL-only
origin authority и final compose применяют их с receipt/Assignment/steps атомарно.
Receipt retry не читает memberships; pending activation/revoke сериализованы.
RESULT: исходный fresh-create RED — active canonical Token actions=[] без seed;
PG17/Python 3.12 — fresh create/socket/reference phases GREEN, live Group/owner
bounds, concurrent apply/revoke/activation, rollback/lost reply/no repair проверены.
STATUS: FIXED — see #747.
"""
from uuid import uuid4

from django.test import Client, TransactionTestCase, override_settings

import _create_cases as create_fixture
import _initial_create_cases as initial_fixture

@override_settings(ALLOWED_HOSTS=['testserver'])
class AgentRBACProvisioning(TransactionTestCase):
    serialized_rollback = True
    setUp = create_fixture.CreateVertical.setUp
    ready, body, submit = create_fixture.CreateVertical.ready, create_fixture.CreateVertical.body, create_fixture.CreateVertical.submit
    machine, planned, claim_next = initial_fixture.InitialCreatePhases.machine, initial_fixture.InitialCreatePhases.planned, initial_fixture.InitialCreatePhases.claim_next
    protected_submit, credential_app = initial_fixture.InitialCreatePhases.protected_submit, initial_fixture.InitialCreatePhases.credential_app

    def configured(self):
        from _register_peer import RegisterPeer
        from mop.server.operations.scheduler import NomadStopScheduler
        peer = RegisterPeer()
        self.addCleanup(peer.close)
        self.ready(scheduler=NomadStopScheduler(origin=peer.origin, scheduler_id=uuid4(), token='fixture-only', timeout=2))
        from django.contrib.auth.models import Group, Permission
        from mop.server.access.rbac import AgentRBACProfiles
        self.group = Group.objects.create(name='explicit-small-baseline')
        self.group.permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_pool'))
        self.rbac = AgentRBACProfiles().approve(self.actor, self.definition.id, [self.group.pk])
        from mop.server.access.models import AllProjectMembership, LoginIdentity
        AllProjectMembership.objects.get_or_create(user=self.user, source=LoginIdentity.objects.get(user=self.user).source)
        self.select(self.rbac)

    def select(self, profile):
        from mop.server.controller.initialselection import configure
        selected = self.selection
        self.selection = configure(self.actor, project_id=self.project.pk, definition_id=self.definition.id,
            bootstrap_revision=selected.bootstrap_revision, image_id=selected.image_id,
            source_origin=selected.source_origin, source_commit=selected.source_commit, source_ref=selected.source_ref,
            desired_model=selected.desired_model, model_config_digest=selected.model_config_digest,
            consumers=selected.consumers, rbac_profile_id=profile.id)
        return self.selection

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
                create_fixture.native_ack(self, work, self.machine_key)
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence=evidence)
        self.ops.activations.activator = create_fixture.CreateActivation.activator(self)
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
        from django.contrib.auth import get_user_model
        from mop.server.access.models import AgentRBACProvisioning
        subject = get_user_model().objects.get(pk=grant.subject_id)
        self.assertEqual((subject.kind, subject.is_staff, subject.is_superuser,
            subject.has_usable_password(), subject.user_permissions.count()), ('agent', False, False, False, 0))
        self.assertEqual(list(subject.groups.values_list('pk', flat=True)), [self.group.pk])
        receipt = AgentRBACProvisioning.objects.get(subject=subject)
        self.assertEqual((receipt.grant_id, receipt.profile_id, receipt.profile_digest,
            receipt.policy_id, receipt.policy_digest),
            (grant.id, self.rbac.id, self.rbac.digest, self.selection.policy_id, self.selection.digest))

    def provision_again(self, grant, *, authority=None):
        from django.db import transaction
        from mop.server.controller.initialselection import InitialRBACAuthority
        from mop.server.operations.models import CreateReservation
        reservation = CreateReservation.objects.get(pk=self.operation.createbinding.reservation_id)
        with transaction.atomic():
            return self.grants.provision_subject_rbac(self.actor, grant.id, expected_revision=grant.revision,
                authority=authority or InitialRBACAuthority(self.actor, reservation.id))

    def test_profile_is_canonical_immutable_group_ids_not_permission_snapshot(self):
        self.configured()
        from django.contrib.auth.models import Group
        from mop.server.access.rbac import AgentRBACProfiles
        from mop.server.access.models import ProfileRevocation
        second = Group.objects.create(name='second-baseline')
        profile = AgentRBACProfiles().approve(self.actor, self.definition.id, [second.pk, self.group.pk])
        self.assertEqual(profile.group_ids, sorted([second.pk, self.group.pk]))
        self.assertEqual(profile.approved_by_id, self.user.pk)
        self.assertEqual(len(profile.digest), 64)
        self.group.permissions.clear()
        self.group.name = 'renamed-baseline'
        self.group.save()
        profile.refresh_from_db()
        self.assertEqual(profile.group_ids, sorted([second.pk, self.group.pk]))
        with self.assertRaises(ValueError):
            profile.save()
        with self.assertRaises(ValueError):
            type(profile).objects.filter(pk=profile.id).update(group_ids=[])
        revoked = AgentRBACProfiles().revoke(self.actor, profile.id)
        self.assertEqual(revoked.profile_id, profile.id)
        self.assertEqual(AgentRBACProfiles().revoke(self.actor, profile.id).id, revoked.id)
        self.assertEqual(ProfileRevocation.objects.filter(profile=profile).count(), 1)

    def test_fresh_profile_scope_and_revocation_fit_one_unlocked_snapshot(self):
        # HYPOTHESIS (#755 + #747): отдельный revocation SELECT на каждый
        # checkpoint переполняет прежний 4096 SQL budget bootstrap batch.
        # Unlocked proof обязан видеть fresh profile/revocation одним SQL;
        # locked activation/apply по-прежнему проверяют отзыв после row lock.
        # SOLUTION: Exists annotation только у unlocked live proof, без cache.
        # RESULT: RED 2 != 1; GREEN fresh read и отзыв — по одному SQL,
        # все 30 #747 cases и 135 merged bootstrap/RBAC cases прошли на PG17.
        # STATUS: FIXED — see #755.
        self.configured()
        from mop.server.access.rbac import AgentRBACProfiles, require_profile
        arguments = dict(definition_id=self.definition.id, owner_id=self.user.pk,
            project_id=self.project.pk)
        with self.assertNumQueries(1):
            row = require_profile(self.rbac.id, self.rbac.digest, **arguments)
        self.assertEqual(row.id, self.rbac.id)
        AgentRBACProfiles().revoke(self.actor, self.rbac.id)
        with self.assertNumQueries(1), self.assertRaisesRegex(PermissionError, 'profile is revoked'):
            require_profile(self.rbac.id, self.rbac.digest, **arguments)

    def test_profile_rejects_unbounded_noncanonical_foreign_and_directory_group_inputs(self):
        self.configured()
        from django.contrib.auth.models import Group
        from mop.server.access.rbac import AgentRBACProfiles
        from mop.server.access.models import DirectoryGroupRule, IdentitySource
        profiles = AgentRBACProfiles()
        for group_ids in (None, (), [True], [0], [-1], ['1'], [self.group.pk]*2,
                          [2**63], list(range(1, 34)), [999999999]):
            with self.subTest(group_ids=group_ids), self.assertRaises((ValueError, PermissionError)):
                profiles.approve(self.actor, self.definition.id, group_ids)
        directory = Group.objects.create(name='directory-managed')
        source = IdentitySource.objects.create(name='directory', kind='ldap')
        DirectoryGroupRule.objects.create(source=source, group_dn='cn=agents', group=directory)
        with self.assertRaises(PermissionError):
            profiles.approve(self.actor, self.definition.id, [directory.pk])
        with self.assertRaises(PermissionError):
            profiles.approve(self.actor, uuid4(), [])

    def test_profile_admin_needs_both_existing_global_auth_permissions_not_role_approval(self):
        self.configured()
        from mop.server.access.rbac import AgentRBACProfiles
        from _grant_cases import permit_actions
        self.user.is_superuser = False
        self.user.save()
        for actions in (('access.approve_role',), ('auth.change_user',), ('auth.change_group',)):
            self.user.user_permissions.clear()
            permit_actions(self.user, *actions)
            with self.subTest(actions=actions), self.assertRaises(PermissionError):
                AgentRBACProfiles().approve(self.actor, self.definition.id, [])
            with self.assertRaises(PermissionError):
                AgentRBACProfiles().revoke(self.actor, self.rbac.id)
        permit_actions(self.user, 'auth.change_user', 'auth.change_group')
        approved = AgentRBACProfiles().approve(self.actor, self.definition.id, [])
        self.assertEqual(approved.group_ids, [])
        from mop.server.access.models import AllProjectMembership
        AllProjectMembership.objects.filter(user=self.user).delete()
        with self.assertRaises(PermissionError):
            AgentRBACProfiles().approve(self.actor, self.definition.id, [])

    def test_explicit_passive_profile_leaves_fresh_subject_permission_free(self):
        self.configured()
        from mop.server.access.rbac import AgentRBACProfiles
        self.select(AgentRBACProfiles().approve(self.actor, self.definition.id, []))
        grant = self.activate(self.create())
        self.assertEqual(self.capabilities(grant.subject_id), [])

    def test_missing_and_revoked_profile_refuse_before_credential_io(self):
        self.configured()
        from unittest.mock import patch
        from mop.server.catalog.models import InitialCreateSelection
        from mop.server.access.rbac import AgentRBACProfiles
        InitialCreateSelection.objects.filter(project=self.project).delete()
        with patch.object(self.ops.creates, '_agent_credential', side_effect=AssertionError('credential I/O')):
            response = self.submit(self.body())
        self.assertNotEqual(response.status_code, 202)
        self.select(self.rbac)
        AgentRBACProfiles().revoke(self.actor, self.rbac.id)
        with patch.object(self.ops.creates, '_agent_credential', side_effect=AssertionError('credential I/O')):
            response = self.submit(self.body())
        self.assertNotEqual(response.status_code, 202)

    def test_receipt_retry_never_repairs_membership_and_profile_revoke_after_active_is_not_authority(self):
        self.configured()
        from django.contrib.auth import get_user_model
        from mop.server.access.rbac import AgentRBACProfiles
        self.operation = self.create()
        grant = self.activate(self.operation)
        AgentRBACProfiles().revoke(self.actor, self.rbac.id)
        self.assertIn('catalog.view_pool', self.capabilities(grant.subject_id))
        subject = get_user_model().objects.get(pk=grant.subject_id)
        subject.groups.remove(self.group)
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as captured:
            receipt = self.provision_again(grant)
        self.assertEqual(receipt.subject_id, subject.pk)
        self.assertFalse(any('auth_group' in query['sql'] or 'actor_actor_groups' in query['sql']
            for query in captured.captured_queries), 'identical receipt must not inspect Group membership')
        self.assertEqual(self.capabilities(subject.pk), [])
        # Отозванная recipe не блокирует явное восстановление обычного RBAC.
        subject.groups.add(self.group)
        self.assertIn('catalog.view_pool', self.capabilities(subject.pk))

    def test_current_group_permissions_and_owner_upper_bound_remain_live(self):
        self.configured()
        from _grant_cases import permit_actions
        grant = self.activate(self.create())
        self.assertIn('catalog.view_pool', self.capabilities(grant.subject_id))
        self.group.permissions.clear()
        self.assertEqual(self.capabilities(grant.subject_id), [])
        from django.contrib.auth.models import Permission
        self.group.permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_pool'))
        self.user.is_superuser = False
        self.user.save()
        permit_actions(self.user, 'access.view_role', 'preparation.approve_bootstrap')
        self.assertNotIn('catalog.view_pool', self.capabilities(grant.subject_id))
        permit_actions(self.user, 'catalog.view_pool')
        self.assertIn('catalog.view_pool', self.capabilities(grant.subject_id))

    def test_group_deletion_and_recreation_cannot_substitute_id_or_repair(self):
        self.configured()
        from django.contrib.auth.models import Group
        self.operation = self.create()
        grant = self.activate(self.operation)
        old_id, name = self.group.pk, self.group.name
        self.group.delete()
        replacement = Group.objects.create(name=name)
        self.assertNotEqual(replacement.pk, old_id)
        self.provision_again(grant)
        self.assertEqual(self.capabilities(grant.subject_id), [])
        from mop.server.access.models import AgentRBACProvisioning
        self.assertEqual(AgentRBACProvisioning.objects.get(subject_id=grant.subject_id).profile_id, self.rbac.id)

    def test_atomic_compose_rollback_leaves_no_groups_or_receipt(self):
        self.configured()
        from unittest.mock import patch
        from mop.server.access.models import AgentProfile, AgentRBACProvisioning
        with patch.object(self.ops.creates.targets, 'assign', side_effect=RuntimeError('compose interrupted')):
            with self.assertRaises(RuntimeError):
                self.submit(self.body())
        subject = AgentProfile.objects.get().agent
        self.assertEqual(subject.groups.count(), 0)
        self.assertFalse(AgentRBACProvisioning.objects.exists())

    def test_revoked_profile_blocks_pending_initial_activation(self):
        self.configured()
        from mop.server.access.rbac import AgentRBACProfiles
        from mop.server.controller.credentials import CurrentBindings
        row = self.create()
        execution, actor = self.machine()
        self.planned(row, execution, actor)
        self.ops.launches.run(row.id)
        for action, evidence in (('native_exec', 'native-started'), ('confirm_ready', 'ready')):
            work, attempt = self.claim_next(execution, actor, row, action)
            if action == 'native_exec':
                create_fixture.native_ack(self, work, self.machine_key)
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence=evidence)
        grant_id = row.restartbinding.grant_id
        self.assertIsNotNone(CurrentBindings().activation(grant_id))
        AgentRBACProfiles().revoke(self.actor, self.rbac.id)
        with self.assertRaises(PermissionError):
            CurrentBindings().activation(grant_id)

    def pending_origin(self):
        from unittest.mock import patch
        from mop.server.access.models import Grant
        from mop.server.operations.models import CreateReservation
        with patch.object(self.ops.creates.targets, 'assign', side_effect=RuntimeError('compose interrupted')):
            with self.assertRaises(RuntimeError):
                self.submit(self.body())
        return Grant.objects.get(), CreateReservation.objects.get()

    def apply_origin(self, grant, reservation):
        from django.db import transaction
        from mop.server.controller.initialselection import InitialRBACAuthority
        with transaction.atomic():
            return self.grants.provision_subject_rbac(self.actor, grant.id, expected_revision=grant.revision,
                authority=InitialRBACAuthority(self.actor, reservation.id))

    def test_profile_change_after_committed_reservation_keeps_exact_original_recipe(self):
        self.configured()
        from mop.server.access.rbac import AgentRBACProfiles
        from mop.server.access.models import AgentRBACProvisioning
        from mop.server.operations.models import Operation
        passive = AgentRBACProfiles().approve(self.actor, self.definition.id, [])
        original = self.selection
        response = self.protected_submit(self.body(), lambda: self.select(passive))
        self.assertEqual(response.status_code, 202, response.content)
        operation = Operation.objects.get(pk=response.json()['operation']['id'])
        self.assertEqual(operation.createbinding.reservation.policy_id, original.policy_id)
        applied = AgentRBACProvisioning.objects.get()
        self.assertEqual(applied.profile_id, self.rbac.id)
        self.assertEqual(list(applied.subject.groups.values_list('pk', flat=True)), [self.group.pk])

    def test_profile_revocation_between_reservation_and_compose_refuses_without_partial_membership(self):
        self.configured()
        from mop.server.access.rbac import AgentRBACProfiles
        from mop.server.access.models import AgentProfile, AgentRBACProvisioning
        from mop.server.operations.models import Operation
        response = self.protected_submit(self.body(), lambda: AgentRBACProfiles().revoke(self.actor, self.rbac.id))
        self.assertNotEqual(response.status_code, 202)
        self.assertFalse(AgentRBACProvisioning.objects.exists())
        self.assertFalse(Operation.objects.exists())
        self.assertEqual(AgentProfile.objects.get().agent.groups.count(), 0)

    def test_scoped_creator_provision_permission_is_required_before_io_without_global_admin(self):
        self.configured()
        from _grant_cases import permit_actions
        from unittest.mock import patch
        self.user.is_superuser = False
        self.user.save()
        self.user.user_permissions.clear()
        permit_actions(self.user, 'operations.view_operation', 'execution.create_puppet',
            'execution.assign_work', 'access.issue_grant', 'access.view_role', 'preparation.approve_bootstrap')
        with patch.object(self.ops.creates, '_agent_credential', side_effect=AssertionError('credential I/O')):
            response = self.submit(self.body())
        self.assertEqual(response.status_code, 403, response.content)
        permit_actions(self.user, 'access.provision_agent_rbac')
        self.assertEqual(self.submit(self.body()).status_code, 202)

    def test_first_application_refuses_active_unprovisioned_foreign_origin_and_directory_transition(self):
        self.configured()
        from mop.server.access.models import Grant, DirectoryGroupRule, IdentitySource, AgentRBACProvisioning
        from mop.server.controller.initialselection import InitialRBACAuthority
        from mop.server.access.rbac import AgentRBACProfiles
        from mop.server.operations.models import CreateReservation
        grant, reservation = self.pending_origin()
        from django.db import transaction
        with transaction.atomic(), self.assertRaises(PermissionError):
            self.grants.provision_subject_rbac(self.actor, grant.id, expected_revision=grant.revision,
                authority=InitialRBACAuthority(self.actor, uuid4()))
        # После approval local Group может стать directory-managed: первый apply отказывает.
        source = IdentitySource.objects.create(name='directory', kind='ldap')
        DirectoryGroupRule.objects.create(source=source, group_dn='cn=agents', group=self.group)
        with self.assertRaises(PermissionError):
            self.apply_origin(grant, reservation)
        DirectoryGroupRule.objects.all().delete()
        # Контрфакт хранилища: active без первой квитанции не легализует apply.
        from django.db.models import QuerySet
        QuerySet.update(Grant.objects.filter(pk=grant.id), state='active')
        with self.assertRaises(PermissionError):
            self.apply_origin(grant, reservation)
        self.assertFalse(AgentRBACProvisioning.objects.exists())

    def test_first_application_rejects_foreign_registered_subject_grant_and_missing_credential(self):
        self.configured()
        from django.db import transaction
        from django.db.models import QuerySet
        from mop.server.access.models import AgentRBACProvisioning, Grant
        from mop.server.controller.initialselection import InitialRBACAuthority
        grant, reservation = self.pending_origin()
        foreign = self.grants.register_agent(self.actor, project_id=self.project.pk,
            target_id=uuid4(), definition_id=self.definition.id)
        # Контрфакты immutable storage не превращают чужую identity или
        # ещё не provisioned Grant в допустимую первую application.
        for changed in ({'subject_id': foreign.agent_id}, {'credential_version_id': None}):
            with self.subTest(changed=changed), transaction.atomic():
                QuerySet.update(Grant.objects.filter(pk=grant.id), **changed)
                with self.assertRaises(PermissionError):
                    self.apply_origin(grant, reservation)
                transaction.set_rollback(True)
        second = self.create()
        other_grant = Grant.objects.get(pk=second.restartbinding.grant_id)
        # Даже одинаковые project/owner/definition/profile не заменяют exact
        # originating Grant/request/target другой committed reservation.
        with transaction.atomic(), self.assertRaises(PermissionError):
            self.grants.provision_subject_rbac(self.actor, other_grant.id,
                expected_revision=other_grant.revision,
                authority=InitialRBACAuthority(self.actor, reservation.id))
        self.assertFalse(AgentRBACProvisioning.objects.filter(subject_id=grant.subject_id).exists())
        self.assertEqual(grant.subject.groups.count(), 0)

    def test_concurrent_first_application_serializes_stable_actor_and_keeps_token(self):
        self.configured()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        from mop.server.access.models import AgentRBACProvisioning
        from rest_framework.authtoken.models import Token
        grant, reservation = self.pending_origin()
        key = Token.objects.get(user_id=grant.subject_id).key
        barrier = Barrier(2)
        def apply():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.apply_origin(grant, reservation).id
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as threads:
            first, second = threads.submit(apply), threads.submit(apply)
            self.assertEqual(first.result(timeout=20), second.result(timeout=20))
        self.assertEqual(AgentRBACProvisioning.objects.count(), 1)
        self.assertEqual(Token.objects.get(user_id=grant.subject_id).key, key)
        self.assertEqual(list(AgentRBACProvisioning.objects.get().subject.groups.values_list('pk', flat=True)), [self.group.pk])

    def test_revoke_and_first_application_serialize_on_profile_separate_connections(self):
        self.configured()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        from django.db import close_old_connections
        from mop.server.access import rbac
        from mop.server.access.models import AgentRBACProvisioning, ProfileRevocation
        grant, reservation = self.pending_origin()
        applying, release, revoking = Event(), Event(), Event()
        original = rbac.ordinary_groups
        def held(*args, **kwargs):
            groups = original(*args, **kwargs)
            applying.set()
            if not release.wait(timeout=10):
                raise AssertionError('profile serialization test timed out')
            return groups
        def apply():
            close_old_connections()
            try:
                return self.apply_origin(grant, reservation).id
            finally:
                close_old_connections()
        def revoke():
            close_old_connections()
            try:
                revoking.set()
                return rbac.AgentRBACProfiles().revoke(self.actor, self.rbac.id).id
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as threads, patch.object(rbac, 'ordinary_groups', held):
            first = threads.submit(apply)
            try:
                self.assertTrue(applying.wait(timeout=10))
                second = threads.submit(revoke)
                self.assertTrue(revoking.wait(timeout=10))
                self.assertFalse(second.done(), 'revoke must wait for first application profile lock')
            finally:
                release.set()
            first.result(timeout=15)
            second.result(timeout=15)
        self.assertEqual(AgentRBACProvisioning.objects.count(), 1)
        self.assertEqual(ProfileRevocation.objects.count(), 1)
        # Обратный порядок: revoked recipe не допускает первую запись.
        other = rbac.AgentRBACProfiles().approve(self.actor, self.definition.id, [])
        rbac.AgentRBACProfiles().revoke(self.actor, other.id)
        with self.assertRaises(PermissionError):
            rbac.require_profile(other.id, other.digest, definition_id=self.definition.id,
                owner_id=self.user.pk, project_id=self.project.pk, lock=False)

    def test_lost_compose_reply_and_update_repin_do_not_reapply_or_rotate_identity(self):
        self.configured()
        from unittest.mock import patch
        from mop.server.access.models import AgentRBACProvisioning
        from mop.server.operations.models import Operation
        from rest_framework.authtoken.models import Token
        body = self.body()
        original = self.ops.creates._compose
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('lost committed reply')
        with patch.object(self.ops.creates, '_compose', lost), self.assertRaises(RuntimeError):
            self.submit(body)
        receipt = AgentRBACProvisioning.objects.get()
        key = Token.objects.get(user_id=receipt.subject_id).key
        receipt.subject.groups.clear()
        response = self.submit(body)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(Operation.objects.count(), 1)
        self.assertEqual(receipt.subject.groups.count(), 0)
        profile = self.grants.agent_profile(self.actor, project_id=self.project.pk,
            target_id=receipt.grant.intent.target_id)
        self.grants.repin_definition(self.actor, profile.target_id,
            definition_id=self.definition.id, expected_revision=profile.revision)
        self.assertEqual(receipt.subject.groups.count(), 0)
        self.assertEqual(Token.objects.get(user_id=receipt.subject_id).key, key)
        self.assertEqual(AgentRBACProvisioning.objects.count(), 1)

    def ready_to_activate(self):
        row = self.create()
        execution, actor = self.machine()
        self.planned(row, execution, actor)
        self.ops.launches.run(row.id)
        for action, evidence in (('native_exec', 'native-started'), ('confirm_ready', 'ready')):
            work, attempt = self.claim_next(execution, actor, row, action)
            if action == 'native_exec':
                create_fixture.native_ack(self, work, self.machine_key)
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence=evidence)
        from mop.server.access.models import Grant
        return row, Grant.objects.get(pk=row.restartbinding.grant_id)

    def test_actual_grant_activation_rechecks_revoked_profile_not_only_worker_claim(self):
        self.configured()
        from mop.server.access.rbac import AgentRBACProfiles
        row, grant = self.ready_to_activate()
        activator = create_fixture.CreateActivation.activator(self)
        AgentRBACProfiles().revoke(self.actor, self.rbac.id)
        with self.assertRaises(PermissionError):
            activator.grants.activate(grant.id, expected_revision=grant.revision)
        grant.refresh_from_db()
        self.assertEqual(grant.state, 'reserved')

    def test_initial_receipt_cannot_activate_if_its_pinned_create_binding_disappeared(self):
        # HYPOTHESIS: отсутствие CreateBinding ошибочно считает initial Grant
        # обычным replacement и обходит его receipt/profile authority.
        self.configured()
        from mop.server.operations.models import CreateBinding
        from mop.server.controller.credentials import CurrentBindings
        row, grant = self.ready_to_activate()
        CreateBinding.objects.filter(operation=row).delete()
        with self.assertRaises(PermissionError):
            CurrentBindings().activation(grant.id)

    def test_actual_activation_and_revoke_serialize_until_commit_not_forever(self):
        self.configured()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        from django.db import close_old_connections
        from mop.server.access import rbac
        row, grant = self.ready_to_activate()
        activator = create_fixture.CreateActivation.activator(self)
        locked, release, revoking = Event(), Event(), Event()
        original = rbac.require_profile
        def held(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('lock'):
                locked.set()
                if not release.wait(timeout=10):
                    raise AssertionError('activation profile serialization timed out')
            return result
        def activate():
            close_old_connections()
            try:
                return activator.grants.activate(grant.id, expected_revision=grant.revision)
            finally:
                close_old_connections()
        def revoke():
            close_old_connections()
            try:
                revoking.set()
                return rbac.AgentRBACProfiles().revoke(self.actor, self.rbac.id)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as threads, patch.object(rbac, 'require_profile', held):
            first = threads.submit(activate)
            try:
                self.assertTrue(locked.wait(timeout=10))
                second = threads.submit(revoke)
                self.assertTrue(revoking.wait(timeout=10))
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=15).state, 'active')
            second.result(timeout=15)
        self.assertIn('catalog.view_pool', self.capabilities(grant.subject_id))

    def test_foreign_profile_proof_cannot_be_selected_for_current_definition_owner_or_project(self):
        self.configured()
        from mop.server.access.rbac import require_profile
        for changed in ({'definition_id': uuid4()}, {'owner_id': self.user.pk+1000}, {'project_id': uuid4()}):
            scope = dict(definition_id=self.definition.id, owner_id=self.user.pk, project_id=self.project.pk)
            scope.update(changed)
            with self.subTest(changed=changed), self.assertRaises(PermissionError):
                require_profile(self.rbac.id, self.rbac.digest, **scope)
        with self.assertRaises(PermissionError):
            require_profile(self.rbac.id, '0'*64, definition_id=self.definition.id,
                owner_id=self.user.pk, project_id=self.project.pk)

    def test_replacement_preparation_never_reapplies_revoked_recipe_or_latest_selection(self):
        self.configured()
        from rest_framework.authtoken.models import Token
        from mop.server.access.models import AgentRBACProvisioning, Grant
        from mop.server.access.rbac import AgentRBACProfiles
        import _replacement_cases as replacement_fixture
        from unittest.mock import patch
        initial = self.create()
        source_grant = self.activate(initial)
        receipt = AgentRBACProvisioning.objects.get(subject_id=source_grant.subject_id)
        subject = source_grant.subject
        token = Token.objects.get(user=subject).key
        subject.groups.clear()
        AgentRBACProfiles().revoke(self.actor, self.rbac.id)
        self.select(AgentRBACProfiles().approve(self.actor, self.definition.id, []))
        # Existing claimed-replacement fixture получает уже реально завершённый
        # initial create; его повторные readiness/activation здесь не нужны.
        with patch.object(self, 'launch', return_value=(initial.id, initial.target_id, None), create=True), \
                patch.object(self, 'through_readiness', return_value=None, create=True), \
                patch.object(self, 'activator', return_value=create_fixture.CreateActivation.activator(self), create=True):
            replacement_fixture.ReplacementPreparation.fixture(self)
        result = replacement_fixture.ReplacementPreparation.prepare(self)
        replacement = Grant.objects.get(pk=result['id'])
        self.assertNotEqual(replacement.id, source_grant.id)
        self.assertEqual(replacement.subject_id, source_grant.subject_id)
        self.assertEqual(Token.objects.get(user=subject).key, token)
        self.assertEqual(list(subject.groups.values_list('pk', flat=True)), [])
        self.assertEqual(AgentRBACProvisioning.objects.get(subject=subject).id, receipt.id)
        self.assertEqual(self.capabilities(subject.pk), [])

    def test_recipe_can_be_archived_without_erasing_receipt_or_disabling_active_group_rights(self):
        self.configured()
        from django.db.models import QuerySet
        from mop.server.access.models import AgentRBACProfile, AgentRBACProvisioning
        self.operation = self.create()
        grant = self.activate(self.operation)
        receipt = AgentRBACProvisioning.objects.get()
        # Моделируем явную retention review, а не предлагаем public delete evidence.
        QuerySet.delete(AgentRBACProfile.objects.filter(pk=self.rbac.id))
        self.assertEqual(self.provision_again(grant).id, receipt.id)
        self.assertIn('catalog.view_pool', self.capabilities(grant.subject_id))
        self.assertEqual(AgentRBACProvisioning.objects.get().profile_id, self.rbac.id)

    def test_group_rename_keeps_exact_id_and_deleted_group_cannot_first_apply_by_recreated_name(self):
        self.configured()
        from django.contrib.auth.models import Group
        grant, reservation = self.pending_origin()
        self.group.name = 'ordinary-renamed'
        self.group.save()
        applied = self.apply_origin(grant, reservation)
        self.assertEqual(list(applied.subject.groups.values_list('pk', flat=True)), [self.group.pk])
        from mop.server.access.rbac import AgentRBACProfiles, ordinary_groups
        other = Group.objects.create(name='deleted-before-apply')
        profile = AgentRBACProfiles().approve(self.actor, self.definition.id, [other.pk])
        other.delete()
        Group.objects.create(name='deleted-before-apply')
        with self.assertRaises(PermissionError):
            ordinary_groups(profile.group_ids)

    def test_new_permission_is_in_public_client_codec_and_schema(self):
        from mop.client.action_catalogue import ACTIONS_V1
        from pathlib import Path
        import yaml
        self.assertIn('access.provision_agent_rbac', ACTIONS_V1)
        schema = yaml.safe_load((Path(__file__).resolve().parents[1]/'docs/API.schema.yaml').read_text())
        self.assertIn('access.provision_agent_rbac', schema['components']['schemas']['ActionsEnum']['enum'])

    def test_admin_approval_and_revoke_are_named_services_not_generic_nonhuman_crud(self):
        self.configured()
        from mop.server.access.models import AgentRBACProfile, ProfileRevocation
        from unittest.mock import patch
        from mop.server.access.rbac import AgentRBACProfiles
        browser = Client()
        response = browser.post('/api/v1/auth/login', {'username': 'owner', 'password': 'fixture-only'}, secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        with patch.object(AgentRBACProfiles, 'approve', wraps=AgentRBACProfiles().approve) as approve:
            response = browser.post('/admin/access/agentrbacprofile/approve/',
                {'definition_id': str(self.definition.id), 'group_ids': [str(self.group.pk)]}, secure=True)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(approve.call_count, 1)
        row = AgentRBACProfile.objects.exclude(id__in=(self.rbac.id, self.passive_rbac.id)).get()
        with patch.object(AgentRBACProfiles, 'revoke', wraps=AgentRBACProfiles().revoke) as revoke:
            response = browser.post(f'/admin/access/agentrbacprofile/{row.id}/revoke/', {}, secure=True)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(revoke.call_count, 1)
        self.assertTrue(ProfileRevocation.objects.filter(profile=row).exists())
        self.assertNotEqual(browser.post('/admin/access/agentrbacprofile/add/', {}, secure=True).status_code, 302)

    def test_application_requires_sql_authority_exact_origin_and_current_creator_permission(self):
        self.configured()
        from django.db import transaction
        from mop.server.access.models import AgentRBACProvisioning
        from mop.server.access.rbac import InitialRBACSelection
        from mop.server.access.grants import Conflict
        self.operation = self.create()
        from mop.server.access.models import Grant
        grant = Grant.objects.get(pk=self.operation.restartbinding.grant_id)
        class Foreign:
            def resolve(inner, actor, grant_id):
                receipt = AgentRBACProvisioning.objects.get(subject_id=grant.subject_id)
                return InitialRBACSelection(receipt.profile_id, receipt.profile_digest, uuid4(),
                    receipt.policy_digest, receipt.reservation_id)
        with transaction.atomic(), self.assertRaises(Conflict):
            self.provision_again(grant, authority=Foreign())
        with transaction.atomic(), self.assertRaises((ValueError, PermissionError)):
            self.grants.provision_subject_rbac(self.actor, grant.id, expected_revision=grant.revision, authority=None)
