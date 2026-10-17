"""#688 поведенческие контракты делегирования Actor→Actor без User-per-Grant.

HYPOTHESIS: выдача делегирования создавала отдельный service User и DRF Token
на каждую выдачу, допуск вёл параллельный capability-каталог, а вид Actor
маршрутизировал проверку. Делегирование обязано быть отношением между уже
существующими issuer и subject Actor с общим token субъекта.
SOLUTION: Grant — доменное имя делегирования: issuer (CreationIntent.actor) →
subject (существующий agent Actor, #686 register_agent того же owner).
Выдача/повтор/отзыв не создают и не удаляют ни Actor, ни token; эффективные
права — пересечение прав субъекта каждого звена, текущего human owner и живой
parent-цепочки через общий RBAC #685; ACTION_CAPABILITIES — только
техническое предусловие spawn; plain-контекста у субъекта делегирования нет.
RED (база 59f70c0f): reserve штампует grant-<hex> service User и Token —
'issuing a delegation must not create an Actor'.
RED (незавершённая правка до этой сессии): проверялся только субъект листа —
ребёнок получал право, которого нет у родителя; делегированный issuer выдавал
ребёнка без собственного issue_grant; после отзыва всех делегирований старый
plain-контекст субъекта снова действовал его правами; чужой human owner
делегировал зарегистрированного не им агента.

RED: 12 поведенческих отказов до реализации (субъект-Actor, цепочка прав, без User-per-Grant).
SOLUTION: Grant.subject — существующий agent Actor; _require_delegated — один RBAC на каждом
звене цепочки и у текущего human owner; ACTION_CAPABILITIES — технические предусловия программы.
RESULT: целевые группы 149/149; полный gate с realtime на Python 3.12 + PostgreSQL 17 — 761/761 OK.
STATUS: FIXED — see #688
"""
import json
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token

from mop.server.access import models as access, services as auth
from mop.server.access.context import ActorContext, ActorKind
from mop.server.access.permissions import permission_names

# Модулем, не классом: имя GrantCases в namespace заставило бы loader
# прогнать все его cases второй раз под этой меткой.
import _grant_cases as fixture
from _grant_cases import delegation_subject, permit_actions


def _revoke(user, *actions):
    for action in actions:
        for permission in permission_names(action):
            app, code = permission.split('.')
            user.user_permissions.remove(
                Permission.objects.get(content_type__app_label=app, codename=code))


