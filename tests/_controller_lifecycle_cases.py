"""Поведенческая регрессия #668: только собственный DROP, настоящий PostgreSQL.

HYPOTHESIS: DiscoverRunner посылает административный DROP через runtime wait
5 s. Даже quiescent test DB не обязана удалить свои файлы/checkpoint за 5 s.
Задержка pg_sleep выполняется на том же собственном postgres management PID,
до обычного DROP: моделирует долгий серверный шаг, не утечку чужого владельца.
Предыдущая трасса #668 отдельно наблюдала реальные CheckpointDone/ProcSignalBarrier.
SOLUTION: только CREATE/DROP точной собственной БД — native postgres management
с общим бюджетом 30 s, OID/owner/nonce и отказом до DROP при живых соединениях.
RESULT: RED — после двух OK teardown fenced на 5.002 s; GREEN PG17 — DROP
после 5.880 s, runtime отказал на 5.000 s, cleanup refusal дал ненулевой exit.
STATUS: FIXED — see #668 (purpose harness; orphan-owner не установлен).
"""
import atexit
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import psycopg
from django.db import connection, OperationalError
from django.test import TransactionTestCase


class OwnedLifecycle(TransactionTestCase):
    serialized_rollback = True

    def test_actual_catalog_ownership_collision_and_quiescence_fail_closed(self):
        import copy
        import uuid
        from mop.server.controller.postgresql.base import DatabaseWrapper
        from _controller_fixture import OwnedTestDatabase, maintenance_cursor
        self.assertIsInstance(getattr(connection.creation._create_test_db, '__self__', None),
                              OwnedTestDatabase, 'the session must use the shared owned DB fixture')
        name = 'test_mop_' + uuid.uuid4().hex
        config = copy.deepcopy(connection.settings_dict)
        config['TEST']['NAME'] = name
        wrapper = DatabaseWrapper(config, alias='owned-lifecycle-probe')
        owner = OwnedTestDatabase(wrapper.creation, name)
        owner.create(0, True)
        try:
            collision = OwnedTestDatabase(wrapper.creation, name)
            with self.assertRaises(psycopg.errors.DuplicateDatabase):
                collision.create(0, True)
            with self.assertRaisesRegex(ValueError, 'created'):
                collision.destroy(name, 0)
            with maintenance_cursor(config) as cursor:
                cursor.execute(psycopg.sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                    psycopg.sql.Identifier(name), psycopg.sql.Literal('changed-owner')))
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                owner.destroy(name, 0)
            with maintenance_cursor(config) as cursor:
                cursor.execute(psycopg.sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                    psycopg.sql.Identifier(name), psycopg.sql.Literal(owner.marker)))
            with psycopg.connect(host=config['HOST'], port=config['PORT'],
                    user=config['USER'], password=config['PASSWORD'], dbname=name,
                    autocommit=True, connect_timeout=5, options='-c statement_timeout=1000') as held:
                with self.assertRaisesRegex(RuntimeError, 'quiescent'):
                    owner.destroy(name, 0)
                self.assertEqual(held.execute('SELECT 1').fetchone(), (1,),
                                 'cleanup must not terminate the remaining connection')
        finally:
            owner.destroy(name, 0)

    def test_cleanup_refusal_still_rejects_the_real_runner_gate(self):
        import hermetic
        env = hermetic.child_env({})
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('controller.py')),
            '_controller_lifecycle_cases.cleanup_failure_suite'], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        # #692: pytest сохраняет passing call и отдельно красит teardown.
        self.assertIn('::hold_own_connection PASSED', result.stdout)
        self.assertIn('1 passed', result.stdout)
        self.assertIn('1 error', result.stdout)
        self.assertIn('owned test database is not quiescent', result.stdout)
        self.assertIn('test database management failure:', result.stdout)
        self.assertNotIn('TransportDeadlineExceeded', result.stdout)
        print('owned lifecycle cleanup refusal rejected the gate after a passing test', flush=True)

    def test_runtime_query_still_fails_at_five_seconds(self):
        from django.conf import settings
        self.assertEqual(settings.CONTROLLER_DB_TIMEOUT, 5)
        self.assertEqual(settings.CONTROLLER_TRANSACTION_TIMEOUT, 10)
        with connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            self.assertEqual(cursor.fetchone()[0], '5s')
            started = time.monotonic()
            with self.assertRaises(OperationalError):
                cursor.execute('SELECT pg_sleep(6)')
            elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 4.5)
        self.assertLess(elapsed, 6)
        connection.close()
        print(f'owned lifecycle runtime query rejected in {elapsed:.3f}s', flush=True)

    def test_quiescent_owned_teardown_can_outlast_the_runtime_query_budget(self):
        name = connection.settings_dict['NAME']
        original = psycopg.Cursor.execute
        owner = self

        def delayed_owned_drop(cursor, query, *args, **kwargs):
            text = query if isinstance(query, str) else query.as_string(cursor.connection)
            if text != 'DROP DATABASE "' + name + '"':
                return original(cursor, query, *args, **kwargs)
            original(cursor, 'SELECT current_database()')
            owner.assertEqual(cursor.fetchone()[0], 'postgres')
            original(cursor, 'SELECT count(*) FROM pg_stat_activity WHERE datname = %s', (name,))
            owner.assertEqual(cursor.fetchone()[0], 0, 'target database is not quiescent')
            started = time.monotonic()
            # Это реальный server wait, не mock clock или ослабленное assertion.
            original(cursor, 'SELECT pg_sleep(5.25)')
            result = original(cursor, query, *args, **kwargs)
            elapsed = time.monotonic() - started
            owner.assertGreater(elapsed, 5)
            owner.assertLess(elapsed, 30)
            from mop.server.controller.postgresql.base import BoundedConnection
            owner.assertNotIsInstance(cursor.connection, BoundedConnection)
            print(f'owned lifecycle quiescent DROP succeeded after {elapsed:.3f}s management wait', flush=True)
            return result

        # Намеренно живёт до teardown_databases, после run_suite; процесс
        # fixture владеет patch. addCleanup остановил бы его ДО нужного DROP.
        patch.object(psycopg.Cursor, 'execute', delayed_owned_drop).start()


def cleanup_failure_suite():
    """Явный отрицательный subprocess probe, не часть зелёного module suite."""
    def hold_own_connection():
        import re
        config = connection.settings_dict
        assert re.fullmatch(r'test_mop_[0-9a-f]{32}', config['NAME']), 'probe requires its owned test database'
        held = psycopg.connect(host=config['HOST'], port=config['PORT'],
            user=config['USER'], password=config['PASSWORD'], dbname=config['NAME'],
            autocommit=True, connect_timeout=5, options='-c statement_timeout=1000')
        # Этот процесс владеет соединением до своего выхода. Runner обязан
        # отказать cleanup, не принудительно убить PID ради зелёного результата.
        atexit.register(held.close)
        held.execute('SELECT 1')
    probe = unittest.FunctionTestCase(hold_own_connection)
    probe.databases = {'default'}
    return unittest.TestSuite([probe])
