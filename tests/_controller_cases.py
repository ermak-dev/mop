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
