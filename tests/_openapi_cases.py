"""HTTP-граница schema endpoint #696: staff-only, никогда anonymous."""
from django.test import Client, TransactionTestCase, override_settings

import _access_cases as access_fixture


@override_settings(ALLOWED_HOSTS=['testserver'])
class SchemaEndpoint(TransactionTestCase):
    serialized_rollback = True
    # setUp -- граница фикстуры доступа, но её permission() вешает права
    # на пользователя: класс целиком, чтобы хелпер приезжал вместе с ней.
    setUp = access_fixture.AccessContracts.setUp
    permission = access_fixture.AccessContracts.permission

    # HYPOTHESIS: endpoint публикует публичный контракт без аутентификации.
    # RED: маршрута нет -- 404 вместо отказа anonymous.
    # SOLUTION: staff-only SpectacularAPIView с CurrentActorRequired и
    #   is_staff permission; сам маршрут из генерации исключён.
    def test_anonymous_is_refused_not_served(self):
        response = Client().get('/api/v1/schema', secure=True)
        self.assertEqual(response.status_code, 401, response.content)
        body = response.json()
        self.assertEqual(body['error']['code'], 'authentication_required')
        self.assertEqual(body['schema_version'], 1)

    def test_authenticated_non_staff_is_refused(self):
        response = Client(HTTP_AUTHORIZATION='Token ' + self.token).get('/api/v1/schema', secure=True)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()['error']['code'], 'permission_denied')

    def test_staff_gets_the_public_schema(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        response = Client(HTTP_AUTHORIZATION='Token ' + self.token).get('/api/v1/schema', secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        import yaml
        document = yaml.safe_load(response.content)
        self.assertEqual(document['openapi'], '3.0.3')
        self.assertIn('/api/v1/projects', document['paths'])
        self.assertNotIn('/api/v1/machine/work', document['paths'])
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
