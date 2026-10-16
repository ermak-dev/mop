"""Django runner на настоящем PostgreSQL, без внешней test-транзакции."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _lib  # noqa: F401,E402 — lifetime всех временных файлов
import hermetic  # noqa: E402 — сначала отделить настройки старой машины


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
        import uuid
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
        runner = DiagnosticRunner(verbosity=2, interactive=False)
        return int(bool(runner.run_tests(labels or ['_controller_cases'])))
