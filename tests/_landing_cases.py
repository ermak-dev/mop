"""Landing #638: конечный SQL-only API и независимые PostgreSQL connections."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.test import Client, TransactionTestCase, override_settings

import _access_cases as access_fixture


# HYPOTHESIS: Landing уже хранит человеческий CAS, но публичный query/command
# отсутствует. GET не материализует строку, команда фиксируется настоящим COMMIT.
# SPEC: текущие canonical permissions, epoch fence, только свой human owner;
# потерянный ответ сверяется чтением, не повторным приобретением.
# RED: маршруты отвечали 404; release materialized rev0 затем выявил 400 вместо 409.
# SOLUTION: finite controller binding с собственным COMMIT, epoch fence и domain CAS.
# RESULT: 15 Landing + 38 соседних контрактов зелёные на PostgreSQL 17.
# STATUS: FIXED — see #638
@override_settings(ALLOWED_HOSTS=['testserver'])
class LandingHTTP(TransactionTestCase):
    serialized_rollback = True
    setUp = access_fixture.AccessContracts.setUp
    permission = access_fixture.AccessContracts.permission

    def url(self, project=None, command=None):
        value = '/api/v1/projects/' + str(project or self.project.pk) + '/landing'
        return value + '/commands/' + command if command else value

    def http(self, token=None):
        return Client(HTTP_AUTHORIZATION='Token ' + (token or self.token))

    def allow(self):
        for action in ('view_landing', 'acquire_landing', 'release_landing'):
            self.permission('execution.' + action)

    def epoch(self):
        from mop.server.controller.models import Installation
        return str(Installation.objects.get().epoch)

    def body(self, revision='0', **extra):
        return dict(schema_version=1, epoch=self.epoch(), expected_revision=revision, **extra)

    def post(self, command, body=None, *, token=None, project=None):
        return self.http(token).post(self.url(project, command), body or self.body(),
            content_type='application/json', secure=True)

    def landing(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('no-store', response['Cache-Control'])
        value = response.json()
        self.assertEqual(set(value), {'schema_version', 'epoch', 'landing'})
        self.assertEqual((value['schema_version'], value['epoch']), (1, self.epoch()))
        self.assertEqual(set(value['landing']), {'project_id', 'state', 'revision', 'generation', 'owner_id'})
        return value['landing']

    def other(self, kind='human'):
        user = get_user_model().objects.create_user('other', kind=kind)
        self.m.LoginIdentity.objects.create(user=user, source=self.source, external_subject='other')
        self.m.ProjectMembership.objects.create(user=user, source=self.source, project=self.project)
        user.user_permissions.set(self.user.user_permissions.all())
        return user, self.s.issue_token(user.pk)

    def test_get_absent_is_explicit_unmaterialized_revision_zero_without_writes(self):
        from mop.server.sessions.models import LandingToken
        self.allow()
        value = self.landing(self.http().get(self.url(), secure=True))
        self.assertEqual(value, dict(project_id=str(self.project.pk), state='unmaterialized',
            revision='0', generation=None, owner_id=None))
        self.assertFalse(LandingToken.objects.exists())

    def test_take_release_and_read_preserve_human_owner_and_exact_cas(self):
        self.allow()
        held = self.landing(self.post('take'))
        self.assertEqual((held['state'], held['revision'], held['generation'], held['owner_id']),
                         ('held', '1', '1', self.user.pk))
        self.assertEqual(self.landing(self.http().get(self.url(), secure=True)), held)
        # Ни повторный take с тем же owner, ни stale take не выдаёт ложный grant.
        for revision in ('0', '1'):
            self.assertEqual(self.post('take', self.body(revision)).status_code, 409)
        self.assertEqual(self.post('release', self.body('0')).status_code, 409)
        free = self.landing(self.post('release', self.body('1')))
        self.assertEqual((free['state'], free['revision'], free['generation'], free['owner_id']),
                         ('free', '2', '2', None))
        again = self.landing(self.post('take', self.body('2')))
        self.assertEqual((again['revision'], again['generation'], again['owner_id']), ('3', '3', self.user.pk))
        self.assertEqual(self.post('release', self.body('1')).status_code, 409)
        self.assertEqual(self.landing(self.http().get(self.url(), secure=True)), again)

    def test_other_human_cannot_release_or_force_take_even_with_all_landing_permissions(self):
        self.allow()
        self.landing(self.post('take'))
        _, key = self.other()
        self.assertEqual(self.post('release', self.body('1'), token=key).status_code, 403)
        self.assertEqual(self.post('take', self.body('1'), token=key).status_code, 409)
        self.assertEqual(self.post('take', self.body('1', force=True), token=key).status_code, 400)
        self.assertEqual(self.landing(self.http().get(self.url(), secure=True))['owner_id'], self.user.pk)

    def test_agent_permission_does_not_make_it_a_human_owner(self):
        self.allow()
        _, key = self.other('agent')
        self.assertEqual(self.post('take', token=key).status_code, 403)
        self.assertEqual(self.post('release', token=key).status_code, 403)
        self.assertEqual(self.http(key).get(self.url(), secure=True).status_code, 200)

    def test_scope_unknown_and_retired_project_are_not_visible_and_never_create_token(self):
        from django.utils import timezone
        from mop.server.sessions.models import LandingToken
        self.allow()
        for project in (self.foreign.pk, uuid4()):
            self.assertEqual(self.http().get(self.url(project), secure=True).status_code, 404)
            self.assertEqual(self.post('take', project=project).status_code, 404)
        self.project.retired_at, self.project.identity_slot = timezone.now(), self.project.pk
        self.project.save(update_fields=['retired_at', 'identity_slot'])
        self.assertEqual(self.http().get(self.url(), secure=True).status_code, 404)
        self.assertEqual(self.post('take').status_code, 404)
        self.assertFalse(LandingToken.objects.exists())

    def test_permissions_are_separate_current_and_staff_is_not_authority(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.assertEqual(self.http().get(self.url(), secure=True).status_code, 403)
        self.assertEqual(self.post('take').status_code, 403)
        self.permission('execution.acquire_landing')
        self.landing(self.post('take'))
        self.assertEqual(self.http().get(self.url(), secure=True).status_code, 403)
        self.assertEqual(self.post('release', self.body('1')).status_code, 403)
        self.permission('execution.view_landing')
        self.assertEqual(self.http().get(self.url(), secure=True).status_code, 200)
        self.user.user_permissions.clear()
        self.assertEqual(self.http().get(self.url(), secure=True).status_code, 403)
        self.assertEqual(self.post('take', self.body('1')).status_code, 403)

    def test_saved_actor_revocation_cannot_release(self):
        self.allow()
        self.landing(self.post('take'))
        self.s.revoke_token(self.actor)
        response = self.post('release', self.body('1'))
        self.assertIn(response.status_code, (401, 403))
        from mop.server.controller.landingbinding import landing_service
        with self.assertRaises(self.s.Denied):
            landing_service().release(self.actor, self.project.pk, epoch=uuid4(), expected_revision=1)

    def test_release_absent_is_conflict_without_materialization(self):
        from mop.server.sessions.models import LandingToken
        self.allow()
        self.assertEqual(self.post('release').status_code, 409)
        self.assertFalse(LandingToken.objects.exists())

    def test_finite_body_and_query_cannot_smuggle_mutations_or_owners(self):
        from mop.server.sessions.models import LandingToken
        self.allow()
        body = self.body()
        invalid = [dict(body, expected_revision=v) for v in (True, 0, '00', '-1', '9223372036854775808')]
        invalid += [dict(body, schema_version=v) for v in (True, '1', 2)]
        invalid += [dict(body, epoch=self.epoch().upper()), dict(body, owner_id=self.user.pk),
            dict(body, puppet='pu-one-1'), dict(body, request_id=str(uuid4())), dict(body, force=True),
            {k:v for k,v in body.items() if k != 'epoch'}]
        for value in invalid:
            with self.subTest(body=value):
                self.assertEqual(self.post('take', value).status_code, 400)
        duplicate = ('{"schema_version":1,"epoch":"' + self.epoch() +
                     '","expected_revision":"1","expected_revision":"0"}')
        self.assertEqual(self.http().post(self.url(command='take'), duplicate,
            content_type='application/json', secure=True).status_code, 400)
        for query in ('?action=take', '?force=true', '?epoch=' + self.epoch() + '&epoch=' + self.epoch(), '?cursor=x'):
            self.assertEqual(self.http().get(self.url() + query, secure=True).status_code, 400)
        self.assertEqual(self.http().post(self.url(command='take') + '?force=true', body,
            content_type='application/json', secure=True).status_code, 400)
        self.assertFalse(LandingToken.objects.exists())

    def test_epoch_and_restore_fence_before_any_mutation(self):
        from mop.server.controller.models import Installation, RestoreGuard
        from mop.server.sessions.models import LandingToken
        self.allow()
        stale = self.body()
        Installation.objects.update(epoch=uuid4())
        self.assertEqual(self.post('take', stale).status_code, 409)
        self.assertEqual(self.http().get(self.url() + '?epoch=' + stale['epoch'], secure=True).status_code, 409)
        RestoreGuard.objects.update(state='restoring')
        response = self.post('take')
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(self.http().get(self.url(), secure=True).status_code, 503)
        self.assertFalse(LandingToken.objects.exists())

    def test_browser_requires_csrf_and_get_is_only_query_method(self):
        self.allow()
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        self.s.bind_session(self.user.pk, browser.session.session_key)
        self.assertEqual(browser.post(self.url(command='take'), self.body(),
            content_type='application/json', secure=True).status_code, 403)
        self.assertEqual(self.http().get(self.url(command='take'), secure=True).status_code, 405)
        self.assertEqual(self.http().post(self.url(), self.body(),
            content_type='application/json', secure=True).status_code, 405)

    def test_public_commands_reject_outer_transaction_and_disabled_autocommit(self):
        from mop.server.controller.landingbinding import landing_service
        from mop.server.sessions.services import Unavailable
        self.allow()
        api = landing_service()
        epoch = uuid4()
        with transaction.atomic():
            with self.assertRaisesRegex(Unavailable, 'surrounding transaction'):
                api.acquire(self.actor, self.project.pk, epoch=epoch, expected_revision=0)
        transaction.set_autocommit(False)
        try:
            with self.assertRaisesRegex(Unavailable, 'surrounding transaction'):
                api.release(self.actor, self.project.pk, epoch=epoch, expected_revision=1)
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)

    def test_committed_command_is_visible_on_independent_connection_before_return(self):
        from mop.server.sessions.models import LandingToken
        self.allow()
        held = self.landing(self.post('take'))
        def observe():
            try:
                return LandingToken.objects.get(project=self.project.pk).owner_id
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(observe).result(timeout=10), held['owner_id'])

    def test_independent_concurrent_acquire_has_one_winner_and_late_release_cannot_touch_new_owner(self):
        from mop.server.sessions.models import LandingToken
        self.allow()
        other, key = self.other()
        barrier = Barrier(2)
        body = self.body()
        def acquire(token):
            try:
                barrier.wait(timeout=10)
                response = Client(HTTP_AUTHORIZATION='Token ' + token).post(self.url(command='take'),
                    body, content_type='application/json', secure=True)
                return token, response.status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, (self.token, key)))
        self.assertEqual(sorted(status for _,status in results), [200, 409])
        winner = next(token for token,status in results if status == 200)
        loser = key if winner == self.token else self.token
        self.landing(self.post('release', self.body('1'), token=winner))
        self.landing(self.post('take', self.body('2'), token=loser))
        self.assertEqual(self.post('release', self.body('1'), token=winner).status_code, 409)
        row = LandingToken.objects.get(project=self.project)
        self.assertEqual((row.revision, row.owner_id), (3, other.pk if loser == key else self.user.pk))

    def test_raw_database_errors_are_safe_and_uncertain_commit_is_not_retryable(self):
        from django.db import OperationalError
        self.allow()
        for unknown in (False, True):
            cause = RuntimeError('private-database-password')
            cause.outcome_unknown = unknown
            error = OperationalError('private-database-password')
            error.__cause__ = cause
            with patch('mop.server.sessions.services.Landing.acquire', side_effect=error):
                response = self.post('take')
            self.assertEqual(response.status_code, 503, response.content)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotIn('private-database-password', response.content.decode())
            self.assertEqual(response.json()['error']['retryable'], not unknown)
            self.assertEqual(response.json()['error']['code'],
                             'database_outcome_unknown' if unknown else 'database_unavailable')
