"""Pytest collection старых check-файлов без изменения их утверждений.

Изоляция subprocess нужна до миграции глобальных patches/import side effects
самих checks. Это переходный адаптер, не второй счётчик результатов.
Hook-модуль без assert: PYTEST_DONT_REWRITE позволяет вложенный session.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

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
        yield LegacyCheck.from_parent(self, name='check')


class LegacyCheck(pytest.Item):
    def runtest(self):
        helper = Path(__file__).with_name('_pytest_legacy.py')
        # Parent владеет TMPDIR даже при SIGKILL ребёнка: его atexit тогда
        # не исполняется. Чужие process groups/каталоги не рассматриваются.
        with tempfile.TemporaryDirectory(prefix='mp-') as directory:
            env = hermetic.child_env({'TMPDIR': directory})
            process = subprocess.Popen([sys.executable, str(helper), str(self.path)],
                cwd=self.path.parent.parent, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True)
            completed = False
            try:
                output, _ = process.communicate()
                completed = True
            finally:
                # И успешный entrypoint мог оставить потомка с закрытым PIPE.
                # Группа принадлежит только этому item; прямой ребёнок reaped.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if not completed:
                    process.communicate()
            if process.returncode:
                reporter = self.config.pluginmanager.getplugin('terminalreporter')
                reporter.write_sep('-', f'FAILED {self.path.name} (exit {process.returncode})')
                reporter.write_line(output.rstrip('\n'))
                reporter._tw.flush()
                pytest.fail(f'{self.path.name} exited {process.returncode}', pytrace=False)

    def reportinfo(self):
        return self.path, 0, self.path.name


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
