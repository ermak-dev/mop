"""Django runner на настоящем PostgreSQL, без внешней test-транзакции.

Контракт #674: под MOP_TESTS_STRICT=1 (как CI) skipped Django-результат --
провал прогона. Прежний DiscoverRunner молча считал его зелёным, и
непривилегированный CI никогда не замечал, что root-only UID граница не
проверена: 1 skipped читался зелёным итогом.
"""
from contextlib import contextmanager
import json
import time
import uuid
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _lib  # noqa: F401,E402 — lifetime всех временных файлов
import hermetic  # noqa: E402 — сначала отделить настройки старой машины


def run_exit_code(failures, *, strict, skipped):
    """Код выхода gate по числу провалов и (в strict) пропусков #697.

    DiscoverRunner.run_tests возвращает целое число провалов, а не объект
    результата: strict_failure_count над ним всегда видит ноль, и gate
    выходил 0 даже при FAILED. Пропуски считаются отдельно, только в strict.
    """
    if type(failures) is not int or type(skipped) is not int:
        raise ValueError('integer failure and skip counts required')
    return int(bool(failures + (skipped if strict else 0)))


def strict_failure_count(result, *, strict):
    """Число провалов прогона. Контракт #229 для Django runner (#674).

    Вне строгого режима -- как у Django: failures + errors. Под строгим --
    каждый skipped тоже отказ: пропуск проверяемой границы не зелёный.
    """
    failures = len(getattr(result, 'failures', ()) or ())
    errors = len(getattr(result, 'errors', ()) or ())
    skipped = len(getattr(result, 'skipped', ()) or ())
    return failures + errors + (skipped if strict else 0)


def _drop_backend_state(config, pid):
    """Только metadata собственной test DB и известного PID её DROP."""
    import re
    name = config.get('NAME')
    if (type(name) is not str or re.fullmatch(r'test_mop_[0-9a-f]{32}', name) is None
            or type(pid) is not int or pid <= 0):
        raise ValueError('diagnostic requires an owned test database and known backend PID')
    import psycopg
    with psycopg.connect(host=config['HOST'], port=config['PORT'], user=config['USER'],
                          password=config['PASSWORD'], dbname='postgres', autocommit=True,
                          connect_timeout=5, options='-c default_transaction_read_only=on -c statement_timeout=1000') as observer:
        return observer.execute(
            'SELECT pid, datname, state, wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s OR datname = %s',
            (pid, config['NAME'])).fetchall()


@contextmanager
def maintenance_cursor(config):
    """Native postgres-only соединение для lifecycle собственной test DB.

    30 s — конечный общий бюджет операции, не runtime setting/исключение для
    SQL приложения. Connect остаётся 5 s, lock 2 s; никаких retry/cancel waits.
    """
    import psycopg
    expires = time.monotonic() + 30

    class MaintenanceConnection(psycopg.Connection):
        def wait(self, gen, interval=.1, timeout=None):
            remaining = expires - time.monotonic()
            if timeout is not None:
                remaining = min(remaining, timeout)
            if remaining > 0:
                try:
                    return super().wait(gen, interval=interval, timeout=remaining)
                except psycopg.errors._WaitTimeout:
                    pass
            self.pgconn.finish()
            raise TimeoutError('owned test database maintenance deadline exceeded; connection fenced; outcome requires reconciliation') from None

    connection = MaintenanceConnection.connect(
        host=config['HOST'], port=config['PORT'], user=config['USER'],
        password=config['PASSWORD'], dbname='postgres', autocommit=True,
        connect_timeout=5, options='-c statement_timeout=30000 -c lock_timeout=2000 '
                                   '-c idle_in_transaction_session_timeout=10000',
        **{key: value for key, value in config.get('OPTIONS', {}).items()
           if key.startswith('ssl')})
    try:
        with connection.cursor() as cursor:
            yield cursor
    finally:
        connection.close()


