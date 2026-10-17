"""Django/PostgreSQL contract единого Actor #683; запуск fixture'ом #678.

RED пишется до реализации: AUTH_USER_MODEL ещё auth.User, приложения actor
нет -- каждый случай падает поведенчески, не ошибкой импорта.
"""
from django.apps import apps  # noqa: F401 — registry contract
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone
from mop.server.access.permissions import permission_names
import json


class UnifiedActorIdentity(TransactionTestCase):
    """Один Actor = единственный AUTH_USER_MODEL; kind не даёт прав."""

    # Как остальные ORM cases: flush не удаляет seeds новой установки.
    serialized_rollback = True

    # FEATURE: #683 — единый Actor вместо stock auth.User + Principal.
    # HYPOTHESIS: своя модель без swappable FK ломает django.contrib.auth
    # и createsuperuser; kind-поле без рамки превращается в permission route.
    # SOLUTION: actor.Actor(AbstractUser) как AUTH_USER_MODEL, kind только
    # классифицирует, standard manager/UserAdmin, свёртка Principal в
    # deprecated metadata до #684--#689.
    # RED: три behavioral red (app, AUTH_USER_MODEL, whitelist) в
    # tests/actor_identity.py; эти Django cases красны до impl в fixture.
    # RESULT: PG17 fixture — 7/7 green; offline actor_identity 3/3 green;
    # makemigrations check пуст по всем apps, полный controller gate green.
    # STATUS: FIXED — see #683

    def test_actor_is_the_only_user_model_and_classifies_without_rights(self):
        User = get_user_model()
        self.assertEqual((User._meta.app_label, User.__name__), ('actor', 'Actor'))
        # Один PK на человека/сервис/агента; incarnation не создаёт Actor.
        human = User.objects.create_user('human-one', password='a-long-distinct-passphrase-683')
        self.assertEqual(User.objects.filter(username='human-one').count(), 1)
        self.assertEqual(human.kind, 'human')
        # Сервисный Actor заводится без пароля с явным kind; password/staff
        # остаются стандартными флагами, а не производными kind.
        service = User(username='service-one', kind='service')
        service.save()
        self.assertEqual(User.objects.get(username='service-one').kind, 'service')
        # kind не даёт ни staff, ни permission, ни scope.
        self.assertFalse(human.is_staff or human.is_superuser or service.is_staff or service.is_superuser)
        self.assertEqual(list(human.user_permissions.all()), [])
        self.assertFalse(User.objects.get(pk=human.pk).has_perm('catalog.view_project'))

    def test_kind_is_a_discriminator_not_a_permission(self):
        User = get_user_model()
        user = User.objects.create_user('kind-proof', password=None)
        for kind in ('human', 'service', 'agent'):
            User.objects.filter(pk=user.pk).update(kind=kind)
            fresh = User.objects.get(pk=user.pk)
            self.assertFalse(fresh.is_staff or fresh.is_superuser)
            self.assertEqual(list(fresh.groups.all()), [])
            self.assertEqual(list(fresh.user_permissions.all()), [])
        # Незарегистрированный kind обязан отказывать: длина/vocabulary
        # констрейнта БД, не мягкая молчаливая нормализация.
        from django.db import IntegrityError, DataError
        with self.assertRaises((IntegrityError, DataError)):
            with transaction.atomic():
                User.objects.create(username='invalid-kind', kind='not-a-kind')
        User.objects.filter(pk=user.pk).delete()

    def test_standard_groups_permissions_and_manager_survive(self):
        from django.contrib.auth.models import Group, Permission
        User = get_user_model()
        user = User.objects.create_user('grouped', password=None)
        group = Group.objects.create(name='operators')
        permission = Permission.objects.get(codename='view_project', content_type__app_label='catalog')
        group.permissions.add(permission)
        user.groups.add(group)
        self.assertTrue(User.objects.get(pk=user.pk).has_perm('catalog.view_project'))
        user.groups.remove(group)
        # Права читаются заново: снятие группы снимает permission.
        self.assertFalse(User.objects.get(pk=user.pk).has_perm('catalog.view_project'))
        self.assertEqual(User._default_manager.get(pk=user.pk).username, 'grouped')

    def test_principal_rows_are_deprecated_metadata_not_identity(self):
        from mop.server.access import models as access
        User = get_user_model()
        user = User.objects.create_user('legacy-shape', password=None)
        # Форма сохранена до #689 (retarget), но это не второй identity:
        # те же права читаются из стандартного User, а не из Principal.
        # С #684 пути выдачи больше не создают строк вовсе.
        self.assertEqual(User.objects.filter(pk=user.pk).count(), 1)
        self.assertEqual(access.Principal.objects.filter(user=user).count(), 0)

    def test_enrollment_and_grant_actors_carry_their_kind_on_the_actor(self):
        # Классификация живёт на самом Actor, дублирующий Principal —
        # deprecated metadata до #684/#688.
        from mop.server.catalog import models as catalog
        from mop.server.access import services as access
        from mop.server.access import models as access_models
        node = catalog.Node.objects.create(name='enroll-node', driver='host',
            ssh_host='n.invalid', control_origin='https://n.invalid',
            lan_address='n.invalid', bootstrap_address='n.invalid')
        token = access.enroll_service('enrolled-service', node.pk, project_ids=(),
                                      permissions=('catalog.observe_node',))
        User = get_user_model()
        service = User.objects.get(username='enrolled-service')
        self.assertEqual(service.kind, 'service')
        self.assertFalse(access_models.Principal.objects.filter(user=service).exists())

    def test_grant_reservation_stamps_service_kind_on_the_actor(self):
        # Делегированная машина получает Actor с kind='service': классификация
        # на самой модели, Principal.kind='grant' — deprecated metadata до #688.
        import uuid as _uuid
        from datetime import timedelta
        from mop.server.controller.runtime import initialize
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        from mop.server.secrets.services import SecretStore
        from mop.server.access import services as auth, grants as grants_module
        initialize()
        from mop.server.catalog.models import Project
        from mop.server.access import models as access
        User = get_user_model()
        project = Project.objects.create(name='grant-kind', origin='https://example.invalid/g.git')
        source = access.IdentitySource.objects.create(name='local', kind='local')
        user = User.objects.create_user('grant-owner-683', password='grant-only-passphrase-683')
        access.LoginIdentity.objects.create(user=user, source=source, external_subject='grant-owner')
        access.ProjectMembership.objects.create(user=user, project_id=project.pk, source=source)
        from django.contrib.auth.models import Permission
        for action in ('access.view_role', 'access.approve_role', 'access.issue_grant',
                       'access.revoke_grant', 'catalog.view_pool', 'secrets.manage_project_secret',
                       'execution.create_puppet'):
            for permission in permission_names(action):
                app, code = permission.split('.')
                user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))
        actor = auth.token_context(auth.issue_token(user.pk))
        provider = XChaCha20Provider(Keyring('test', {'test': b'q' * 32}))
        policy = SecretStore(provider=provider, authorizer=auth.Access()).create(actor,
            project_id=project.pk, name='policy-kind', kind='credential', purpose='policy', value=b'bytes')
        proof = grants_module.ApprovedPolicy(_uuid.uuid4(), policy.active_version_id, 'b' * 64,
                                             user.pk, project.pk)
        class _Approvals:
            def pin(self, binding):
                return proof

            def validate(self, proof_value, binding):
                # Trusted port обязан возвращать None, не caller booleans.
                if (proof_value.owner_id, proof_value.project_id) != (binding.owner_id, binding.project_id):
                    raise PermissionError('approved policy is unavailable')
                return None
        class _Bindings:
            def __init__(self):
                self.bindings, self.receipts = {}, {}
        api = grants_module.Grants(provider=provider, approvals=_Approvals(), bindings=_Bindings())
        raw = b'# manifest\n' + json.dumps({'soul': 'worker', 'version': 1,
            'runtime': {'driver': 'any', 'receives_dispatch': True},
            'capabilities': {'node_verbs': ['send'], 'cluster_verbs': [], 'dispatch': False, 'spawn': []},
            'prompt': 'soul.md'}, ensure_ascii=False).encode()
        bundle = api.publish_bundle(actor, project_id=project.pk, manifest=raw,
                                    prompt=b'prompt', provenance='test')
        api.approve_bundle(actor, bundle.id)
        row = api.reserve(actor, project_id=project.pk, bundle_id=bundle.id,
                          target_id=_uuid.uuid4(), incarnation=_uuid.uuid4(),
                          request_id=_uuid.uuid4(), expires_at=timezone.now() + timedelta(hours=1))
        machine = User.objects.get(username='grant-' + row.id.hex)
        self.assertEqual(machine.kind, 'service')
        self.assertFalse(access.Principal.objects.filter(user=machine).exists())

    def test_user_admin_is_the_standard_one_and_bootstrap_still_works(self):
        from django.contrib import admin
        from django.contrib.auth.admin import UserAdmin
        from mop.server.controller.models import RestoreGuard
        RestoreGuard.objects.get_or_create(pk=1)
        User = get_user_model()
        adapter = admin.site._registry[User]
        self.assertIsInstance(adapter, UserAdmin)
        # create_admin штатно делает первого superuser на Actor.
        from mop.server.controller.runtime import initialize, create_admin
        initialize()
        create_admin('bootstrap-admin-683', 'a-long-distinct-passphrase-683')
        admin_user = User.objects.get(username='bootstrap-admin-683')
        self.assertTrue(admin_user.is_staff and admin_user.is_superuser)
        self.assertEqual(admin_user.kind, 'human')

    def test_swappable_fks_target_the_actor_everywhere(self):
        User = get_user_model()
        from mop.server.access import models as access
        from mop.server.operations import models as operations
        from mop.server.sessions import models as sessions
        for model, field in ((access.LoginIdentity, 'user'), (access.ProjectMembership, 'user'),
                             (operations.Operation, 'actor'), (sessions.Assignment, 'owner')):
            related = model._meta.get_field(field)
            self.assertEqual(related.related_model, User,
                             f'{model.__name__}.{field} должен ссылаться на Actor')
        # apps registry не знает второй user-модели: swappable auth.User
        # заменён, а не продублирован.
        self.assertEqual(get_user_model()._meta.label, 'actor.Actor')
