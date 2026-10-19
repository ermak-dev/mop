"""Pytest collection старых check-файлов без изменения их утверждений.

Изоляция subprocess нужна до миграции глобальных patches/import side effects
самих checks. Это переходный адаптер, не второй счётчик результатов.
Hook-модуль без assert: PYTEST_DONT_REWRITE позволяет вложенный session.
"""
import shutil
import tempfile

from _pytest_process import OwnedProcess

from _pytest_runtime import load_pytest
pytest = load_pytest()

import hermetic


def pytest_addoption(parser):
    parser.addoption('--mop-strict', action='store_true',
                     help='fail on every skipped check or test')


def pytest_configure(config):
    if config.getoption('mop_strict'):
        hermetic.STRICT = True
    config.pluginmanager.register(ImmediateFailures(config), 'mop-immediate-failures')


def native(config):
    """Django-label session собирает настоящие TestCase, не entrypoint-скрипт."""
    return bool(config.getoption('mop_django_labels', default=()))


def pytest_ignore_collect(collection_path, config):
    if collection_path.suffix != '.py' or native(config):
        return None
    if collection_path.name.startswith('_'):
        return True
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path, parent):
    if not native(parent.config):
        return LegacyFile.from_parent(parent, path=module_path)
    return None


class LegacyFile(pytest.File):
    def collect(self):
        yield pytest.Function.from_parent(self, name='check', callobj=legacy_check)


@pytest.fixture
def owned_legacy_process(request):
    # Каталог удаляется только после доказанного завершения owner. При отказе
    # он остаётся для диагностики; никакой atexit не сносит pending child HOME.
    directory = tempfile.mkdtemp(prefix='mp-')
    try:
        process = OwnedProcess(request.node.path, cwd=request.node.path.parent.parent,
                               env=hermetic.child_env({'TMPDIR': directory}))
    except BaseException:
        shutil.rmtree(directory)
        raise
    try:
        yield process
    finally:
        process.close()
        shutil.rmtree(directory)


def legacy_check(request, owned_legacy_process):
    process = owned_legacy_process
    output, _ = process.communicate()
    if process.returncode:
        reporter = request.config.pluginmanager.getplugin('terminalreporter')
        path = request.node.path
        reporter.write_sep('-', f'FAILED {path.name} (exit {process.returncode})')
        reporter.write_line(output.rstrip('\n'))
        reporter._tw.flush()
        pytest.fail(f'{path.name} exited {process.returncode}', pytrace=False)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    from _pytest.unittest import TestCaseFunction
    if hermetic.STRICT and isinstance(item, TestCaseFunction):
        # Pytest заменяет исходный exception на XFailed без причины.
        # В strict сохраняем исходные traceback/утверждение через addFailure.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(item, 'addExpectedFailure', item.addFailure)
            yield
    else:
        yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    expected = hasattr(report, 'wasxfail')
    if hermetic.STRICT and (report.skipped or expected):
        reason = report.longreprtext
        kind = 'skipped test'
        if expected:
            kind = 'expected outcome'
            reason = f'{report.wasxfail}\n{reason}'
            # Session считает failed только без wasxfail: None тоже не годится.
            del report.wasxfail
        report.outcome = 'failed'
        report.longrepr = f'strict mode rejects {kind} {item.nodeid}: {reason}'


@pytest.hookimpl(tryfirst=True)
def pytest_collectreport(report):
    if hermetic.STRICT and report.skipped:
        report.outcome = 'failed'
        report.longrepr = f'strict mode rejects skipped collection {report.nodeid}: {report.longreprtext}'


class ImmediateFailures:
    """Session-local reporter: вложенный pytest не меняет чужой session."""
    def __init__(self, config):
        self.config = config

    def pytest_runtest_logreport(self, report):
        # Native traceback и startup failure видны до следующего файла.
        # Legacy body уже напечатан; сам report содержит только exit verdict.
        # Полный report остаётся доступным стандартному pytest/junit.
        if report.failed:
            reporter = self.config.pluginmanager.getplugin('terminalreporter')
            reporter.write_line(report.longreprtext)
            for heading, content in report.sections:
                reporter.write_sep('-', heading)
                reporter.write_line(content)
            reporter._tw.flush()
