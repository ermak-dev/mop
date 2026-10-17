"""Django/PostgreSQL контракт #686: программа агента отдельно от его Actor.

RED до реализации: записей AgentProfile/AgentDefinitionVersion и use case'ов
publish/read/approve_definition нет — случаи падают поведенчески (assert
отсутствующей возможности), не ошибкой импорта.
"""
from datetime import timedelta
import json
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import serializers
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token

from mop.server.access import models as access, services as auth
from mop.server.catalog.models import Node, NodeProject, Project
from mop.server.controller.runtime import assignments_service, initialize
from mop.server.secrets.crypto import Keyring, XChaCha20Provider
from mop.server.sessions import protocol
from mop.server.sessions.services import Assignments

from _grant_cases import ApprovalFixture, BindingFixture, manifest


class AgentProgramCases(TransactionTestCase):
    # Как остальные ORM cases: flush не удаляет seeds новой установки.
    serialized_rollback = True

    # FEATURE: #686 — программа/исполнение агента отдельно от identity.
    # HYPOTHESIS: RoleBundle-семейство выглядит второй identity/ролевой
    # номенклатурой; у агента нет устойчивой привязки Actor→программа.
    # SOLUTION: AgentProfile (стабильный agent Actor → текущая approved
    # версия AgentDefinitionVersion) + минимальный register_agent; версии
    # программы immutable как прежде, словарь capabilities остаётся
    # техническим предусловием (сведение — #685/#688).
    # RED: до реализации все случаи падают на отсутствующих записях и use
    # case'ах (hasattr-предохранители в setUp).
    def setUp(self):
        self.assertTrue(hasattr(access, 'AgentProfile'), 'agent profile domain record is missing')
        self.assertTrue(hasattr(access, 'AgentDefinitionVersion'), 'immutable agent definition version is missing')
        from mop.server.access import grants as grants_module
        self.g = grants_module
        for name in ('publish_definition', 'read_definition', 'approve_definition',
                     'register_agent', 'repin_definition'):
            self.assertTrue(hasattr(grants_module.Grants, name), f'definition use case {name} is missing')
        initialize()
        self.project = Project.objects.create(name='alpha', origin='https://example.invalid/alpha.git')
        self.source = access.IdentitySource.objects.create(name='local', kind='local')
        self.user = get_user_model().objects.create_user('owner', password='test-only-password')
        access.LoginIdentity.objects.create(user=self.user, source=self.source, external_subject='owner')
        access.ProjectMembership.objects.create(user=self.user, project=self.project, source=self.source)
        for action in ('access.view_role', 'access.approve_role', 'access.issue_grant',
                       'access.revoke_grant', 'catalog.view_pool', 'sessions.create_puppet',
                       'secrets.manage_project_secret'):
            app, code = auth.permission_names(action)[0].split('.')
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        self.actor = auth.token_context(auth.issue_token(self.user.pk))
        self.provider = XChaCha20Provider(Keyring('test', {'test': b'q' * 32}))
        from mop.server.secrets.services import SecretStore
        policy = SecretStore(provider=self.provider, authorizer=auth.Access()).create(self.actor,
            project_id=self.project.pk, name='approved-policy', kind='credential', purpose='policy', value=b'exact approved bytes')
        self.proof = self.g.ApprovedPolicy(uuid4(), policy.active_version_id, 'a' * 64,
                                           self.user.pk, self.project.pk)
        self.approvals = ApprovalFixture(self.proof)
        self.bindings = BindingFixture()
        self.api = self.g.Grants(provider=self.provider, approvals=self.approvals, bindings=self.bindings)
        self.definition = self.publish('worker')

    def publish(self, role='worker', **options):
        raw = ('# exact manifest bytes\n' + json.dumps(manifest(role, **options), ensure_ascii=False)).encode()
        value = self.api.publish_definition(self.actor, project_id=self.project.pk,
            manifest=raw, prompt=b'private role prompt\r\n', provenance='test source revision')
        self.api.approve_definition(self.actor, value.id)
        return value

    def register(self, definition=None, target_id=None):
        return self.api.register_agent(self.actor, project_id=self.project.pk,
            target_id=target_id or uuid4(), definition_id=(definition or self.definition).id)

    def active(self, *, definition=None, actor=None):
        row = self.api.reserve(actor or self.actor, project_id=self.project.pk,
            definition_id=(definition or self.definition).id, target_id=uuid4(), incarnation=uuid4(),
            request_id=uuid4(), expires_at=timezone.now() + timedelta(hours=1))
        self.api.provision(actor or self.actor, row.id, expected_revision=row.revision)
        row = self.api.get(actor or self.actor, row.id)
        binding = self.g.RoleBinding(self.project.pk, row.target_id, row.incarnation,
                                    row.role, row.digest, row.id, self.user.pk)
        self.bindings.bindings[(self.project.pk, row.target_id)] = binding
        self.bindings.receipts[row.id] = binding
        self.api.activate(row.id, expected_revision=row.revision)
        return self.api.get(self.actor, row.id), auth.token_context(
            Token.objects.get(user__grant_machine__id=row.id).key, grants=self.api)

    def test_register_agent_creates_stable_agent_actor_without_new_identity(self):
        info = self.register()
        User = get_user_model()
        agent = User.objects.get(pk=info.agent_id)
        self.assertEqual(agent.kind, 'agent')
        self.assertFalse(agent.is_staff or agent.is_superuser or agent.has_usable_password())
        profile = access.AgentProfile.objects.get(project_id=self.project.pk, target_id=info.target_id)
        self.assertEqual((profile.agent_id, profile.definition_id), (info.agent_id, self.definition.id))
        # Повторная регистрация того же target не создаёт второго Actor.
        before = User.objects.count()
        again = self.register(target_id=info.target_id)
        self.assertEqual((again.agent_id, again.target_id), (info.agent_id, info.target_id))
        self.assertEqual(User.objects.count(), before)
        # Новая identity-таблица не появляется: Principal остался metadata.
        self.assertEqual(access.Principal.objects.count(), 0)

    def test_registration_requires_human_and_approved_project_definition(self):
        raw = json.dumps(manifest('unapproved')).encode()
        unapproved = self.api.publish_definition(self.actor, project_id=self.project.pk,
            manifest=raw, prompt=b'new', provenance='unapproved')
        with self.assertRaises(PermissionError):
            self.register(definition=unapproved)
        self.assertFalse(access.AgentProfile.objects.exists())

    def test_repin_moves_program_without_changing_agent_identity(self):
        info = self.register()
        second = self.publish('worker-2')
        repinned = self.api.repin_definition(self.actor, info.target_id,
            definition_id=second.id, expected_revision=info.revision)
        self.assertEqual((repinned.agent_id, repinned.definition_id, repinned.revision),
                         (info.agent_id, second.id, info.revision + 1))
        # Смена программы не трогает Actor: ни нового, ни удалённого.
        self.assertTrue(get_user_model().objects.filter(pk=info.agent_id, kind='agent').exists())
        with self.assertRaises(self.g.Conflict):
            self.api.repin_definition(self.actor, info.target_id,
                definition_id=second.id, expected_revision=info.revision)

    def test_one_agent_actor_survives_reassignment_restart_and_delegation_revocation(self):
        User = get_user_model()
        parent, parent_actor = self.active(definition=self.publish('parent', spawn=('worker',)))
        info = self.register(target_id=parent.target_id)
        node = Node.objects.create(name='node', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='assign_work'))
        now = timezone.now()

        class CleanClones:
            def observe(inner, **scope):
                return protocol.FactV1(scope['target_id'], scope['incarnation_id'], 1, now, now,
                    'fresh', (('clean', True), ('branch', 'master'), ('untracked', 0), ('modified', 0)))
        assignments = assignments_service(clones=CleanClones())
        before = User.objects.count()
        target = assignments.create(self.actor, project_id=self.project.pk, node_id=node.pk,
            role='worker', bundle_digest=parent.digest, grant_id=parent.id,
            expires_at=timezone.now() + timedelta(hours=1))
        first = assignments.activate(self.actor, target.id, expected_revision=target.revision, executor_epoch=uuid4())
        # restart: новая инкарнация того же назначения — тот же agent Actor.
        binding = assignments.target(first.assignment_id)
        released = assignments.release(self.actor, binding.target_id, expected_revision=binding.revision)
        reassigned = assignments.reassign(self.actor, binding.target_id, expected_revision=released.revision,
            role='worker', bundle_digest=parent.digest, grant_id=parent.id,
            expires_at=timezone.now() + timedelta(hours=1))
        second = assignments.activate(self.actor, reassigned.id, expected_revision=reassigned.revision, executor_epoch=uuid4())
        self.assertNotEqual(first.id, second.id, 'restart must be a new execution incarnation')
        self.assertEqual(User.objects.count(), before, 'assignment lifecycle created or deleted an Actor')
        self.assertTrue(access.AgentProfile.objects.filter(agent_id=info.agent_id, target_id=info.target_id).exists())
        # Отзыв отдельного делегирования не удаляет ни agent Actor, ни машину делегирования.
        revoked = self.api.revoke(self.actor, parent.id, expected_revision=parent.revision)
        self.assertEqual(revoked.state, 'revoked')
        self.assertEqual(User.objects.count(), before, 'delegation revocation deleted an Actor')
        self.assertTrue(User.objects.filter(pk=info.agent_id, kind='agent').exists())
        events = access.GrantEvent.objects.filter(grant_id=parent.id).order_by('created_at')
        self.assertGreaterEqual(events.count(), 3, 'history must stay attached to the same grant')

    def test_profile_is_domain_data_not_a_permission_source(self):
        info = self.register()
        agent = get_user_model().objects.get(pk=info.agent_id)
        # Профиль и версия программы не дают ни одного Django permission.
        self.assertFalse(agent.user_permissions.exists())
        self.assertFalse(agent.has_perm('catalog.view_pool'))
        permission = Permission.objects.get(content_type__app_label='catalog', codename='view_pool')
        agent.user_permissions.add(permission)
        # Кэш прав читается заново с живого Actor, не из профиля (#685).
        self.assertTrue(get_user_model().objects.get(pk=info.agent_id).has_perm('catalog.view_pool'))
        agent.user_permissions.remove(permission)
        self.assertFalse(get_user_model().objects.get(pk=info.agent_id).has_perm('catalog.view_pool'),
            'agent profile or definition must not keep granting after permission removal')
        # Право без runtime facts не даёт физический эффект: без живого grant
        # исполнение закрыто — прямой Authority запрос отказан.
        agent.user_permissions.add(permission)
        from django.db import transaction
        with transaction.atomic(), self.assertRaises(PermissionError):
            self.api.authorize_execution(self.g.RoleBinding(self.project.pk, uuid4(), uuid4(),
                'worker', 'b' * 64, uuid4(), self.user.pk))

    def test_definition_versions_keep_exact_bytes_seal_and_private_prompt(self):
        value = self.api.read_definition(self.actor, self.definition.id)
        self.assertEqual(value.prompt, b'private role prompt\r\n')
        self.assertNotIn('private role prompt', repr(value))
        dump = serializers.serialize('json', access.AgentDefinitionVersion.objects.all())
        self.assertNotIn('private role prompt', dump)
        with self.assertRaisesRegex(ValueError, 'sealed'):
            access.DefinitionCapability.objects.create(definition_id=self.definition.id,
                scope='cluster', verb='restart')
        with self.assertRaises(PermissionError):
            self.api.read_definition(self.actor, uuid4())