class OwnedTestDatabase:
    """Единственные два management-действия fixture, не общий SQL adapter.

    Django autoclobber/retry не получает права удалить collision: владение
    появляется только после нашего CREATE и фиксируется OID/owner/nonce.
    """
    def __init__(self, creation, name):
        self.creation, self.name = creation, name
        self.identity = None
        self.marker = 'mop-test-owner-' + uuid.uuid4().hex
        self._validate(name)

    def _validate(self, name):
        import re
        config = self.creation.connection.settings_dict
        if (type(name) is not str or name != self.name
                or re.fullmatch(r'test_mop_[0-9a-f]{32}', name) is None
                or config.get('TEST', {}).get('NAME') != name):
            raise ValueError('management requires the exact owned test database name')
        return config

    @contextmanager
    def _cursor(self, action):
        config = self._validate(self.name)
        started, pid = time.monotonic(), None
        try:
            with maintenance_cursor(config) as cursor:
                observed_pid = cursor.connection.info.backend_pid
                pid = observed_pid if type(observed_pid) is int and observed_pid > 0 else None
                yield cursor
        except Exception as error:
            # Без connect inputs/пароля: исходное исключение не маскируется,
            # metadata только нашего имени и известного management PID.
            diagnostic = {'database': self.name, 'action': action, 'management_pid': pid,
                          'elapsed': round(time.monotonic() - started, 3),
                          'error_type': type(error).__name__, 'sqlstate': getattr(error, 'sqlstate', None)}
            if type(pid) is int and pid > 0:
                try:
                    diagnostic['activity'] = _drop_backend_state({**config, 'NAME': self.name}, pid)
                except Exception as observation:
                    diagnostic['diagnostic_error'] = type(observation).__name__
            print('test database management failure:', json.dumps(diagnostic), flush=True)
            raise

    def _metadata(self, cursor):
        cursor.execute('SELECT oid, datdba, shobj_description(oid, \'pg_database\') '
                       'FROM pg_database WHERE datname = %s', (self.name,))
        return cursor.fetchone()

    def create(self, verbosity, autoclobber, keepdb=False):
        self._validate(self.creation._get_test_db_name())
        if keepdb or self.identity is not None:
            raise ValueError('owned test database must be created once, without keepdb')
        if self.creation.sql_table_creation_suffix():
            raise ValueError('owned test database requires the default creation template')
        with self._cursor('create') as cursor:
            cursor.execute('CREATE DATABASE "' + self.name + '"')
            from psycopg import sql
            cursor.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(self.name), sql.Literal(self.marker)))
            self.identity = self._metadata(cursor)
            if self.identity is None or self.identity[2] != self.marker:
                raise RuntimeError('test database ownership metadata was not established')
        return self.name

    def destroy(self, test_database_name, verbosity):
        self._validate(test_database_name)
        if self.identity is None:
            raise ValueError('test database was not created by this owner')
        # Django destroy_test_db уже закрыл wrapper; его PostgreSQL hook до
        # DROP закрывает и joins pool. Не обходим этот порядок сменой purpose.
        self.creation.connection.close_pool()
        with self._cursor('drop') as cursor:
            if self._metadata(cursor) != self.identity:
                raise RuntimeError('test database ownership metadata changed; refusing DROP')
            cursor.execute('SELECT pid, state, wait_event_type, wait_event '
                           'FROM pg_stat_activity WHERE datname = %s', (self.name,))
            activity = cursor.fetchall()
            if activity:
                raise RuntimeError('owned test database is not quiescent: ' + json.dumps(activity))
            cursor.execute('DROP DATABASE "' + self.name + '"')
        self.identity = None


def restore_snapshot(creation, data, original):
    """Batch только migration seeds; прочие snapshots сохраняют путь Django."""
    from django.apps import apps
    from django.core import serializers
    from django.db import transaction
    from django.db.models.signals import pre_init, post_init, pre_save, post_save
    seeds = {'contenttypes.contenttype', 'auth.permission', 'controller.restoreguard'}
    labels = {row['model'] for row in json.loads(data)}
    if not labels <= seeds:
        return original(data)
    models = [apps.get_model(label) for label in labels]
    if any(model._meta.many_to_many or model._meta.parents
           or any(signal.has_listeners(model) for signal in (pre_init, post_init, pre_save, post_save))
           for model in models):
        return original(data)
    # Guard до deserialization: fallback не должен повторять init signals.
    objects = serializers.deserialize('json', data, using=creation.connection.alias)
    batches = {}
    for obj in objects:
        batches.setdefault(type(obj.object), []).append(obj.object)
    using = creation.connection.alias
    # Как у Django: одна настоящая транзакция, deferred FK и явная проверка
    # всех затронутых таблиц до COMMIT; ни constraints, ни seeds не убраны.
    with transaction.atomic(using=using):
        with creation.connection.constraint_checks_disabled():
            for model, rows in batches.items():
                model._base_manager.using(using).bulk_create(rows, update_conflicts=True,
                    unique_fields=[model._meta.pk.name],
                    update_fields=[field.name for field in model._meta.concrete_fields if not field.primary_key])
        creation.connection.check_constraints(table_names={model._meta.db_table for model in batches})


