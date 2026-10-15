"""Контракты Access через настоящий Django runner и PostgreSQL."""
from django.contrib.auth import get_user_model
from django.test import Client, TransactionTestCase, override_settings


# FEATURE: #633 — authoritative Django login/token boundary.
# HYPOTHESIS: foundation публикует только health/Admin, auth endpoints отсутствуют.
# SOLUTION: один Django backend и credential-bound live Access evaluator.
@override_settings(ALLOWED_HOSTS=['testserver'])
class AuthenticationBoundary(TransactionTestCase):
    def test_login_requires_csrf_even_for_anonymous_browser(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post('/api/v1/auth/login',
            {'username': 'human', 'password': 'not-a-real-secret'}, secure=True)
        self.assertEqual(response.status_code, 403)

    def test_password_token_endpoint_returns_standard_token_only_explicitly(self):
        # User без Principal не получает credential; наличие Django row не identity.
        get_user_model().objects.create_user('unbound', password='test-password-only')
        response = Client().post('/api/v1/auth/token',
            {'username': 'unbound', 'password': 'test-password-only'}, secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('test-password-only', response.content.decode())

    def test_logout_is_post_only_and_credential_responses_are_no_store(self):
        response = Client().get('/api/v1/auth/logout', secure=True)
        self.assertEqual(response.status_code, 405)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
