"""Транзакционный контракт foundation; fake I/O не подменяет PostgreSQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase
from mop.server.controller.runtime import Runtime

# HYPOTHESIS: штатный sync_to_async пропускает ленивый QuerySet и оставляет
# соединение живым; настройки по умолчанию не ограничивают runtime.
# PostgreSQL-проверки ниже используют отдельные соединения, не TestCase.
# SOLUTION: bounded runtime, lifecycle/pool cleanup, protected identity и
# matching-major backup/restore; отрицательные сценарии проверяются на БД.
# RESULT: constraints/CAS/locks и отказ записи не подменяются fake I/O.
# STATUS: FIXED — see #632
# BUG: statement_timeout не ограничивает ожидание потерянного ответа PostgreSQL.
# HYPOTHESIS: driver wait должен иметь клиентский deadline и fence соединение.
# SOLUTION: bounded psycopg wait и абсолютный transaction deadline; COMMIT
# теряет только ответ, не превращается в подтверждённый rollback при cleanup.
# Обычный atomic тоже ограничен на driver boundary; native observer не наследует
# policy holder и измеряет время настоящего server-side lock.
class TransportDeadline(TransactionTestCase):
    # Как соседние классы: migration seeds восстанавливаются сериализацией,
    # иначе post_migrate после flush дублирует content types следующего класса.
    serialized_rollback = True

    def test_acknowledged_query_and_lost_commit_are_bounded_without_proxy_disconnect(self):
        from django.test import override_settings
        from _controller_transport import Blackhole
        for pooled in (False, True):
            for mode in ('orm', 'drf', 'commit', 'atomic_commit'):
                commit = mode in {'commit', 'atomic_commit'}
                with self.subTest(pool=pooled, mode=mode), override_settings(CONTROLLER_DB_TIMEOUT=.25):
                    proxy = Blackhole(connection.settings_dict)
                    alias = proxy.alias('blackhole', pool=pooled)
                    allowed = type(self).databases
                    type(self).databases = allowed | {alias}
                    finished = threading.Event()
                    errors, raw, responses = [], [], []
                    def service():
                        db = connections[alias]
                        try:
                            db.ensure_connection()
                            raw.append(db.connection)
                            if mode == 'atomic_commit':
                                from mop.server.controller.runtime import short_transaction
                                with short_transaction(using=alias, budget=1):
                                    get_user_model().objects.using(alias).create(username=f'lost-{mode}-{pooled}')
                                    proxy.drop.set()
                                return
                            if commit:
                                db.set_autocommit(False)
                                get_user_model().objects.using(alias).create(username=f'lost-{mode}-{pooled}')
                            proxy.drop.set()
                            if commit:
                                db.commit()
                            elif mode == 'drf':
                                from rest_framework.test import APIRequestFactory, force_authenticate
                                from rest_framework.views import APIView
                                from rest_framework.response import Response
                                from rest_framework.permissions import IsAuthenticated
                                class Probe(APIView):
                                    # Transport fixture не наследует domain admission.
                                    permission_classes = [IsAuthenticated]

                                    def get(self, request):
                                        return Response({'count': get_user_model().objects.using(alias).count()})
                                request = APIRequestFactory().get('/fixture')
                                force_authenticate(request, user=get_user_model()(username='authenticated-fixture'))
                                responses.append(Probe.as_view()(request))
                            else:
                                get_user_model().objects.using(alias).count()
                        except Exception as exc:
                            errors.append(exc)
                        finally:
                            db.close()
                            finished.set()
                    thread = threading.Thread(target=service)
                    thread.start()
                    try:
                        self.assertTrue(proxy.dropped.wait(3), 'proxy never dropped a live PostgreSQL reply')
                        self.assertTrue(proxy.forwarded.is_set())
                        self.assertTrue(finished.wait(1), 'client transport wait exceeded deadline with proxy still connected')
                        if mode == 'drf':
                            self.assertFalse(errors)
                            self.assertEqual(responses[0].status_code, 503)
                            self.assertEqual(responses[0].data['error']['code'], 'database_unavailable')
                        else:
                            self.assertTrue(errors, 'blackholed response reported success')
                        self.assertTrue(raw[0].closed, 'uncertain transport connection was reused')
                        if commit:
                            self.assertIn('outcome unknown', str(errors[0]))
                            self.assertTrue(getattr(errors[0].__cause__, 'outcome_unknown', False))
                            self.assertTrue(get_user_model().objects.filter(username=f'lost-{mode}-{pooled}').exists())
                    finally:
                        # Только ПОСЛЕ deadline assertion закрываем proxy: иначе тест скрывает дефект.
                        proxy.close()
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                        db = connections[alias]
                        if hasattr(db, 'close_pool'):
                            db.close_pool()
                        del connections[alias]
                        del connections.databases[alias]
                        type(self).databases = allowed

    def test_cancelled_async_database_call_drains_blackholed_transport_and_executor(self):
        for pooled in (False, True):
            with self.subTest(pool=pooled):
                self.cancelled_database_call(pooled)

    def cancelled_database_call(self, pooled):
        from django.test import override_settings
        from mop.server.controller.runtime import Runtime
        from _controller_transport import Blackhole
        with override_settings(CONTROLLER_DB_TIMEOUT=.25):
            proxy = Blackhole(connection.settings_dict)
            alias = proxy.alias('async_blackhole', pool=pooled)
            allowed = type(self).databases
            type(self).databases = allowed | {alias}
            entered = threading.Event()
            raw, driver_threads = [], []
            def service():
                db = connections[alias]
                db.ensure_connection()
                raw.append(db.connection)
                if pooled:
                    driver_threads.extend(db.pool._workers)
                    driver_threads.append(db.pool._sched_runner)
                proxy.drop.set()
                entered.set()
                return get_user_model().objects.using(alias).count()
            async def run():
                runtime = Runtime(blocking_limit=1, queue_limit=0)
                task = asyncio.create_task(runtime.database(service))
                try:
                    async with asyncio.timeout(3):
                        while not entered.is_set() or not proxy.dropped.is_set():
                            await asyncio.sleep(.005)
                    task.cancel()
                    done, _ = await asyncio.wait([task], timeout=1)
                    self.assertIn(task, done, 'cancelled database caller hangs on lost reply')
                    # Пул может уже заменять fenced соединение: connect_timeout
                    # тоже конечен (5 s). Proxy не закрывается до всех assertions.
                    await runtime.aclose(timeout=6 if pooled else 1)
                    self.assertEqual(runtime.pending, 0)
                    self.assertTrue(raw[0].closed)
                    self.assertFalse(any(t.is_alive() for t in runtime._executor._threads))
                    self.assertFalse(any(t.is_alive() for t in driver_threads))
                finally:
                    proxy.close()
                    await asyncio.gather(task, return_exceptions=True)
                    await runtime.aclose()
                    del connections.databases[alias]
                    type(self).databases = allowed
            async_to_sync(run)()

    def test_transaction_deadline_releases_observed_lock_during_successive_short_queries(self):
        from mop.server.controller.runtime import short_transaction
        self.observe_lock_deadline(lambda: short_transaction(budget=.2))

    def test_plain_atomic_cannot_bypass_the_transaction_deadline(self):
        from django.test import override_settings
        with override_settings(CONTROLLER_TRANSACTION_TIMEOUT=.2):
            self.observe_lock_deadline(transaction.atomic)

    def test_each_transaction_has_a_fresh_deadline(self):
        from django.test import override_settings
        with override_settings(CONTROLLER_TRANSACTION_TIMEOUT=.2):
            for _ in range(3):
                with transaction.atomic():
                    end = time.monotonic() + .08
                    while time.monotonic() < end:
                        get_user_model().objects.exists()

    def observe_lock_deadline(self, atomic):
        import psycopg
        from psycopg import sql
        config = connection.settings_dict
        observer = psycopg.connect(host=config['HOST'], port=config['PORT'], dbname=config['NAME'],
                                   user=config['USER'], password=config['PASSWORD'], connect_timeout=5,
                                   options='-c lock_timeout=2000 -c statement_timeout=5000',
                                   **{key: value for key, value in config['OPTIONS'].items()
                                      if key.startswith('ssl')})
        user = get_user_model().objects.create(username='deadline-lock')
        locked, finished = threading.Event(), threading.Event()
        errors = []
        def holder():
            try:
                with atomic():
                    get_user_model().objects.select_for_update().get(pk=user.pk)
                    locked.set()
                    end = time.monotonic() + 1
                    while time.monotonic() < end:
                        get_user_model().objects.filter(pk=user.pk).exists()
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
                finished.set()
        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(locked.wait(3))
            start = time.monotonic()
            # Независимый native observer не наследует deadline holder: иначе
            # истечение его собственного таймера маскировало бы время locks.
            with observer.cursor() as cursor:
                pk = sql.Identifier(get_user_model()._meta.pk.column)
                cursor.execute(sql.SQL('SELECT {pk} FROM {table} WHERE {pk} = %s FOR UPDATE').format(
                    pk=pk, table=sql.Identifier(get_user_model()._meta.db_table)), (user.pk,))
                self.assertEqual(cursor.fetchone(), (user.pk,))
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, .65, 'independent connection observed a lock past transaction deadline')
            self.assertTrue(finished.wait(1))
            self.assertTrue(errors)
        finally:
            observer.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())


class Foundation(TransactionTestCase):
    serialized_rollback = True

    def test_empty_schema_repeatable_and_no_missing_migrations(self):
        call_command('migrate', verbosity=0, interactive=False)
        call_command('migrate', verbosity=0, interactive=False)
        call_command('check', verbosity=0)
        call_command('makemigrations', check=True, dry_run=True, verbosity=0)
        user = get_user_model().objects.create_user('schema-user')
        self.assertEqual(get_user_model().objects.get(pk=user.pk).username, 'schema-user')

    def test_connection_policy_is_bounded_and_async_safe(self):
        self.assertEqual(connection.settings_dict['CONN_MAX_AGE'], 0)
        self.assertFalse(connection.settings_dict['ATOMIC_REQUESTS'])
        options = connection.settings_dict['OPTIONS']
        self.assertEqual(options['connect_timeout'], 5)
        self.assertLessEqual(options['pool']['max_size'], 8)
        self.assertIn('statement_timeout=5000', options['options'])
        self.assertIn('lock_timeout=2000', options['options'])
        self.assertIn('idle_in_transaction_session_timeout=10000', options['options'])

    def test_boundary_rejects_lazy_models_and_closes_connections(self):
        async def run():
            runtime = Runtime()
            try:
                with self.assertRaisesRegex(TypeError, 'materialized'):
                    await runtime.database(lambda: get_user_model().objects.all())
                with self.assertRaisesRegex(TypeError, 'materialized'):
                    await runtime.database(lambda: get_user_model().objects.first())
                value = await runtime.database(lambda: list(get_user_model().objects.values('username')))
                self.assertIsInstance(value, list)
                # Повторный вызов в том же потоке видит закрытое прошлое соединение.
                self.assertTrue(await runtime.database(lambda: connection.connection is None))
            finally:
                await runtime.aclose()
        get_user_model().objects.create_user('boundary-user')
        async_to_sync(run)()

    def test_orm_boundary_does_not_block_heartbeat(self):
        ticks = []
        async def run():
            runtime = Runtime()
            async def beat():
                for _ in range(12):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(.02)
            def slow_service():
                with transaction.atomic():
                    return list(get_user_model().objects.values_list('username', flat=True))
            async def slow_effect():
                # Управляемый сетевой adapter: нет внешнего I/O в atomic.
                snapshot = await runtime.database(slow_service)
                await asyncio.sleep(.25)
                return snapshot
            try:
                await asyncio.gather(beat(), slow_effect())
            finally:
                await runtime.aclose()
        async_to_sync(run)()
        self.assertEqual(len(ticks), 12)
        self.assertLess(max(b-a for a, b in zip(ticks, ticks[1:])), .1)

    def test_cancellation_does_not_leak_connection_or_free_a_running_slot(self):
        from mop.server.controller.runtime import Runtime
        async def run():
            entered = threading.Event()
            release = threading.Event()
            runtime = Runtime(blocking_limit=1, queue_limit=1)
            def service():
                get_user_model().objects.count()
                entered.set()
                release.wait(timeout=3)
                return {'finished': True}
            try:
                task = asyncio.create_task(runtime.database(service))
                while not entered.is_set():
                    await asyncio.sleep(.01)
                task.cancel()
                await asyncio.sleep(.02)
                self.assertTrue(task.cancelled())
                self.assertEqual(runtime.pending, 1)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await runtime.database(lambda: connection.connection is None))
            finally:
                release.set()
                await runtime.aclose()
            self.assertEqual(runtime.pending, 0)
            with self.assertRaisesRegex(RuntimeError, 'stopped'):
                await runtime.database(lambda: 1)
        async_to_sync(run)()

    def test_bounded_executor_and_async_adapter_ownership(self):
        from mop.server.controller.runtime import Runtime, Backpressure
        class FakeIO:
            def __init__(self):
                self.started = self.closed = False
            async def start(self):
                self.started = True
            async def aclose(self):
                self.closed = True
        async def run():
            io = FakeIO()
            runtime = Runtime(blocking_limit=2, effect_limit=2, queue_limit=2, adapters=(io,))
            await runtime.start()
            self.assertTrue(io.started)
            active, peak = 0, 0
            lock = threading.Lock()
            def blocking():
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(.08)
                with lock:
                    active -= 1
                return {'value': 1}
            tasks = [asyncio.create_task(runtime.blocking(blocking)) for _ in range(4)]
            await asyncio.sleep(.01)
            with self.assertRaises(Backpressure):
                await runtime.blocking(blocking)
            await asyncio.gather(*tasks)
            self.assertLessEqual(peak, 2)
            ticks = 0
            async def effect():
                await asyncio.sleep(.15)
                return 'receipt'
            async def heartbeat():
                nonlocal ticks
                for _ in range(10):
                    ticks += 1
                    await asyncio.sleep(.02)
            self.assertEqual((await asyncio.gather(runtime.effect(effect), heartbeat()))[0], 'receipt')
            self.assertEqual(ticks, 10)
            await runtime.aclose()
            self.assertTrue(io.closed)
            self.assertEqual(runtime.pending, 0)
        async_to_sync(run)()

    def test_write_refusal_and_broken_connection_surface_without_fallback(self):
        from django.db import DatabaseError
        connection.ensure_connection()
        raw = connection.connection
        raw.read_only = True
        try:
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    get_user_model().objects.create(username='write-refused')
        finally:
            raw.read_only = False
        self.assertFalse(get_user_model().objects.filter(username='write-refused').exists())
        # Настоящий разрыв driver connection, не mock БД.
        connection.connection.pgconn.finish()
        with self.assertRaises(DatabaseError):
            get_user_model().objects.count()
        connections.close_all()
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_system_checks_reject_privileged_runtime_and_missing_guarantees(self):
        from django.conf import settings
        from django.core.checks import run_checks
        from django.test import override_settings
        from unittest.mock import patch
        with override_settings(BOOTSTRAP=dict(settings.BOOTSTRAP, role='web')):
            errors = run_checks(databases=['default'])
            self.assertIn('controller.E004', {error.id for error in errors})
        with patch.object(connection.features, 'has_select_for_update', False):
            errors = run_checks(databases=['default'])
            self.assertIn('controller.E002', {error.id for error in errors})

    def test_runtime_db_principal_has_crud_but_no_schema_or_admin_rights(self):
        import copy
        import uuid
        import psycopg
        from psycopg import sql
        from django.conf import settings
        from django.core.checks import run_checks
        from django.db import ProgrammingError
        from django.test import override_settings
        role = 'runtime_' + uuid.uuid4().hex
        password = uuid.uuid4().hex
        config = copy.deepcopy(connection.settings_dict)
        params = {'host': config['HOST'], 'port': config['PORT'], 'user': config['USER'],
                  'password': config['PASSWORD'], 'dbname': config['NAME']}
        with psycopg.connect(**params, autocommit=True) as provisioner:
            provisioner.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}').format(
                sql.Identifier(role), sql.Literal(password)))
            allowed = type(self).databases
            type(self).databases = allowed | {'runtime'}
            config.update(USER=role, PASSWORD=password)
            connections.databases['runtime'] = config
            try:
                provisioner.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                    sql.Identifier(config['NAME']), sql.Identifier(role)))
                for privilege in ('GRANT USAGE ON SCHEMA public TO {}',
                                  'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}',
                                  'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}'):
                    provisioner.execute(sql.SQL(privilege).format(sql.Identifier(role)))
                with override_settings(BOOTSTRAP=dict(settings.BOOTSTRAP, role='web')):
                    self.assertEqual(run_checks(databases=['runtime']), [])
                get_user_model().objects.using('runtime').create(username='restricted-runtime-write')
                self.assertTrue(get_user_model().objects.filter(username='restricted-runtime-write').exists())
                with self.assertRaises(ProgrammingError) as error:
                    with connections['runtime'].cursor() as cursor:
                        cursor.execute('CREATE TABLE mop_permission_probe (id integer)')
                self.assertEqual(error.exception.__cause__.sqlstate, '42501')
                # NOINHERIT не запрещает SET ROLE в роль владельца таблицы.
                owner = role + '_owner'
                provisioner.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(owner)))
                try:
                    provisioner.execute('CREATE TABLE mop_permission_probe (id integer)')
                    provisioner.execute(sql.SQL('ALTER TABLE mop_permission_probe OWNER TO {}').format(sql.Identifier(owner)))
                    provisioner.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(owner), sql.Identifier(role)))
                    with override_settings(BOOTSTRAP=dict(settings.BOOTSTRAP, role='web')):
                        errors = run_checks(databases=['runtime'])
                        self.assertIn('controller.E004', {error.id for error in errors})
                finally:
                    provisioner.execute(sql.SQL('DROP OWNED BY {} CASCADE').format(sql.Identifier(owner)))
                    provisioner.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(owner)))
            finally:
                connections['runtime'].close()
                connections['runtime'].close_pool()
                del connections['runtime']
                del connections.databases['runtime']
                type(self).databases = allowed
                provisioner.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                provisioner.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))

    def test_precommit_guard_check_survives_a_healthy_database_step_longer_than_ten_milliseconds(self):
        # BUG: pipeline 5464/29462 падал до проверки Python pre-COMMIT guard:
        # здоровый INSERT занимал больше 10 ms, и driver правильно закрывал socket.
        # HYPOTHESIS: setup проверки не должен требовать sub-10ms PostgreSQL
        # под параллельным hermetic load; transport deadlines проверяются отдельно.
        # SOLUTION: независимые часы только для Python guard после настоящего
        # INSERT, при неизменных production driver deadlines и SQL rollback.
        # RESULT: deterministic slow-step red, затем все 43 PG cases green.
        # STATUS: FIXED — see #632 (изолированный acceptance harness).
        from unittest.mock import patch
        from mop.server.controller.postgresql.base import BoundedConnection
        wait = BoundedConnection.wait
        def healthy_slow_wait(driver, *args, **kwargs):
            time.sleep(.02)
            return wait(driver, *args, **kwargs)
        with patch.object(BoundedConnection, 'wait', healthy_slow_wait):
            self.test_short_transaction_rolls_back_before_over_budget_commit()

    def test_short_transaction_rolls_back_before_over_budget_commit(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from mop.server.controller import runtime
        clock = [0.]
        # Только часы Python pre-COMMIT guard: настоящий driver/SQL сохраняет
        # production timeout 10 s, реальную транзакцию и проверку rollback.
        with patch.object(runtime, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
            with self.assertRaisesRegex(TimeoutError, 'transaction'):
                with runtime.short_transaction():
                    get_user_model().objects.create(username='over-budget')
                    clock[0] = 11.
        self.assertFalse(get_user_model().objects.filter(username='over-budget').exists())

    def test_initialization_binds_identity_and_root_without_resetting_admin(self):
        from mop.server.controller.runtime import initialize, create_admin, verify_installation
        from django.test import override_settings
        from django.conf import settings
        from mop.server.controller.models import Installation
        first = initialize()
        self.assertEqual(initialize(), first)
        self.assertFalse(first['effects_enabled'])
        create_admin('first-admin', 'a-long-distinct-passphrase-632')
        user = get_user_model().objects.get(username='first-admin')
        self.assertTrue(user.check_password('a-long-distinct-passphrase-632'))
        self.assertTrue(user.is_staff and user.is_superuser)
        with self.assertRaisesRegex(RuntimeError, 'already'):
            create_admin('second-admin', 'another-distinct-passphrase-632')
        self.assertEqual(verify_installation()['epoch'], first['epoch'])
        with override_settings(BOOTSTRAP=dict(settings.BOOTSTRAP, installation_id='different')):
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                verify_installation()
        row = Installation.objects.get()
        row.root_digest = '0' * 64
        row.save(update_fields=['root_digest'])
        with self.assertRaisesRegex(RuntimeError, 'root'):
            verify_installation()

    def test_standard_auth_sessions_tokens_and_admin_token_redaction(self):
        from django.contrib import admin
        from django.contrib.sessions.models import Session
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import Client
        from rest_framework.authtoken.models import Token, TokenProxy
        from rest_framework.authentication import TokenAuthentication
        from rest_framework.test import APIRequestFactory
        user = get_user_model().objects.create_user('token-user', password='private-passphrase')
        token = Token.objects.create(user=user)
        request = APIRequestFactory().get('/', HTTP_AUTHORIZATION='Token ' + token.key)
        authenticated, _ = TokenAuthentication().authenticate(request)
        self.assertEqual(authenticated.pk, user.pk)
        store = SessionStore()
        store['value'] = 'retained'
        store.save()
        self.assertTrue(Session.objects.filter(session_key=store.session_key).exists())
        client = Client(enforce_csrf_checks=True)
        response = client.post('/admin/login/', {'username': 'token-user', 'password': 'private-passphrase'},
                               secure=True, HTTP_HOST='testserver')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(admin.site.is_registered(Token))
        self.assertFalse(admin.site.is_registered(TokenProxy))
        token.delete()
        from rest_framework.exceptions import AuthenticationFailed
        with self.assertRaises(AuthenticationFailed):
            TokenAuthentication().authenticate(request)

    def test_real_postgres_backup_restore_and_fresh_epoch(self):
        self._backup_restore_case()

    def test_failed_root_verification_cannot_leave_restored_effects_enabled(self):
        self._backup_restore_case(root_mismatch=True)

    def test_archive_commit_crash_cannot_reopen_old_timeline_after_restart(self):
        # BUG: pg_restore COMMIT предшествует fence; авария возвращает старую authority.
        # HYPOTHESIS: guard вне архива должен переживать COMMIT и новый startup.
        # SOLUTION: ORM guard коммитится до restore и не входит в archive.
        # RESULT: окно воспроизведено в PG16/17; PG16 job29332 уже отвергает startup.
        # STATUS: FIXED — see #632
        self._backup_restore_case(archive_commit_crash=True)

    def _backup_restore_case(self, root_mismatch=False, archive_commit_crash=False):
        import copy
        import tempfile
        from pathlib import Path
        import uuid
        import psycopg
        from psycopg import sql
        from mop.server.controller.runtime import initialize, verify_installation
        from mop.server.controller.backup import backup, restore
        from mop.server.controller.models import Installation
        original = initialize()
        Installation.objects.filter(pk=1).update(effects_enabled=True)
        get_user_model().objects.create_user('backed-up-user', password='never-in-tool-argv')
        name = 'restore_' + uuid.uuid4().hex
        config = copy.deepcopy(connection.settings_dict)
        config['NAME'] = name
        params = {'host': config['HOST'], 'port': config['PORT'], 'user': config['USER'],
                  'password': config['PASSWORD'], 'dbname': 'postgres'}
        with psycopg.connect(**params, autocommit=True) as provisioner:
            provisioner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            original_databases = type(self).databases
            type(self).databases = original_databases | {'restored'}
            try:
                connections.databases['restored'] = config
                with tempfile.TemporaryDirectory() as directory:
                    archive = Path(directory) / 'backup.dump'
                    backup(archive)
                    self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn(b'never-in-tool-argv', archive.read_bytes())
                    if archive_commit_crash:
                        from unittest.mock import patch
                        from mop.server.controller import backup as backup_module
                        from mop.server.controller.runtime import initialize as restart_initialize
                        tool = backup_module._tool
                        def crash_after_archive_commit(command, *args, **kwargs):
                            result = tool(command, *args, **kwargs)
                            if command == 'pg_restore' and '--single-transaction' in kwargs.get('extra', ()):
                                raise SystemExit('injected crash after archive commit before fence')
                            return result
                        with patch.object(backup_module, '_tool', crash_after_archive_commit):
                            with self.assertRaisesRegex(SystemExit, 'after archive commit'):
                                restore(archive, using='restored')
                        # Подтверждение окна: реально восстановленная БД, не fake restore.
                        row = Installation.objects.using('restored').get()
                        self.assertTrue(row.effects_enabled)
                        self.assertEqual(str(row.epoch), original['epoch'])
                        connections['restored'].close()
                        connections['restored'].close_pool()
                        with self.assertRaisesRegex(RuntimeError, 'restore|guard|maintenance'):
                            verify_installation(using='restored')
                        with self.assertRaisesRegex(RuntimeError, 'restore|guard|maintenance'):
                            restart_initialize(using='restored')
                        return
                    if root_mismatch:
                        from django.conf import settings
                        from django.test import override_settings
                        wrong_root = Path(directory) / 'wrong-root.key'
                        wrong_root.write_bytes(b'wrong-root-material-' * 4)
                        wrong_root.chmod(0o600)
                        with override_settings(BOOTSTRAP=dict(settings.BOOTSTRAP, root_key=str(wrong_root))):
                            with self.assertRaisesRegex(RuntimeError, 'root'):
                                restore(archive, using='restored')
                        row = Installation.objects.using('restored').get()
                        self.assertFalse(row.effects_enabled)
                        self.assertNotEqual(str(row.epoch), original['epoch'])
                        return
                    restore(archive, using='restored')
                    restored = verify_installation(using='restored')
                    self.assertNotEqual(restored['epoch'], original['epoch'])
                    self.assertFalse(restored['effects_enabled'])
                    self.assertTrue(get_user_model().objects.using('restored').get(username='backed-up-user').has_usable_password())
                    with self.assertRaisesRegex(RuntimeError, 'empty'):
                        restore(archive, using='restored')
            finally:
                connections['restored'].close()
                if hasattr(connections['restored'], 'close_pool'):
                    connections['restored'].close_pool()
                del connections['restored']
                del connections.databases['restored']
                type(self).databases = original_databases
                provisioner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

    def test_asgi_lifespan_owns_adapters_and_rejects_unbound_websockets(self):
        from asgiref.testing import ApplicationCommunicator
        from mop.server.controller.asgi import ControllerApplication
        from mop.server.controller.runtime import Runtime
        class FakeIO:
            started = closed = False
            async def start(self):
                self.started = True
            async def aclose(self):
                self.closed = True
        async def run():
            io = FakeIO()
            runtime = Runtime(adapters=(io,))
            app = ControllerApplication(runtime=runtime, startup=lambda: None)
            lifespan = ApplicationCommunicator(app, {'type': 'lifespan'})
            await lifespan.send_input({'type': 'lifespan.startup'})
            self.assertEqual((await lifespan.receive_output(timeout=3))['type'], 'lifespan.startup.complete')
            self.assertTrue(io.started)
            websocket = ApplicationCommunicator(app, {'type': 'websocket', 'path': '/ws/v1/events'})
            await websocket.send_input({'type': 'websocket.connect'})
            self.assertEqual((await websocket.receive_output())['type'], 'websocket.close')
            await websocket.wait()
            await lifespan.send_input({'type': 'lifespan.shutdown'})
            self.assertEqual((await lifespan.receive_output(timeout=3))['type'], 'lifespan.shutdown.complete')
            await lifespan.wait()
            self.assertTrue(io.closed)
            self.assertEqual(runtime.pending, 0)
        async_to_sync(run)()

    def test_cas_on_independent_os_processes_and_backend_connections(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        row = get_user_model().objects.create(username='process-cas', first_name='1')
        code = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from mop.server.controller.runtime import configure
configure()
from django.db import connection, connections
from django.contrib.auth import get_user_model
connections.databases['default']['NAME'] = sys.argv[2]
connection.ensure_connection()
print(json.dumps({'pid': os.getpid(), 'db_pid': connection.connection.info.backend_pid,
                  'database': connection.connection.info.dbname}), flush=True)
sys.stdin.readline()
try:
    count = get_user_model().objects.filter(pk=sys.argv[3], first_name='1').update(first_name='2')
    print(json.dumps({'count': count}), flush=True)
finally:
    connections.close_all()
'''
        root = str(Path(__file__).resolve().parents[1])
        children = [subprocess.Popen([sys.executable, '-I', '-c', code, root,
                                      connection.settings_dict['NAME'], str(row.pk)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            identities = [json.loads(child.stdout.readline()) for child in children]
            self.assertEqual(len({item['pid'] for item in identities}), 2)
            self.assertEqual(len({item['db_pid'] for item in identities}), 2)
            self.assertEqual({item['database'] for item in identities}, {connection.settings_dict['NAME']})
            for child in children:
                child.stdin.write('go\n')
                child.stdin.flush()
            results = []
            for child in children:
                stdout, stderr = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, stderr)
                results.append(json.loads(stdout)['count'])
            self.assertCountEqual(results, [0, 1])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_installation_singleton_constraint_is_enforced_by_database(self):
        from mop.server.controller.models import Installation
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Installation.objects.create(id=2, identity='invalid-second-installation', root_digest='0' * 64)
        self.assertFalse(Installation.objects.exists())

    def test_observed_threads_and_database_connections_are_bounded_and_drained(self):
        import psycopg
        from mop.server.controller.runtime import Runtime
        config = connection.settings_dict
        connections.close_all()
        connection.close_pool()
        params = {'host': config['HOST'], 'port': config['PORT'], 'user': config['USER'],
                  'password': config['PASSWORD'], 'dbname': 'postgres', 'autocommit': True}
        threads, lock = set(), threading.Lock()
        def service():
            with lock:
                threads.add(threading.get_ident())
            with transaction.atomic():
                return get_user_model().objects.count()
        async def run():
            admin = await psycopg.AsyncConnection.connect(**params)
            runtime = Runtime(blocking_limit=4, queue_limit=64)
            samples = []
            stop = asyncio.Event()
            async def count():
                cursor = await admin.execute('SELECT count(*) FROM pg_stat_activity WHERE datname = %s',
                                             (config['NAME'],))
                return (await cursor.fetchone())[0]
            async def observe():
                while not stop.is_set():
                    samples.append(await count())
                    await asyncio.sleep(.002)
            observer = asyncio.create_task(observe())
            try:
                await asyncio.gather(*(runtime.database(service) for _ in range(48)))
                stop.set()
                await observer
                await runtime.aclose()
                self.assertEqual(await count(), 0)
                self.assertGreater(max(samples), 0)
                self.assertLessEqual(max(samples), 8)
                self.assertLessEqual(len(threads), 4)
                self.assertEqual(runtime.pending, 0)
            finally:
                stop.set()
                await observer
                await runtime.aclose()
                await admin.close()
        async_to_sync(run)()

    def test_unique_constraint_on_independent_connections(self):
        barrier = threading.Barrier(2)
        def create():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                with transaction.atomic():
                    get_user_model().objects.create(username='same-identity')
                return 'created'
            except IntegrityError:
                return 'conflict'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: create(), range(2)))
        self.assertCountEqual(outcomes, ['created', 'conflict'])

    def test_cas_has_one_winner_and_rollback_is_real(self):
        row = get_user_model().objects.create(username='revision', first_name='1')
        barrier = threading.Barrier(2)
        def change():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return get_user_model().objects.filter(pk=row.pk, first_name='1').update(first_name='2')
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(list(executor.map(lambda _: change(), range(2))), [0, 1])
        with self.assertRaises(ValueError):
            with transaction.atomic():
                get_user_model().objects.filter(pk=row.pk).update(first_name='3')
                raise ValueError('rollback')
        row.refresh_from_db()
        self.assertEqual(row.first_name, '2')

    def test_row_lock_wait_is_bounded_and_recovers(self):
        row = get_user_model().objects.create(username='locked')
        entered = threading.Event()
        release = threading.Event()
        def holder():
            try:
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=row.pk)
                    entered.set()
                    release.wait(timeout=8)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(holder)
            self.assertTrue(entered.wait(timeout=5))
            start = time.monotonic()
            try:
                with self.assertRaises(OperationalError):
                    with transaction.atomic():
                        get_user_model().objects.select_for_update().get(pk=row.pk)
                self.assertLess(time.monotonic() - start, 4)
            finally:
                release.set()
                future.result(timeout=5)
        self.assertTrue(get_user_model().objects.filter(pk=row.pk).exists())
