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


def main():
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
        settings.DATABASES['default'].setdefault('TEST', {})['NAME'] = 'test_mop_' + uuid.uuid4().hex
        runner = DiscoverRunner(verbosity=2, interactive=False)
        return int(bool(runner.run_tests(['_ldap_cases', '_controller_cases', '_catalog_cases', '_access_cases'])))

if __name__ == '__main__':
    sys.exit(main())
