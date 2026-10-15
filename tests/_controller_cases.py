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
from mop.server.controller.runtime import database_call

# HYPOTHESIS: штатный sync_to_async пропускает ленивый QuerySet и оставляет
# соединение живым; настройки по умолчанию не ограничивают runtime.
# PostgreSQL-проверки ниже используют отдельные соединения, не TestCase.
# SOLUTION: bounded runtime, lifecycle/pool cleanup, protected identity и
# matching-major backup/restore; отрицательные сценарии проверяются на БД.
# RESULT: constraints/CAS/locks и отказ записи не подменяются fake I/O.
# STATUS: FIXED — see #632
class Foundation(TransactionTestCase):
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
            with self.assertRaisesRegex(TypeError, 'materialized'):
                await database_call(lambda: get_user_model().objects.all())
            with self.assertRaisesRegex(TypeError, 'materialized'):
                await database_call(lambda: get_user_model().objects.first())
            value = await database_call(lambda: list(get_user_model().objects.values('username')))
            self.assertIsInstance(value, list)
            # Повторный вызов в том же потоке видит закрытое прошлое соединение.
            self.assertTrue(await database_call(lambda: connection.connection is None))
        get_user_model().objects.create_user('boundary-user')
        async_to_sync(run)()

    def test_orm_boundary_does_not_block_heartbeat(self):
        ticks = []
        async def run():
            async def beat():
                for _ in range(12):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(.02)
            def slow_service():
                with transaction.atomic():
                    return list(get_user_model().objects.values_list('username', flat=True))
            async def slow_effect():
                # Управляемый сетевой adapter: нет внешнего I/O в atomic.
                snapshot = await database_call(slow_service)
                await asyncio.sleep(.25)
                return snapshot
            await asyncio.gather(beat(), slow_effect())
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
                self.assertFalse(task.done())
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

    def test_short_transaction_rolls_back_before_over_budget_commit(self):
        from mop.server.controller.runtime import short_transaction
        with self.assertRaisesRegex(TimeoutError, 'transaction'):
            with short_transaction(budget=.01):
                get_user_model().objects.create(username='over-budget')
                time.sleep(.03)
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

    def _backup_restore_case(self, root_mismatch=False):
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
print(json.dumps({'pid': os.getpid(), 'db_pid': connection.connection.info.backend_pid}), flush=True)
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
        from mop.server.controller.runtime import Runtime, close_pools
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
                await database_call(close_pools)
                self.assertEqual(await count(), 0)
                self.assertGreater(max(samples), 0)
                self.assertLessEqual(max(samples), 8)
                self.assertLessEqual(len(threads), 4)
                self.assertEqual(runtime.pending, 0)
            finally:
                stop.set()
                await observer
                await runtime.aclose()
                await database_call(close_pools)
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