class DelegationCases(TransactionTestCase):
    """Один субъект-Actor, несколько делегирований, один общий token.

    Окружение то же, что у GrantCases, но без неявной регистрации и без
    унаследованной capability-поверхности: права субъекта задаёт сам тест.
    """
    serialized_rollback = True
    setUp = fixture.GrantCases.setUp
    permit = fixture.GrantCases.permit
    publish = fixture.GrantCases.publish

    def register_subject(self, *, target_id=None, definition=None, permissions=()):
        """Существующий agent Actor через #686 register_agent, не через выдачу."""
        profile, agent, key = delegation_subject(self.api, self.actor, project_id=self.project.pk,
            definition_id=(definition or self.definition).id, target_id=target_id or uuid4(),
            actions=permissions)
        return profile, agent, key

    def reserve(self, *, target_id, definition=None, actor=None, **kw):
        """Выдача на уже зарегистрированного субъекта: identity здесь не заводится."""
        args = dict(project_id=self.project.pk, definition_id=(definition or self.definition).id,
            target_id=target_id, incarnation=uuid4(), request_id=uuid4(),
            expires_at=timezone.now() + timedelta(hours=1))
        args.update(kw)
        return self.api.reserve(actor or self.actor, **args)

    def delegate(self, *, target_id, definition=None, actor=None):
        """Активная делегировка; runtime binding target переходит на неё."""
        row = self.reserve(actor=actor, definition=definition, target_id=target_id)
        self.bindings.bindings[(self.project.pk, target_id)] = row.binding
        self.bindings.receipts[row.id] = row.binding
        self.api.provision(actor or self.actor, row.id, expected_revision=row.revision)
        self.api.activate(row.id, expected_revision=row.revision + 1)
        return self.api.get(self.actor, row.id)

    def resolvers(self):
        """Полный и SQL-only резолверы делегирования: один и тот же контракт."""
        return (self.api, self.api.local_authority())

    # FEATURE: #688 — делегирование не является разновидностью User.
    # RED: reserve создавал grant-<hex> service Actor и Token на каждую выдачу.
    def test_lifecycle_creates_neither_actor_nor_token_and_revocation_deletes_nothing(self):
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target,
            permissions=('execution.send_message',))
        actors = get_user_model().objects.count()
        tokens = Token.objects.count()
        row = self.delegate(target_id=target)
        self.assertEqual(get_user_model().objects.count(), actors,
            'issuing a delegation must not create an Actor')
        self.assertEqual(Token.objects.count(), tokens,
            'issuing a delegation must not create a token')
        self.assertEqual((row.subject_id, get_user_model().objects.get(pk=row.subject_id).kind),
            (agent.pk, 'agent'), 'the delegation subject is the registered agent Actor')
        self.assertFalse(get_user_model().objects.filter(username__startswith='grant-').exists(),
            'no grant-<hex> machine user may appear')
        revoked = self.api.revoke(self.actor, row.id, expected_revision=row.revision)
        self.assertEqual(revoked.state, 'revoked')
        self.assertTrue(Token.objects.filter(user=agent, key=key).exists(),
            'revocation must not delete or rotate the shared token')
        self.assertTrue(get_user_model().objects.filter(pk=agent.pk, kind='agent', is_active=True).exists(),
            'revocation must not delete or deactivate the subject Actor')

    # FEATURE: #688 — повтор намерения не создаёт второго субъекта.
    def test_intent_retry_returns_the_same_subject_without_new_identity(self):
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target)
        intent = dict(target_id=target, request_id=uuid4(), incarnation=uuid4(),
            expires_at=timezone.now() + timedelta(hours=1))
        first = self.reserve(**intent)
        actors = get_user_model().objects.count()
        second = self.reserve(**intent)
        self.assertEqual((first.id, first.subject_id), (second.id, agent.pk),
            'identical retry returns the same delegation and subject')
        with self.assertRaises(self.g.Conflict):
            self.reserve(**dict(intent, incarnation=uuid4()))
        self.assertEqual(get_user_model().objects.count(), actors,
            'a retry or a conflicting intent creates nothing')
        self.assertEqual(access.Grant.objects.filter(subject=agent).count(), 1)

    # FEATURE: #688 — один Actor с двумя независимыми делегированиями.
    # RED: OneToOne Actor на делегирование; token машины не различал строки.
    def test_two_independent_delegations_keep_one_actor_and_shared_token(self):
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target,
            permissions=('catalog.view_pool', 'execution.send_message'))
        first = self.delegate(target_id=target)
        second = self.delegate(target_id=target)
        self.assertEqual({first.subject_id, second.subject_id}, {agent.pk},
            'both delegations bind the same subject Actor')
        self.assertEqual(access.Grant.objects.filter(subject=agent, state='active').count(), 2)
        gate = auth.Access(grants=self.api)
        # Действующую делегировку выбирает текущий runtime binding target.
        actor = auth.token_context(key, grants=self.api)
        self.assertEqual(actor.grant_id, second.id)
        gate.authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))
        self.bindings.bindings[(self.project.pk, target)] = first.binding
        actor = auth.token_context(key, grants=self.api)
        self.assertEqual(actor.grant_id, first.id, 'the first delegation is still alive on its own')
        # Отзыв текущей не переключает субъекта на другую активную строку.
        self.api.revoke(self.actor, first.id, expected_revision=first.revision)
        with self.assertRaises(auth.Denied):
            auth.token_context(key, grants=self.api)
        with self.assertRaises(auth.Denied):
            gate.authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))
        # Вторая независима: её binding снова текущий — она действует.
        self.bindings.bindings[(self.project.pk, target)] = second.binding
        again = auth.token_context(key, grants=self.api)
        self.assertEqual((again.user_id, again.grant_id), (agent.pk, second.id))
        gate.authorize(again, 'execution.send_message', project_ids=(self.project.pk,))
        self.api.revoke(self.actor, second.id, expected_revision=second.revision)
        with self.assertRaises(auth.Denied):
            auth.token_context(key, grants=self.api)
        self.assertTrue(Token.objects.filter(user=agent, key=key).exists(),
            'the shared token survives revocation of every delegation')
        self.assertTrue(get_user_model().objects.filter(pk=agent.pk, is_active=True).exists())

    # FEATURE: #688 — эффективные права: пересечение субъекта, issuer и scope.
    # RED: допуск вёл capability-каталог; права субъекта не проверялись вовсе.
    def test_effective_rights_are_the_intersection_of_subject_issuer_and_scope(self):
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target,
            permissions=('catalog.view_pool', 'execution.send_message', 'execution.wipe_puppet'))
        self.delegate(target_id=target)
        for resolver in self.resolvers():
            gate = auth.Access(grants=resolver)
            actor = auth.token_context(key, grants=resolver)
            gate.authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))
            # Текущее право issuer снято — делегирование сужено до отказа.
            _revoke(self.user, 'execution.send_message')
            with self.assertRaises(auth.Denied):
                gate.authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))
            self.permit('execution.send_message')
            # Права нет у субъекта — делегирование не расширяет subject rights.
            _revoke(agent, 'execution.send_message')
            with self.assertRaises(auth.Denied):
                gate.authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))
            permit_actions(agent, 'execution.send_message')
            gate.authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))
            # Право есть только у субъекта — шире issuer запрещено.
            with self.assertRaises(auth.Denied):
                gate.authorize(actor, 'execution.wipe_puppet', project_ids=(self.project.pk,))
            # Деактивация issuer закрывает делегирование.
            get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
            with self.assertRaises(auth.Denied):
                gate.authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))
            get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
            # Деактивация субъекта закрывает контекст целиком.
            get_user_model().objects.filter(pk=agent.pk).update(is_active=False)
            with self.assertRaises(auth.Denied):
                auth.token_context(key, grants=resolver)
            get_user_model().objects.filter(pk=agent.pk).update(is_active=True)
            # Чужой проект и узел вне области делегирования.
            with self.assertRaises(auth.Denied):
                gate.authorize(actor, 'catalog.view_pool', project_ids=(self.foreign.pk,))
            with self.assertRaises(auth.Denied):
                gate.authorize(actor, 'catalog.view_pool', project_ids=(self.project.pk,),
                               node_ids=(uuid4(),))
            # Без привязанного резолвера делегированный контекст закрыт.
            with self.assertRaises(auth.Denied):
                auth.Access().authorize(actor, 'execution.send_message', project_ids=(self.project.pk,))

    # FEATURE: #688 — ребёнок не шире родителя: право нужно субъекту каждого звена.
    # RED: проверялся только субъект листа; ребёнок получал право, которого
    # нет у субъекта родителя (его issuer).
    def test_child_is_never_wider_than_its_parent_subject(self):
        self.permit('execution.wipe_puppet')
        parent = self.publish('parent', spawn=('worker',))
        target = uuid4()
        profile, parent_agent, parent_key = self.register_subject(target_id=target, definition=parent,
            permissions=('access.issue_grant', 'execution.create_puppet', 'execution.send_message'))
        self.delegate(target_id=target, definition=parent)
        parent_actor = auth.token_context(parent_key, grants=self.api)
        child_target = uuid4()
        child_profile, child_agent, child_key = self.register_subject(target_id=child_target,
            permissions=('execution.send_message', 'execution.wipe_puppet'))
        self.delegate(actor=parent_actor, target_id=child_target)
        for resolver in self.resolvers():
            gate = auth.Access(grants=resolver)
            child = auth.token_context(child_key, grants=resolver)
            gate.authorize(child, 'execution.send_message', project_ids=(self.project.pk,))
            with self.assertRaises(auth.Denied, msg='child gained a right its parent subject lacks'):
                gate.authorize(child, 'execution.wipe_puppet', project_ids=(self.project.pk,))
            permit_actions(parent_agent, 'execution.wipe_puppet')
            gate.authorize(child, 'execution.wipe_puppet', project_ids=(self.project.pk,))
            # Право родителя снято позже — ребёнок сужается немедленно.
            _revoke(parent_agent, 'execution.send_message')
            with self.assertRaises(auth.Denied):
                gate.authorize(child, 'execution.send_message', project_ids=(self.project.pk,))
            permit_actions(parent_agent, 'execution.send_message')
            _revoke(parent_agent, 'execution.wipe_puppet')

    # FEATURE: #688 — делегированный issuer выдаёт ребёнка только своими правами.
    # RED: reserve/provision ребёнка проверяли лишь human owner; субъект
    # родителя без issue_grant выдавал делегирование.
    def test_delegated_issuer_needs_its_own_issue_and_create_rights(self):
        parent = self.publish('parent', spawn=('worker',))
        target = uuid4()
        profile, parent_agent, parent_key = self.register_subject(target_id=target, definition=parent,
            permissions=('execution.create_puppet', 'execution.send_message'))
        self.delegate(target_id=target, definition=parent)
        parent_actor = auth.token_context(parent_key, grants=self.api)
        child_target = uuid4()
        self.register_subject(target_id=child_target, permissions=('execution.send_message',))
        actors, grants = get_user_model().objects.count(), access.Grant.objects.count()
        with self.assertRaises(auth.Denied, msg='delegated issuance without the subject issue right'):
            self.reserve(actor=parent_actor, target_id=child_target)
        self.assertEqual((get_user_model().objects.count(), access.Grant.objects.count()), (actors, grants))
        permit_actions(parent_agent, 'access.issue_grant')
        child = self.reserve(actor=parent_actor, target_id=child_target)
        # Право создания снято до provision — повторная проверка issuer отказывает.
        _revoke(parent_agent, 'execution.create_puppet')
        with self.assertRaises(auth.Denied):
            self.api.provision(parent_actor, child.id, expected_revision=child.revision)
        self.assertIsNone(access.Grant.objects.get(pk=child.id).credential_version_id)

    # FEATURE: #688 — живая parent-цепочка: отзыв родителя закрывает ребёнка,
    # роль вне spawn-предусловия родителя отказана.
    def test_parent_revoke_closes_child_and_wider_child_is_refused(self):
        target = uuid4()
        parent = self.publish('parent', spawn=('worker',))
        profile, agent, key = self.register_subject(target_id=target, definition=parent,
            permissions=('access.issue_grant', 'execution.create_puppet', 'execution.send_message'))
        parent_row = self.delegate(target_id=target, definition=parent)
        parent_actor = auth.token_context(key, grants=self.api)
        child_target = uuid4()
        child_profile, child_agent, child_key = self.register_subject(target_id=child_target,
            permissions=('execution.send_message',))
        child = self.delegate(actor=parent_actor, target_id=child_target)
        self.assertEqual(child.subject_id, child_agent.pk, 'the child binds its own target subject')
        self.assertEqual(child.parent_id, parent_row.id)
        child_actor = auth.token_context(child_key, grants=self.api.local_authority())
        auth.Access(grants=self.api.local_authority()).authorize(child_actor,
            'execution.send_message', project_ids=(self.project.pk,))
        # Роль вне spawn allowlist родителя отказана до записи, даже когда
        # её субъект зарегистрирован и права у всех есть.
        rogue = self.publish('rogue')
        rogue_target = uuid4()
        self.register_subject(target_id=rogue_target, definition=rogue,
            permissions=('execution.send_message',))
        grants = access.Grant.objects.count()
        with self.assertRaisesRegex(auth.Denied, 'spawn'):
            self.reserve(actor=parent_actor, target_id=rogue_target, definition=rogue)
        self.assertEqual(access.Grant.objects.count(), grants)
        # Отзыв родителя закрывает контекст ребёнка, но не его identity.
        self.api.revoke(self.actor, parent_row.id, expected_revision=parent_row.revision)
        for resolver in self.resolvers():
            with self.assertRaises(auth.Denied):
                auth.token_context(child_key, grants=resolver)
        self.assertTrue(Token.objects.filter(user=child_agent, key=child_key).exists())
        self.assertTrue(get_user_model().objects.filter(pk=child_agent.pk, is_active=True).exists())

    # FEATURE: #688 — словарь предусловий не является источником прав.
    # RED: authorize отказывал 'outside pinned delegated capabilities' даже при
    # правах субъекта и issuer.
    def test_capability_preconditions_are_not_a_permission_source(self):
        raw = ('# no verbs\n' + json.dumps({'soul': 'noverbs', 'version': 1,
            'runtime': {'driver': 'any', 'receives_dispatch': True},
            'capabilities': {'node_verbs': [], 'cluster_verbs': [], 'spawn': ['worker'], 'dispatch': False},
            'prompt': 'soul.md'})).encode()
        empty = self.api.publish_definition(self.actor, project_id=self.project.pk,
            manifest=raw, prompt=b'prompt', provenance='test')
        self.api.approve_definition(self.actor, empty.id)
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target, definition=empty,
            permissions=('execution.send_message', 'access.issue_grant', 'execution.create_puppet'))
        self.delegate(target_id=target, definition=empty)
        for resolver in self.resolvers():
            actor = auth.token_context(key, grants=resolver)
            # Программа без verb всё равно действует: допуск даёт общий RBAC.
            auth.Access(grants=resolver).authorize(actor, 'execution.send_message',
                project_ids=(self.project.pk,))
        # Техническое предусловие остаётся: программа без cluster add не
        # порождает детей даже при полном RBAC всей цепочки.
        child_target = uuid4()
        self.register_subject(target_id=child_target, permissions=('execution.send_message',))
        with self.assertRaisesRegex(auth.Denied, 'spawn'):
            self.reserve(actor=auth.token_context(key, grants=self.api), target_id=child_target)

    # FEATURE: #688 — stale context после ротации и подмена holder.
    # RED: _token_actor выбирал делегирование по user машины делегирования.
    def test_stale_context_after_rotation_adopts_no_new_identity(self):
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target,
            permissions=('execution.send_message',))
        row = self.delegate(target_id=target)
        resolver = self.api.local_authority()
        gate = auth.Access(grants=resolver)
        old = auth.token_context(key, grants=resolver)
        # Ротация общего token субъекта — отдельное событие credential Actor.
        Token.objects.filter(user=agent).delete()
        access.CredentialBinding.objects.filter(token_user=agent).delete()
        fresh = auth.issue_token(agent.pk)
        for stale in (lambda: auth.token_context(key, grants=resolver),
                      lambda: auth.current_actor(old, grants=resolver),
                      lambda: auth.current_actor(old, grants=self.api),
                      lambda: gate.authorize(old, 'execution.send_message', project_ids=(self.project.pk,))):
            with self.assertRaises(auth.Denied, msg='stale context adopted the rotated credential'):
                stale()
        current = auth.token_context(fresh, grants=resolver)
        self.assertEqual((current.user_id, current.grant_id), (agent.pk, row.id))
        self.assertNotEqual(current.credential_id, old.credential_id)
        self.assertEqual(access.Grant.objects.get(pk=row.id).state, 'active',
            'token rotation is not a delegation revocation')

    # FEATURE: #688 — чужая цель, чужой owner, чужой проект и forged holder.
    # RED: reserve проверял лишь существование профиля target — другой human
    # owner делегировал агента, зарегистрированного не им.
    def test_foreign_owner_target_project_and_forged_holder_are_refused(self):
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target,
            permissions=('execution.send_message',))
        other = get_user_model().objects.create_user('other-owner', password='test-only-password')
        access.LoginIdentity.objects.create(user=other, source=self.source, external_subject='other-owner')
        access.ProjectMembership.objects.create(user=other, project=self.project, source=self.source)
        permit_actions(other, 'access.view_role', 'access.approve_role', 'access.issue_grant',
            'execution.create_puppet', 'execution.send_message')
        other_actor = auth.token_context(auth.issue_token(other.pk))
        raw = ('# other\n' + json.dumps({'soul': 'worker', 'version': 1,
            'runtime': {'driver': 'any', 'receives_dispatch': True},
            'capabilities': {'node_verbs': ['send'], 'cluster_verbs': [], 'spawn': [], 'dispatch': False},
            'prompt': 'soul.md'})).encode()
        other_definition = self.api.publish_definition(other_actor, project_id=self.project.pk,
            manifest=raw, prompt=b'other prompt', provenance='test')
        self.api.approve_definition(other_actor, other_definition.id)
        self.approvals.proof = self.g.ApprovedPolicy(uuid4(), self.proof.version_id, 'b' * 64,
                                                     other.pk, self.project.pk)
        grants = access.Grant.objects.count()
        with self.assertRaises(auth.NotVisible, msg='another owner delegated a foreign agent'):
            self.reserve(actor=other_actor, target_id=target, definition=other_definition)
        with self.assertRaises(auth.NotVisible, msg='an unregistered target became a subject'):
            self.reserve(actor=other_actor, target_id=uuid4(), definition=other_definition)
        self.assertEqual(access.Grant.objects.count(), grants)
        self.approvals.proof = self.proof
        with self.assertRaises(PermissionError):
            self.reserve(target_id=target, project_id=self.foreign.pk)
        # Forged holder: контекст субъекта, называющий чужое делегирование.
        row = self.delegate(target_id=target)
        second_target = uuid4()
        second_profile, second_agent, second_key = self.register_subject(target_id=second_target,
            permissions=('execution.send_message',))
        second = self.delegate(target_id=second_target)
        actor = auth.token_context(key, grants=self.api)
        forged = ActorContext(actor.user_id, ActorKind.AGENT, actor.credential_id,
                              actor.human_owner_id, actor.node_id, second.id)
        for resolver in self.resolvers():
            with self.assertRaises(auth.Denied):
                auth.current_actor(forged, grants=resolver)
            with self.assertRaises(auth.Denied):
                auth.Access(grants=resolver).authorize(forged, 'execution.send_message',
                    project_ids=(self.project.pk,))
        self.assertEqual(auth.token_context(second_key, grants=self.api).grant_id, second.id)
        self.assertEqual(actor.grant_id, row.id)

    # FEATURE: #688 — выбранный контекст не обходит fallback к правам Actor.
    # RED: после отзыва всех делегирований plain-контекст, полученный до
    # первой выдачи, снова действовал собственными правами субъекта.
    def test_delegated_subject_never_falls_back_to_plain_actor_rights(self):
        target = uuid4()
        profile, agent, key = self.register_subject(target_id=target,
            permissions=('catalog.view_pool',))
        plain = auth.token_context(key)
        self.assertIsNone(plain.grant_id)
        auth.Access().authorize(plain, 'catalog.view_pool', project_ids=(self.project.pk,))
        row = self.delegate(target_id=target)

        def closed():
            with self.assertRaises(auth.Denied):
                auth.token_context(key)
            for gate in (auth.Access(), auth.Access(grants=self.api)):
                with self.assertRaises(auth.Denied, msg='plain subject context bypassed delegation'):
                    gate.authorize(plain, 'catalog.view_pool', project_ids=(self.project.pk,))
            with self.assertRaises(auth.Denied):
                auth.current_actor(plain)
            with self.assertRaises(auth.Denied):
                auth.visible_project_ids(plain)
        closed()
        actor = auth.token_context(key, grants=self.api.local_authority())
        auth.Access(grants=self.api.local_authority()).authorize(actor,
            'catalog.view_pool', project_ids=(self.project.pk,))
        # Отзыв последнего делегирования не открывает plain path субъекта.
        self.api.revoke(self.actor, row.id, expected_revision=row.revision)
        with self.assertRaises(auth.Denied):
            auth.token_context(key, grants=self.api.local_authority())
        closed()

    # FEATURE: #688 — expiry закрывает только свою строку.
    def test_expiry_closes_only_its_own_delegation(self):
        target, other_target = uuid4(), uuid4()
        profile, agent, key = self.register_subject(target_id=target,
            permissions=('execution.send_message',))
        other_profile, other, other_key = self.register_subject(target_id=other_target,
            permissions=('execution.send_message',))
        row = self.delegate(target_id=target)
        kept = self.delegate(target_id=other_target)
        access.CreationIntent._base_manager.filter(
            pk=access.Grant.objects.get(pk=row.id).intent_id).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        for resolver in self.resolvers():
            with self.assertRaises(auth.Denied):
                auth.token_context(key, grants=resolver)
            actor = auth.token_context(other_key, grants=resolver)
            self.assertEqual(actor.grant_id, kept.id)
            auth.Access(grants=resolver).authorize(actor, 'execution.send_message',
                project_ids=(self.project.pk,))
        expired = self.api.expire(row.id, expected_revision=row.revision)
        self.assertEqual(expired.state, 'expired')
        self.assertTrue(Token.objects.filter(user=agent, key=key).exists())
        self.assertTrue(get_user_model().objects.filter(pk=agent.pk, is_active=True).exists())
