"""Импорт зависимостей pytest вне плоского namespace старых checks."""
from contextlib import contextmanager
import os
from pathlib import Path
import sys


@contextmanager
def pytest_environment():
    """Внешние options/plugins не меняют точный plan; окружение возвращается."""
    keys = ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_pytest():
    # tests/packaging.py — check, не distribution packaging. Pytest импортирует
    # packaging.version лениво, уже после загрузки своего plugin/config.
    tests = Path(__file__).resolve().parent
    previous = list(sys.path)
    try:
        sys.path[:] = [entry for entry in sys.path if Path(entry or '.').resolve() != tests]
        import pytest
        import packaging.version  # noqa: F401 — до возврата namespace checks
        return pytest
    finally:
        sys.path[:] = previous
