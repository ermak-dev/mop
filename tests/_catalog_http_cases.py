"""Каталог DRF: общий application service, scope и CAS без Admin bypass."""
from django.test import Client, TransactionTestCase, override_settings
import _access_cases as access_fixture


@override_settings(ALLOWED_HOSTS=['testserver'])
class CatalogHTTP(TransactionTestCase):
    serialized_rollback = True
    setUp = access_fixture.AccessContracts.setUp
    permission = access_fixture.AccessContracts.permission

    def authenticated_client(self):
        return Client(HTTP_AUTHORIZATION='Token ' + self.token)

    def configure_permissions(self):
        for action in ('catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit'):
            self.permission(action)

    # HYPOTHESIS: REST публикует только GET, хотя общий Catalog уже владеет
    # конфигурацией. HTTP должен проверять тот же scope/CAS, а не писать ORM.
    # RED: PG17 — 8 failures (405) в пяти проверках.
    # SOLUTION: конечные serializers и вызов тех же Catalog commands, без ORM в view.
    # RESULT: PG16/17 — по 5/5 green, включая stale CAS, scope, staff и CSRF.
    # STATUS: FIXED — see #634
    def test_project_create_is_strict_and_uses_canonical_identity(self):
        from mop.server.catalog.models import Project
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        body = {'origin': 'https://example.invalid/new-project.git', 'puppet_limit': 3}
        response = self.authenticated_client().post('/api/v1/projects', body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 201, response.content)
        result = response.json()
        self.assertEqual((result['name'], result['puppet_limit'], result['revision']), ('new-project', 3, '2'))
        self.assertEqual(Project.objects.get(pk=result['id']).puppet_limit, 3)
        duplicate = self.authenticated_client().post('/api/v1/projects', body, content_type='application/json', secure=True)
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        self.assertEqual(Project.objects.filter(name='new-project').count(), 1)
        self.assertIn('no-store', response['Cache-Control'])

    def test_configuration_commits_then_stale_revision_is_conflict(self):
        self.configure_permissions()
        self.m.AllProjectMembership.objects.create(user=self.user, source=self.source)
        url = '/api/v1/projects/' + str(self.project.pk)
        body = {'origin': self.project.origin, 'puppet_limit': 4, 'expected_revision': '1'}
        response = self.authenticated_client().put(url, body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['revision'], '2')
        self.project.refresh_from_db()
        self.assertEqual(self.project.puppet_limit, 4)
        response = self.authenticated_client().put(url, dict(body, puppet_limit=9), content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 409, response.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.puppet_limit, 4)

    def test_configuration_cannot_cross_scope_or_use_staff_as_permission(self):
        self.configure_permissions()
        body = {'origin': self.foreign.origin, 'puppet_limit': 9, 'expected_revision': '1'}
        response = self.authenticated_client().put('/api/v1/projects/' + str(self.foreign.pk), body,
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 404, response.content)
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.puppet_limit)
        self.user.user_permissions.clear()
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        body['origin'] = self.project.origin
        response = self.authenticated_client().put('/api/v1/projects/' + str(self.project.pk), body,
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 403, response.content)

    def test_finite_input_rejects_unknown_fields_and_noncanonical_revision(self):
        self.configure_permissions()
        url = '/api/v1/projects/' + str(self.project.pk)
        body = {'origin': self.project.origin, 'puppet_limit': None, 'expected_revision': '1'}
        for invalid in (dict(body, retired_at='2026-01-01'), dict(body, expected_revision=True),
                        dict(body, expected_revision='01'), dict(body, expected_revision=1),
                        dict(body, puppet_limit=True)):
            with self.subTest(body=invalid):
                response = self.authenticated_client().put(url, invalid, content_type='application/json', secure=True)
                self.assertEqual(response.status_code, 400, response.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.revision, 1)

    def test_browser_mutation_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.s.bind_session(self.user.pk, client.session.session_key)
        response = client.put('/api/v1/projects/' + str(self.project.pk),
            {'origin': self.project.origin, 'puppet_limit': None, 'expected_revision': '1'},
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 403, response.content)
