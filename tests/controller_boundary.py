"""Материализованная sync/async-граница; без запросов к БД."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _lib  # noqa: F401,E402 — lifetime временных файлов
import hermetic  # noqa: E402 — до framework и mop

# HYPOTHESIS: штатный sync_to_async переносит живые модели и QuerySet.
# Эти проверки не заменяют PostgreSQL lifecycle/concurrency gate.
def main():
    if importlib.util.find_spec('django') is None:
        import hermetic
        hermetic.skip('controller boundary', 'declared server extra is not installed')
        return
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        key = path / 'django.key'
        key.write_text('a-private-test-key-' * 5)
        key.chmod(0o600)
        bootstrap = path / 'bootstrap.json'
        bootstrap.write_text(json.dumps({
            'installation_id': 'boundary-test', 'role': 'web', 'django_key': str(key),
            'database': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'unused',
                         'HOST': 'unused.invalid', 'PORT': '5432', 'USER': 'unused',
                         'PASSWORD': 'unused', 'OPTIONS': {'sslmode': 'disable'}}}))
        bootstrap.chmod(0o600)
        os.environ['MOP_CONTROLLER_BOOTSTRAP'] = str(bootstrap)
        from mop.server.controller.runtime import configure
        configure()
        unittest.main()


class Boundary(unittest.TestCase):
    def test_health_is_a_protected_drf_query(self):
        from django.test import Client, override_settings
        with override_settings(ALLOWED_HOSTS=['testserver']):
            response = Client().get('/api/v1/health', secure=True)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['WWW-Authenticate'], 'Token')

    def test_database_failure_is_a_safe_503_not_an_auth_bypass(self):
        from unittest.mock import patch
        from django.db import DatabaseError
        from django.test import Client, override_settings
        with override_settings(ALLOWED_HOSTS=['testserver']), patch(
                'rest_framework.authentication.TokenAuthentication.authenticate',
                side_effect=DatabaseError('private credential must not escape')):
            response = Client().get('/api/v1/health', secure=True)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'database_unavailable')
        self.assertNotIn(b'private credential', response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_safe_management_refusal_keeps_its_reason(self):
        from unittest.mock import patch
        from django.core.management import CommandError
        from mop.server.controller.runtime import maintenance
        with patch('django.core.management.call_command', side_effect=CommandError('schema check needs a migration')):
            with self.assertRaisesRegex(RuntimeError, 'schema check needs a migration'):
                maintenance('migrations-check')

    def test_lazy_values_do_not_cross_await(self):
        from django.contrib.auth import get_user_model
        from mop.server.controller.runtime import database_call
        async def run():
            for value in (get_user_model().objects.all(), get_user_model()(username='unsaved'),
                          {'nested': [get_user_model().objects.all()]}, iter([1])):
                with self.subTest(type=type(value).__name__):
                    with self.assertRaisesRegex(TypeError, 'materialized'):
                        await database_call(lambda: value)
            self.assertEqual(await database_call(lambda: {'items': [1, 'two', None]}),
                             {'items': [1, 'two', None]})
        asyncio.run(run())

if __name__ == '__main__':
    main()
