"""Материализованная sync/async-граница; без запросов к БД."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

import _lib  # noqa: F401 — lifetime временных файлов
import hermetic
from _controller_boundary_cases import Boundary, Materialization  # noqa: F401


def main():
    if importlib.util.find_spec('django') is None:
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


if __name__ == '__main__':
    main()
