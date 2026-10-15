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
# SOLUTION: проверять входы и выходы, дождаться quiescence при отмене,
# отдельно проверять настоящий ASGI listener и HTTP-resource cleanup.
# RESULT: модели/QuerySet не пересекают await, отказ сохраняет причину.
# STATUS: FIXED — see #632
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
                         'PASSWORD': 'unused-private-password', 'OPTIONS': {'sslmode': 'disable'}}}))
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

    def test_production_asgi_refuses_maintenance_credentials_before_database_access(self):
        from unittest.mock import patch
        from django.conf import settings
        from django.test import override_settings
        from mop.server.controller.asgi import ControllerApplication
        with override_settings(BOOTSTRAP=dict(settings.BOOTSTRAP, role='maintenance')):
            app = ControllerApplication()
            with patch('mop.server.controller.runtime._schema_ready',
                       side_effect=AssertionError('database accessed before process role check')):
                with self.assertRaisesRegex(RuntimeError, 'web/gateway'):
                    app.startup()

    def test_safe_management_refusal_keeps_its_reason(self):
        from unittest.mock import patch
        from django.core.management import CommandError
        from mop.server.controller.runtime import maintenance
        with patch('django.core.management.call_command', side_effect=CommandError('schema check needs a migration')):
            with self.assertRaisesRegex(RuntimeError, 'schema check needs a migration'):
                maintenance('migrations-check')

    def test_production_entrypoint_separates_bind_and_https_identity(self):
        from unittest.mock import patch
        from mop.cli.server.controller import main
        with patch('mop.server.controller.runtime.configure'), patch(
                'mop.server.controller.runtime.serve', return_value=0) as serve:
            main(['serve', '--host', '127.0.0.1', '--port', '8443',
                  '--hostname', 'controller.example.invalid',
                  '--tls-cert', '/private/cert.pem', '--tls-key', '/private/key.pem'])
        serve.assert_called_once_with('127.0.0.1', 8443,
                                      hostnames=['controller.example.invalid'],
                                      tls_cert='/private/cert.pem', tls_key='/private/key.pem')

    def test_public_http_without_tls_is_refused_before_listening(self):
        from unittest.mock import patch
        from mop.server.controller.runtime import serve
        with patch('mop.server.controller.runtime.validate_asgi_startup'), patch('uvicorn.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'TLS'):
                serve('0.0.0.0', 8443)
            run.assert_not_called()

    def test_real_asgi_server_and_worker_harness_own_lifecycle(self):
        import socket
        import aiohttp
        import uvicorn
        from mop.server.controller.asgi import ControllerApplication
        from mop.server.controller.runtime import Runtime, run_worker
        async def run():
            class HttpResource:
                session = None
                async def start(self):
                    self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))
                async def aclose(self):
                    await self.session.close()
            resource = HttpResource()
            runtime = Runtime(adapters=(resource,))
            async def http(scope, receive, send):
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'fixture'})
            # Только test harness, не анонимный production endpoint/workflow.
            app = ControllerApplication(runtime=runtime, startup=lambda: None, http=http)
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, lifespan='on', log_level='error'))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(3):
                    while not server.started:
                        await asyncio.sleep(.01)
                async def slow_io():
                    await asyncio.sleep(.2)
                    return 'receipt'
                effect = asyncio.create_task(runtime.effect(slow_io))
                async with resource.session.get(f'http://127.0.0.1:{port}/fixture') as response:
                    self.assertEqual(await response.text(), 'fixture')
                self.assertFalse(effect.done())
                self.assertEqual(await effect, 'receipt')
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 3)
                sock.close()
            self.assertTrue(resource.session.closed)
            self.assertEqual(runtime.pending, 0)
            worker_resource = HttpResource()
            stop = asyncio.Event()
            stop.set()
            await run_worker(stop, Runtime(adapters=(worker_resource,)))
            self.assertTrue(worker_resource.session.closed)
        asyncio.run(run())

    def test_backup_tools_require_matching_server_major_without_password_argv(self):
        from unittest.mock import Mock, patch
        from django.conf import settings
        from mop.server.controller.backup import _tool
        config = settings.DATABASES['default']
        for major in (16, 17):
            version = Mock(stdout=f'pg_dump (PostgreSQL) {major}.10'.encode())
            done = Mock(returncode=0, stderr=b'')
            with patch('subprocess.run', side_effect=[version, done]) as run:
                _tool('pg_dump', config, major=major)
                self.assertEqual(run.call_count, 2)
                self.assertNotIn(config['PASSWORD'], run.call_args.args[0])
        with patch('subprocess.run', return_value=Mock(stdout=b'pg_dump (PostgreSQL) 17.10')) as run:
            with self.assertRaisesRegex(RuntimeError, 'PostgreSQL 16'):
                _tool('pg_dump', config, major=16)
            self.assertEqual(run.call_count, 1)

    def test_blocking_external_io_does_not_starve_database_boundary(self):
        import threading
        from mop.server.controller.runtime import Runtime
        async def run():
            runtime = Runtime(blocking_limit=1)
            entered, release = threading.Event(), threading.Event()
            def external_io():
                entered.set()
                release.wait(timeout=3)
                return 'receipt'
            slow = asyncio.create_task(runtime.blocking(external_io))
            query = None
            try:
                while not entered.is_set():
                    await asyncio.sleep(.005)
                query = asyncio.create_task(runtime.database(lambda: {'snapshot': 'ready'}))
                await asyncio.sleep(.1)
                self.assertTrue(query.done(), 'slow external I/O starved the ORM boundary')
                self.assertEqual(await query, {'snapshot': 'ready'})
                self.assertFalse(slow.done())
            finally:
                release.set()
                await slow
                if query:
                    await query
                await runtime.aclose()
        asyncio.run(run())

    def test_shutdown_timeout_keeps_the_running_effect_accounted_for(self):
        from mop.server.controller.runtime import Runtime
        async def run():
            runtime = Runtime(effect_limit=1)
            entered, release = asyncio.Event(), asyncio.Event()
            async def effect():
                entered.set()
                await release.wait()
                return 'receipt'
            task = asyncio.create_task(runtime.effect(effect))
            try:
                await entered.wait()
                with self.assertRaisesRegex(RuntimeError, 'still running'):
                    await runtime.aclose(timeout=.01)
                self.assertEqual(runtime.pending, 1)
                self.assertFalse(task.done())
                release.set()
                self.assertEqual(await task, 'receipt')
            finally:
                release.set()
                await task
                await runtime.aclose()
            self.assertEqual(runtime.pending, 0)
        asyncio.run(run())

    def test_orm_inputs_do_not_cross_the_thread_boundary(self):
        from django.contrib.auth import get_user_model
        from django.db import connection
        from mop.server.controller.runtime import Runtime, database_call
        async def run():
            runtime = Runtime()
            try:
                for value in (get_user_model()(username='unsaved'),
                              get_user_model().objects.all(), connection):
                    for call in (database_call, runtime.database):
                        with self.assertRaisesRegex(TypeError, 'materialized'):
                            await call(lambda item: True, value)
                        with self.assertRaisesRegex(TypeError, 'materialized'):
                            await call(lambda **kwargs: True, item=value)
            finally:
                await runtime.aclose()
        asyncio.run(run())

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