def main(labels=None):
    host = hermetic.FIXTURE_ENV.get('MOP_TEST_POSTGRES_HOST')
    if not host:
        hermetic.skip('controller PostgreSQL', 'isolated fixture requires MOP_TEST_POSTGRES_HOST')
        return 0
    with tempfile.TemporaryDirectory(prefix='mop-controller-test-') as directory:
        path = Path(directory)
        for name in ('root.key', 'django.key'):
            (path / name).write_text('test-only-key-' * 8)
            (path / name).chmod(0o600)
        bootstrap = {
            'installation_id': 'isolated-ci', 'role': 'maintenance',
            'root_key': str(path / 'root.key'), 'django_key': str(path / 'django.key'),
            'database': {'ENGINE': 'django.db.backends.postgresql',
                         'NAME': 'mop_ci', 'HOST': host, 'PORT': '5432',
                         'USER': 'mop_ci', 'PASSWORD': 'isolated-test-only',
                         'OPTIONS': {'sslmode': 'disable'}}}
        (path / 'bootstrap.json').write_text(json.dumps(bootstrap))
        (path / 'bootstrap.json').chmod(0o600)
        os.environ['MOP_CONTROLLER_BOOTSTRAP'] = str(path / 'bootstrap.json')
        from mop.server.controller.runtime import configure
        configure()
        from django.test.runner import DiscoverRunner
        from django.conf import settings
        # hermetic гоняет clean/hostile одновременно: у каждого своя БД.
        name = 'test_mop_' + uuid.uuid4().hex
        settings.DATABASES['default'].setdefault('TEST', {})['NAME'] = name
        class DiagnosticRunner(DiscoverRunner):
            def teardown_databases(self, old_config, **kwargs):
                import psycopg
                import time
                from unittest.mock import patch
                from django.db import connections
                config = dict(connections['default'].settings_dict)
                original_execute = psycopg.Cursor.execute
                def diagnostic_execute(cursor, query, *args, **kw):
                    if query != 'DROP DATABASE "' + name + '"':
                        return original_execute(cursor, query, *args, **kw)
                    # До execute: после fencing libpq уже не отдаёт backend_pid.
                    pid = cursor.connection.info.backend_pid
                    started = time.monotonic()
                    try:
                        return original_execute(cursor, query, *args, **kw)
                    except Exception:
                        elapsed = round(time.monotonic() - started, 3)
                        try:
                            rows = _drop_backend_state(config, pid)
                        except Exception as diagnostic:
                            # Не маскировать исходный DROP failure диагностикой
                            # и не печатать сырой driver error с connect inputs.
                            print('test database teardown diagnostic unavailable:', type(diagnostic).__name__, flush=True)
                        else:
                            print('test database teardown diagnostic:', json.dumps({
                                'database': name, 'drop_pid': pid, 'elapsed': elapsed,
                                'activity': rows}), flush=True)
                        raise
                # До отказа нет SQL/thread observer: не добавляем сетевых
                # ожиданий перед DROP и не заявляем server quiescence.
                with patch.object(psycopg.Cursor, 'execute', diagnostic_execute):
                    return super().teardown_databases(old_config, **kwargs)
        class ResultCapturingRunner(DiagnosticRunner):
            # run_tests возвращает ЦЕЛОЕ (failures+errors+unexpectedSuccesses),
            # объекта результата в нём нет. Пропуски видит только сам runner:
            # перехватываем run_suite, чтобы strict-режим (#674) снова считал их.
            def run_suite(self, suite, **kwargs):
                self.captured_result = super().run_suite(suite, **kwargs)
                return self.captured_result
        runner = ResultCapturingRunner(verbosity=2, interactive=False)
        from django.db import connection
        from unittest.mock import patch
        creation = connection.creation
        original = creation.deserialize_db_from_string
        owned = OwnedTestDatabase(creation, name)
        with patch.object(creation, '_create_test_db', new=owned.create), \
                patch.object(creation, '_destroy_test_db', new=owned.destroy), \
                patch.object(creation, 'deserialize_db_from_string',
                             new=lambda data: restore_snapshot(creation, data, original)):
            suite_result = runner.run_tests(labels or ['_controller_cases'])
            skipped = getattr(getattr(runner, 'captured_result', None), 'skipped', ()) or ()
            return run_exit_code(suite_result, strict=hermetic.STRICT, skipped=len(skipped))
