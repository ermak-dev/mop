"""Общие проверки материализованной границы и владельца ресурсов."""
import asyncio
import unittest

import _lib  # noqa: F401 — lifetime временных файлов
import hermetic  # noqa: F401 — до framework и mop

# HYPOTHESIS: штатный sync_to_async переносит живые модели и QuerySet.
# Эти проверки не заменяют PostgreSQL lifecycle/concurrency gate.
# SOLUTION: проверять входы и выходы, дождаться quiescence при отмене,
# отдельно проверять настоящий ASGI listener и HTTP-resource cleanup.
# RESULT: модели/QuerySet не пересекают await, отказ сохраняет причину.
# STATUS: FIXED — see #632


# BUG: init=False обходил проверку, replace повторно запускал пользовательский код.
# HYPOTHESIS: копировать нужно все поля без __init__/__post_init__, с поиском циклов.
# SOLUTION: object.__new__ + снимок всех полей; caller cancellation не отменяет
# owned задачу, не отдаёт её слот и не задерживает самого caller до конца CPU.
# Точные типы не запускают lazy proxy; cleanup futures и реальные pool workers
# остаются owned до stop, а ошибка/timeout не превращаются в closed=True.
class Materialization(unittest.TestCase):
    def test_all_frozen_fields_are_checked_without_reinitialization(self):
        from dataclasses import dataclass, field
        from mop.server.controller.runtime import materialized
        calls = []
        @dataclass(frozen=True)
        class Snapshot:
            value: list
            derived: object = field(init=False, default_factory=lambda: calls.append('factory') or [])
            def __post_init__(self):
                calls.append('post_init')
        value = Snapshot([1])
        object.__setattr__(value, 'derived', [2])
        copy = materialized(value)
        self.assertEqual(calls, ['factory', 'post_init'])
        self.assertEqual(copy.derived, [2])
        self.assertIsNot(copy.derived, value.derived)
        self.assertIsNot(copy.value, value.value)
        object.__setattr__(value, 'derived', iter([1]))
        with self.assertRaisesRegex(TypeError, 'materialized'):
            materialized(value)
        object.__delattr__(value, 'derived')
        with self.assertRaisesRegex(TypeError, 'materialized'):
            materialized(value)

    def test_init_false_factory_cannot_smuggle_a_lazy_value(self):
        from dataclasses import dataclass, field
        from mop.server.controller.runtime import materialized
        @dataclass(frozen=True)
        class Snapshot:
            hidden: object = field(init=False, default_factory=lambda: iter([1]))
        with self.assertRaisesRegex(TypeError, 'materialized'):
            materialized(Snapshot())

    def test_dataclass_primitive_and_container_subclasses_cannot_bypass_field_validation(self):
        from dataclasses import dataclass, field
        from mop.server.controller.runtime import materialized
        @dataclass(frozen=True)
        class Text(str):
            hidden: object = field(init=False, default_factory=lambda: iter([1]))
        @dataclass(frozen=True)
        class Sequence(list):
            hidden: object = field(init=False, default_factory=lambda: iter([1]))
        for value in (Text(), Sequence()):
            with self.subTest(type=type(value).__name__):
                with self.assertRaisesRegex(TypeError, 'materialized'):
                    materialized(value)

    def test_django_lazy_values_are_rejected_without_resolving_them(self):
        from dataclasses import dataclass
        from django.utils.functional import SimpleLazyObject
        from mop.server.controller.runtime import materialized
        calls = []
        @dataclass(frozen=True)
        class Snapshot:
            value: object
        lazy = SimpleLazyObject(lambda: calls.append('resolved') or 'text')
        for value in (lazy, Snapshot(lazy)):
            with self.subTest(type=type(value).__name__):
                with self.assertRaisesRegex(TypeError, 'materialized'):
                    materialized(value)
        self.assertEqual(calls, [], 'materialization evaluated a lazy database value')

    def test_cycles_are_refused_but_shared_values_are_snapshotted(self):
        from dataclasses import dataclass
        from mop.server.controller.runtime import materialized
        @dataclass(frozen=True, slots=True)
        class Snapshot:
            value: object
        value = Snapshot(None)
        object.__setattr__(value, 'value', value)
        for cyclic in (value,):
            with self.assertRaisesRegex(TypeError, 'materialized'):
                materialized(cyclic)
        shared = [1]
        copy = materialized([shared, shared])
        self.assertEqual(copy, [[1], [1]])
        self.assertIsNot(copy[0], copy[1])

    def test_backend_retains_django_connection_settings_before_first_connection(self):
        # HYPOTHESIS: копия всего settings_dict отделяет wrapper от
        # ConnectionHandler и fresh-process probe попадает не в test database.
        # SOLUTION: сохранить ссылку на настройки Django, копировать только OPTIONS.
        # RESULT: unit red и реальный process-CAS red, затем проверена выбранная БД.
        import copy
        from django.conf import settings
        from mop.server.controller.postgresql.base import DatabaseWrapper
        config = copy.deepcopy(settings.DATABASES['default'])
        wrapper = DatabaseWrapper(config, 'settings_identity_probe')
        config['NAME'] = 'explicit-target-before-connect'
        self.assertEqual(wrapper.settings_dict['NAME'], 'explicit-target-before-connect')
        self.assertIsNone(wrapper.connection)

    def test_plain_enum_identity_values_can_cross_with_frozen_contexts(self):
        # HYPOTHESIS: ActorContext.kind — Enum; строгая scalar-проверка
        # ошибочно отвергала полностью материализованный frozen context.
        # SOLUTION: только обычные enum members со scalar value и без
        # дополнительного/lazy состояния; исходные ORM guards сохраняются.
        # RESULT: observed red, затем безопасный roundtrip и два отказа green.
        from dataclasses import dataclass
        from enum import Enum
        from mop.server.controller.runtime import materialized
        class Kind(str, Enum):
            HUMAN = 'human'
        @dataclass(frozen=True)
        class Context:
            kind: Kind
        copied = materialized(Context(Kind.HUMAN))
        self.assertIs(copied.kind, Kind.HUMAN)
        class Lazy(Enum):
            VALUE = iter([1])
        with self.assertRaisesRegex(TypeError, 'materialized'):
            materialized(Lazy.VALUE)
        class Extra(str, Enum):
            HUMAN = 'human'
        Extra.HUMAN.hidden = iter([1])
        with self.assertRaisesRegex(TypeError, 'materialized'):
            materialized(Extra.HUMAN)

    def test_only_owned_runtime_exposes_the_database_bridge(self):
        from mop.server.controller import runtime
        self.assertFalse(hasattr(runtime, 'database_call'), 'unowned database bridge remains public')

    def test_pool_closed_flag_does_not_hide_a_live_driver_thread(self):
        import copy
        import threading
        from django.conf import settings
        from django.db import connections
        from mop.server.controller.runtime import Runtime
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(10))
        worker.start()
        class Pool:
            # psycopg_pool.close очищает ссылки, даже когда join истёк.
            _workers = [worker]
            _sched_runner = None
            closed = False
            def close(self, timeout=5):
                self.closed = True
                self._workers = []
        alias = 'closing_probe'
        connections.databases[alias] = copy.deepcopy(settings.DATABASES['default'])
        db, pool = connections[alias], Pool()
        db._connection_pools[alias] = pool
        async def run():
            runtime = Runtime()
            try:
                with self.assertRaisesRegex(RuntimeError, 'shutdown incomplete'):
                    await runtime.aclose(timeout=.5)
                self.assertTrue(pool.closed)
                self.assertFalse(runtime._closed)
                self.assertIs(db._connection_pools[alias], pool)
                release.set()
                worker.join(1)
                await runtime.aclose(timeout=.5)
                self.assertTrue(runtime._closed)
                self.assertNotIn(alias, db._connection_pools)
            finally:
                release.set()
                worker.join(3)
                await runtime.aclose()
        try:
            asyncio.run(run())
        finally:
            release.set()
            worker.join(3)
            db._connection_pools.pop(alias, None)
            del connections[alias]
            del connections.databases[alias]

    def test_adapter_shutdown_budget_does_not_wait_for_cancellation_acknowledgement(self):
        from mop.server.controller.runtime import Runtime
        async def run():
            release = asyncio.Event()
            class Resource:
                calls = 0
                closed = False
                async def start(self):
                    pass
                async def aclose(self):
                    self.calls += 1
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    self.closed = True
            resource = Resource()
            runtime = Runtime(adapters=(resource,))
            await runtime.start()
            closing = asyncio.create_task(runtime.aclose(timeout=.02))
            try:
                done, _ = await asyncio.wait([closing], timeout=.15)
                self.assertIn(closing, done, 'shutdown waits forever for adapter cancellation acknowledgement')
                with self.assertRaisesRegex(RuntimeError, 'shutdown incomplete'):
                    await closing
                self.assertFalse(runtime._closed)
                self.assertFalse(resource.closed)
                release.set()
                await runtime.aclose(timeout=1)
                self.assertTrue(resource.closed)
                self.assertEqual(resource.calls, 1)
            finally:
                release.set()
                await asyncio.gather(closing, return_exceptions=True)
                await runtime.aclose()
        asyncio.run(run())

    def test_failed_adapter_cleanup_does_not_become_a_successful_second_shutdown(self):
        from mop.server.controller.runtime import Runtime
        async def run():
            class Resource:
                calls = 0
                closed = False
                async def start(self):
                    pass
                async def aclose(self):
                    self.calls += 1
                    if self.calls == 1:
                        raise ValueError('private adapter detail')
                    self.closed = True
            resource = Resource()
            runtime = Runtime(adapters=(resource,))
            await runtime.start()
            try:
                with self.assertRaisesRegex(RuntimeError, 'adapter shutdown failed') as error:
                    await runtime.aclose()
                self.assertNotIn('private', str(error.exception))
                import traceback
                self.assertNotIn('private adapter detail', ''.join(traceback.format_exception(error.exception)))
                self.assertFalse(runtime._closed)
                self.assertFalse(resource.closed)
                await runtime.aclose()
                self.assertTrue(resource.closed)
                self.assertEqual(resource.calls, 2)
            finally:
                await runtime.aclose()
        asyncio.run(run())

    def test_cancelled_cpu_callback_keeps_capacity_and_shutdown_is_honest(self):
        import threading
        from mop.server.controller.runtime import Runtime, Backpressure
        async def run():
            runtime = Runtime(blocking_limit=1, queue_limit=0)
            entered, release = threading.Event(), threading.Event()
            def callback():
                entered.set()
                release.wait(3)
            task = asyncio.create_task(runtime.blocking(callback))
            try:
                while not entered.is_set():
                    await asyncio.sleep(.005)
                task.cancel()
                done, _ = await asyncio.wait([task], timeout=.1)
                self.assertIn(task, done, 'cancellation waits forever for a trusted CPU callback')
                self.assertEqual(runtime.pending, 1)
                with self.assertRaises(Backpressure):
                    await runtime.blocking(lambda: None)
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    await runtime.aclose(timeout=.02)
                self.assertEqual(runtime.pending, 1)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await runtime.aclose()
        asyncio.run(run())


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

    def test_uncertain_commit_is_not_advertised_as_a_retryable_rollback(self):
        from django.db import DatabaseError
        from mop.server.controller.views import exception_handler
        driver = Exception('private driver context')
        driver.outcome_unknown = True
        error = DatabaseError('private database detail')
        error.__cause__ = driver
        response = exception_handler(error, {})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['error']['code'], 'database_outcome_unknown')
        self.assertFalse(response.data['error']['retryable'])
        self.assertNotIn('private', str(response.data))

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
        # HYPOTHESIS: custom bounded backend тоже PostgreSQL, но stock-only
        # ENGINE guard запрещал tools после объединения runtime/restore.
        # SOLUTION: явный набор поддерживаемых PostgreSQL engine paths.
        # RESULT: observed integration red, затем тот же matching-major gate green.
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
        from mop.server.controller.runtime import Runtime
        async def run():
            runtime = Runtime()
            try:
                for value in (get_user_model()(username='unsaved'),
                              get_user_model().objects.all(), connection):
                    for call in (runtime.database,):
                        with self.assertRaisesRegex(TypeError, 'materialized'):
                            await call(lambda item: True, value)
                        with self.assertRaisesRegex(TypeError, 'materialized'):
                            await call(lambda **kwargs: True, item=value)
            finally:
                await runtime.aclose()
        asyncio.run(run())

    def test_lazy_values_do_not_cross_await(self):
        from django.contrib.auth import get_user_model
        from mop.server.controller.runtime import Runtime
        async def run():
            runtime = Runtime()
            try:
                for value in (get_user_model().objects.all(), get_user_model()(username='unsaved'),
                              {'nested': [get_user_model().objects.all()]}, iter([1])):
                    with self.subTest(type=type(value).__name__):
                        with self.assertRaisesRegex(TypeError, 'materialized'):
                            await runtime.database(lambda: value)
                self.assertEqual(await runtime.database(lambda: {'items': [1, 'two', None]}),
                                 {'items': [1, 'two', None]})
            finally:
                await runtime.aclose()
        asyncio.run(run())
