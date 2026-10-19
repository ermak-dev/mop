"""Native pytest-django session для прежних точечных Django labels (#692).

unittest loader определяет только исходное множество ID: исполнение,
setup/teardown TestCase и session DB принадлежат pytest/pytest-django.
Hook-модуль без assert: PYTEST_DONT_REWRITE позволяет вложенный session.
"""
import importlib
from pathlib import Path
import unittest
from unittest.mock import patch

# Export pytest_* в plugin считается hook, а этот context — обычный helper.
from _pytest_runtime import load_pytest, pytest_environment as session_environment
pytest = load_pytest()

import hermetic
from _controller_fixture import OwnedTestDatabase, restore_snapshot


def cases(labels):
    """Точное исходное множество, включая inherited методы и suite factories."""
    def flatten(suite):
        if isinstance(suite, unittest.TestSuite):
            for child in suite:
                yield from flatten(child)
        else:
            error = getattr(suite, '_exception', None)
            if error is not None:
                raise ValueError(f'cannot collect Django label: {error}')
            yield suite

    result = {}
    for label in labels:
        for case in flatten(unittest.defaultTestLoader.loadTestsFromName(label)):
            result.setdefault(case.id(), case)
    if not result:
        raise ValueError('Django labels selected no tests')
    return result


def run_labels(labels):
    """Один pytest session и один session DB fixture на весь набор labels."""
    paths = list(dict.fromkeys(str(Path(importlib.import_module(label.split('.')[0]).__file__).resolve())
                               for label in labels))
    options = ['-c', str(Path(__file__).resolve().parents[1] / 'pyproject.toml'),
               '-p', 'pytest_django.plugin', '-p', '_pytest_django', '-v']
    for label in labels:
        options += ['--mop-django-label', label]
    if hermetic.STRICT:
        options.append('--mop-strict')
    with session_environment():
        return int(pytest.main(options + paths))


def pytest_addoption(parser):
    parser.addoption('--mop-django-label', action='append', default=[], dest='mop_django_labels')


def pytest_configure(config):
    try:
        config._mop_cases = cases(config.getoption('mop_django_labels'))
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None


def item_id(item):
    if item.cls is not None:
        return f'{item.cls.__module__}.{item.cls.__qualname__}.{item.name}'
    return None


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    expected = config._mop_cases
    selected, omitted = {}, []
    for item in items:
        identity = item_id(item)
        if identity in expected and identity not in selected:
            selected[identity] = item
        else:
            omitted.append(item)
    # Старый отрицательный lifecycle probe -- FunctionTestCase factory,
    # не метод класса. Он становится настоящим pytest function + real DB.
    for identity, case in expected.items():
        if isinstance(case, unittest.FunctionTestCase):
            item = pytest.Function.from_parent(session, name=identity, callobj=case._testFunc)
            item.add_marker(pytest.mark.django_db(transaction=True, serialized_rollback=True))
            selected[identity] = item
    missing = set(expected) - set(selected)
    if missing:
        raise pytest.UsageError('Django collection lost test IDs: ' + ', '.join(sorted(missing)))
    items[:] = [selected[identity] for identity in expected]
    config.hook.pytest_deselected(items=omitted)


@pytest.fixture(scope='session')
def _owned_django_management(django_db_blocker):
    """Ownership hooks вокруг штатного session setup/teardown pytest-django.

    Ни runtime backend, ни его 5/10 s budgets не меняются. При ошибке
    setup закрываем только собственный wrapper/pool и удаляем только БД,
    CREATE которой выполнен этим owner; teardown refusal не повторяем.
    """
    from django.db import connection
    creation = connection.creation
    owner = OwnedTestDatabase(creation, creation._get_test_db_name())
    original = creation.deserialize_db_from_string
    attempted_drop = False
    teardown_error = None

    def destroy(name, verbosity):
        nonlocal attempted_drop, teardown_error
        attempted_drop = True
        try:
            return owner.destroy(name, verbosity)
        except Exception as error:
            # Upstream pytest-django переводит teardown error в warning.
            # Запоминаем исходный отказ и возвращаем его session finalizer:
            # passing test не делает неубранную БД зелёным прогоном.
            teardown_error = error
            raise

    with patch.object(creation, '_create_test_db', new=owner.create), \
            patch.object(creation, '_destroy_test_db', new=destroy), \
            patch.object(creation, 'deserialize_db_from_string',
                         new=lambda data: restore_snapshot(creation, data, original)):
        try:
            yield owner
        finally:
            if teardown_error is not None:
                raise teardown_error
            if owner.identity is not None and not attempted_drop:
                with django_db_blocker.unblock():
                    connection.close()
                    owner.destroy(owner.name, 0)


@pytest.fixture(scope='session')
def django_db_setup(_owned_django_management, django_db_setup, django_db_blocker):
    """Одна общая DB fixture; сам migration/teardown -- из pytest-django.

    Порядок зависимостей важен: owned hooks уже стоят до upstream setup,
    upstream teardown заканчивается до выхода owned context.
    """
    from django.core.management import call_command
    # DiscoverRunner вызывал framework checks после setup_databases.
    # pytest-django сам их не запускает: ID/assertions не заменяют этот gate.
    with django_db_blocker.unblock():
        call_command('check', verbosity=0, databases=['default'])
    return django_db_setup
