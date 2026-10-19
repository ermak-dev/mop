"""Настоящий catalogue CAS через TLS; потеря ответа не становится receipt (#761)."""
import json

from django.test import TransactionTestCase, override_settings

import _access_cases as access_fixture
import _catalog_http_cases as catalog_fixture
import _control_client_cases as control_fixture
from _lib import run_command
from mop import cli
from mop.server.catalog.models import Project


@override_settings(ALLOWED_HOSTS=['localhost', 'testserver'])
class NativeProjectLimitClient(TransactionTestCase):
    serialized_rollback = True
    permission = access_fixture.AccessContracts.permission
    https = control_fixture.NativeControlClient.https

    def setUp(self):
        access_fixture.AccessContracts.setUp(self)
        catalog_fixture.CatalogHTTP.configure_permissions(self)
        self.m.AllProjectMembership.objects.create(user=self.user, source=self.source)

    def test_real_tls_limit_changes_only_desired_catalogue(self):
        from mop.server.operations.models import Operation
        with self.https() as (server, client):
            out, err, code = run_command(cli.main, ['project', 'limit', str(self.project.pk), '0',
                '--expected-revision', '1'])
            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertEqual((result['puppet_limit'], result['revision']), (0, '2'))
            self.assertEqual([method for method, _ in server.calls], ['GET', 'PUT'])
            self.assertEqual(client.project(str(self.project.pk)).puppet_limit, 0)
        self.project.refresh_from_db()
        self.assertEqual((self.project.puppet_limit, self.project.revision), (0, 2))
        self.assertEqual(Operation.objects.count(), 0)
        self.assertNotIn(self.token, out+err)

    def test_real_commit_then_lost_put_reply_is_unknown_without_retry(self):
        with self.https() as (server, _):
            server.drop_put = True
            out, err, code = run_command(cli.main, ['project', 'limit', str(self.project.pk), '3'])
            self.assertEqual(code, 3, err)
            self.assertEqual(out, '')
            self.assertIn('unknown', err)
            self.assertEqual([method for method, _ in server.calls], ['GET', 'PUT'])
        self.project.refresh_from_db()
        self.assertEqual((self.project.puppet_limit, self.project.revision), (3, 2))
        self.assertNotIn(self.token, err)

    def test_real_cas_conflict_after_get_does_not_replace_submitted_revision(self):
        with self.https() as (server, _):
            def changed(method, response):
                if method == 'GET' and response.status_code == 200:
                    Project.objects.filter(pk=self.project.pk).update(revision=2, puppet_limit=9)
            server.after = changed
            out, err, code = run_command(cli.main, ['project', 'limit', str(self.project.pk), '3'])
            self.assertEqual(code, 1, err)
            self.assertEqual(out, '')
            self.assertIn('catalog_conflict', err)
            self.assertEqual([method for method, _ in server.calls], ['GET', 'PUT'])
        self.project.refresh_from_db()
        self.assertEqual((self.project.puppet_limit, self.project.revision), (9, 2))

    def test_permission_revocation_between_read_and_put_keeps_catalogue_unchanged(self):
        with self.https() as (server, _):
            def revoked(method, response):
                if method == 'GET' and response.status_code == 200:
                    self.user.user_permissions.clear()
            server.after = revoked
            out, err, code = run_command(cli.main, ['project', 'limit', str(self.project.pk), '3'])
            self.assertEqual(code, 1, err)
            self.assertEqual(out, '')
            self.assertEqual([method for method, _ in server.calls], ['GET', 'PUT'])
        self.project.refresh_from_db()
        self.assertEqual((self.project.puppet_limit, self.project.revision), (None, 1))
        self.assertNotIn(self.token, err)
