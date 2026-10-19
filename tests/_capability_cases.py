"""#633: конечный scoped action catalogue на настоящем PostgreSQL 17.

HYPOTHESIS: identity описывает вид Actor, но не является каталогом действий;
отсутствующий authenticated query оставляет новых consumers без authority.
SPEC: один Access/Grants pipeline, явная область, живые права и no-store.
SOLUTION: явный query вызывает канонический authorizer дважды, без kind grants.
RESULT: исходные семь проверок падали на 404; девять проходят на PostgreSQL 17.
STATUS: FIXED — see #633
"""
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client, TransactionTestCase

import _rbac_unified_cases as rbac
import _delegation_cases as delegation
from mop.server.access import models as m, services as access
from mop.server.access.permissions import ACTION_PERMISSIONS


class CapabilityCatalogue(rbac.RbacBase):
    def query(self, token=None, **scope):
        return Client().get('/api/v1/capabilities', {'schema_version': '1', **scope},
            secure=True, HTTP_AUTHORIZATION='Token ' + (token or self.token))

    def actions(self, token=None, **scope):
        response = self.query(token, **scope)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        body = response.json()
        self.assertEqual(body['catalogue_version'], 1)
        self.assertEqual(body['scope'], dict(project_id=None, node_id=None, client_id=None, **{} ) | scope)
        self.assertEqual(body['actions'], sorted(set(body['actions'])))
        self.assertLessEqual(set(body['actions']), set(ACTION_PERMISSIONS))
        self.assertNotIn(self.token, response.content.decode())
        return body

    def test_authenticated_explicit_scope_and_current_epoch(self):
        from mop.server.controller.runtime import installation_snapshot
        response = Client().get('/api/v1/capabilities', secure=True)
        self.assertEqual(response.status_code, 401)
        body = self.actions(project_id=str(self.project.pk))
        self.assertEqual(body['epoch'], installation_snapshot()['epoch'])
        self.assertEqual(body['actor']['user_id'], self.human.pk)
        self.assertEqual(body['actions'], ['catalog.view_project'])
        self.assertEqual(self.actions()['actions'], [], 'no scope must not mean all projects')
        for scope in ({'project_id': str(self.foreign.pk)}, {'project_id': str(uuid4())},
                      {'node_id': str(uuid4())}, {'client_id': str(uuid4())}):
            self.assertEqual(self.actions(**scope)['actions'], [])

    def test_finite_query_rejects_unknown_duplicate_and_noncanonical_scope(self):
        for query in ('schema_version=2', 'schema_version=1&all=true', 'schema_version=1&project_id=no',
                      'schema_version=1&project_id=', 'schema_version=1&project_id=' + str(self.project.pk).upper(),
                      'schema_version=1&project_id=' + str(self.project.pk) + '&project_id=' + str(self.project.pk)):
            response = Client().get('/api/v1/capabilities?' + query,
                secure=True, HTTP_AUTHORIZATION='Token ' + self.token)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response['Cache-Control'], 'no-store')

    def test_human_agent_and_service_use_the_same_django_group_permission(self):
        node = self.node('catalogue-node')
        agent, actor = self.agent_actor('catalogue-agent')
        key = access.enroll_service('catalogue-service', node.pk,
            project_ids=(self.project.pk,), permissions=('catalog.observe_node',))
        service = get_user_model().objects.get(pk=access.token_context(key).user_id)
        group = Group.objects.create(name='catalogue-shared-rbac')
        group.permissions.add(Permission.objects.get(content_type__app_label='catalog', codename='view_project'))
        for user in (self.human, agent, service):
            user.user_permissions.clear()
            user.groups.add(group)
        for token in (self.token, access.issue_token(agent.pk), key):
            body = self.actions(token, project_id=str(self.project.pk), node_id=str(node.pk))
            self.assertEqual(body['actions'], ['catalog.view_project'])
        group.permissions.clear()
        for token in (self.token, access.issue_token(agent.pk), key):
            self.assertEqual(self.actions(token, project_id=str(self.project.pk), node_id=str(node.pk))['actions'], [])
        self.assertEqual(self.actions(key, project_id=str(self.project.pk))['actions'], [])
        m.ServiceEnrollment.objects.filter(actor=service).delete()
        self.assertEqual(self.query(key, node_id=str(node.pk)).status_code, 403)

    def test_scope_and_directory_revocation_are_not_kind_permissions(self):
        self.assertEqual(self.actions(project_id=str(self.project.pk))['actions'], ['catalog.view_project'])
        m.ProjectMembership.objects.filter(user=self.human).delete()
        self.assertEqual(self.actions(project_id=str(self.project.pk))['actions'], [])
        self.source.enabled = False
        self.source.save(update_fields=['enabled'])
        self.assertEqual(self.query(project_id=str(self.project.pk)).status_code, 403)

    def test_rechecks_permissions_and_actor_across_catalogue_query(self):
        original = access.Access.authorize
        removed = []
        def revoke(gate, actor, action, **scope):
            result = original(gate, actor, action, **scope)
            if action == 'catalog.view_project' and not removed:
                removed.append(True)
                self.human.user_permissions.clear()
            return result
        with patch.object(access.Access, 'authorize', revoke):
            self.assertEqual(self.actions(project_id=str(self.project.pk))['actions'], [])
        self.permit(self.human, 'catalog.view_project')
        current = access.current_actor
        calls = []
        def deactivate(actor, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                get_user_model().objects.filter(pk=self.human.pk).update(is_active=False)
            return current(actor, **kwargs)
        with patch.object(access, 'current_actor', deactivate):
            self.assertEqual(self.query(project_id=str(self.project.pk)).status_code, 403)

    def test_public_schema_uses_the_exact_finite_canonical_dictionary(self):
        self.assertEqual(self.query(project_id=str(self.project.pk)).status_code, 200)
        from mop.server.controller.openapi import schema
        document = schema()
        operation = document['paths']['/api/v1/capabilities']['get']
        self.assertEqual({p['name'] for p in operation['parameters']},
            {'schema_version', 'project_id', 'node_id', 'client_id'})
        schemas = document['components']['schemas']
        action = schemas['CapabilitiesReply']['properties']['actions']['items']
        if '$ref' in action:
            action = schemas[action['$ref'].rsplit('/', 1)[-1]]
        self.assertEqual(set(action['enum']), set(ACTION_PERMISSIONS))

    def test_client_ownership_conjunction_and_retired_project_narrow(self):
        from mop.server.catalog.models import ClientRegistration
        from mop.server.controller.models import Installation
        client = ClientRegistration.objects.create(label='catalogue-client', installation=Installation.objects.get())
        m.ClientOwnership.objects.create(client=client, user=self.human)
        self.permit(self.human, 'catalog.change_project')
        # configure_project — конъюнкция, не только её одноимённое право.
        body = self.actions(project_id=str(self.project.pk), client_id=str(client.pk))
        self.assertNotIn('catalog.configure_project', body['actions'])
        m.ClientOwnership.objects.filter(client=client).delete()
        self.assertEqual(self.actions(project_id=str(self.project.pk), client_id=str(client.pk))['actions'], [])
        from django.utils import timezone
        self.project.retired_at = timezone.now()
        self.project.identity_slot = self.project.pk
        self.project.save(update_fields=['retired_at', 'identity_slot'])
        self.assertEqual(self.actions(project_id=str(self.project.pk))['actions'], [])


class DelegatedCapabilities(TransactionTestCase):
    serialized_rollback = True
    setUp = delegation.DelegationCases.setUp
    permit = delegation.DelegationCases.permit
    publish = delegation.DelegationCases.publish
    register_subject = delegation.DelegationCases.register_subject
    reserve = delegation.DelegationCases.reserve
    delegate = delegation.DelegationCases.delegate

    def query_actions(self, actor, project=None):
        from mop.server.controller import catalogueviews
        with patch.object(catalogueviews, 'grant_authority', return_value=self.api.local_authority()):
            response = Client().get('/api/v1/capabilities', {'schema_version': '1', 'project_id': str((project or self.project).pk)},
                secure=True, HTTP_AUTHORIZATION='Token ' + self.key)
        return response

    def test_catalogue_revalidates_delegated_issuer_role_scope_and_legacy_service(self):
        target = uuid4()
        profile, agent, self.key = self.register_subject(target_id=target,
            permissions=('catalog.view_pool', 'execution.send_message'))
        row = self.delegate(target_id=target)
        actor = access.token_context(self.key, grants=self.api)
        # Первая проверка поведения отсутствующего endpoint, не import failure.
        response = Client().get('/api/v1/capabilities', secure=True, HTTP_AUTHORIZATION='Token ' + self.key)
        self.assertNotEqual(response.status_code, 404, 'authenticated action catalogue is missing')
        response = self.query_actions(actor)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('execution.send_message', response.json()['actions'])
        self.assertEqual(self.query_actions(actor, self.foreign).json()['actions'], [])
        approval = m.AgentDefinitionApproval.objects.get(definition_id=self.definition.id, human=self.user)
        # Удаление evidence намеренно обходит lifecycle только в собственной БД:
        # проверяем fail-closed чтение утраченного approval, не публичный delete.
        from django.db.models import QuerySet
        QuerySet(model=m.AgentDefinitionApproval).filter(pk=approval.pk).delete()
        self.assertEqual(self.query_actions(actor).status_code, 403)
        m.AgentDefinitionApproval.objects.create(definition_id=self.definition.id, human=self.user)
        delegation._revoke(self.user, 'execution.send_message')
        self.assertNotIn('execution.send_message', self.query_actions(actor).json()['actions'])
        self.api.revoke(self.actor, row.id, expected_revision=row.revision)
        self.assertEqual(self.query_actions(actor).status_code, 403)
        get_user_model().objects.filter(pk=agent.pk).update(kind='service')
        self.assertEqual(self.query_actions(actor).status_code, 403)

    def test_catalogue_follows_live_parent_permissions_and_revocation(self):
        target = uuid4()
        parent = self.publish('parent', spawn=('worker',))
        profile, parent_agent, parent_key = self.register_subject(target_id=target, definition=parent,
            permissions=('access.issue_grant', 'execution.create_puppet', 'execution.send_message'))
        parent_row = self.delegate(target_id=target, definition=parent)
        parent_actor = access.token_context(parent_key, grants=self.api)
        child_target = uuid4()
        profile, child_agent, self.key = self.register_subject(target_id=child_target,
            permissions=('execution.send_message',))
        self.delegate(actor=parent_actor, target_id=child_target)
        child = access.token_context(self.key, grants=self.api)
        response = Client().get('/api/v1/capabilities', secure=True, HTTP_AUTHORIZATION='Token ' + self.key)
        self.assertNotEqual(response.status_code, 404, 'authenticated action catalogue is missing')
        self.assertIn('execution.send_message', self.query_actions(child).json()['actions'])
        delegation._revoke(parent_agent, 'execution.send_message')
        self.assertNotIn('execution.send_message', self.query_actions(child).json()['actions'])
        self.api.revoke(self.actor, parent_row.id, expected_revision=parent_row.revision)
        self.assertEqual(self.query_actions(child).status_code, 403)
