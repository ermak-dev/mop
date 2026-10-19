#!/usr/bin/env python3
"""Ограничение дочерних прогонов hermetic без сокращения покрытия (#673).

HYPOTHESIS: check_runs запускает все файлы обоих домов до первого communicate;
предел процессов отсутствует, а PIPE остальных детей никто не читает.
SOLUTION: четыре worker-а владеют Popen + communicate, отмена собирает только
свои session-группы; стартовая ошибка становится красным verdict с traceback.
RESULT: fake воспроизвёл peak=24/draining=1 до правки; после неё peak<=4,
параллельный drain, все 24 пары (дом, файл) сохранены. Это не замер ускорения.
STATUS: FIXED — see #673
"""
import os
import threading
import time
import signal
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import hermetic
from _lib import Checks


class Verdicts:
    def __init__(self):
        self.rows = []

    def check(self, what, ok):
        self.rows.append((what, ok))


def check_bounded_runs(c):
    lock = threading.Lock()
    active = peak = draining = peak_draining = 0
    launches = []
    names = [f'/source/tests/case_{n}.py' for n in range(12)]

    class Process:
        returncode = 0

        def __init__(self, argv, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                launches.append((argv, kwargs))

        def communicate(self):
            nonlocal active, draining, peak_draining
            with lock:
                draining += 1
                peak_draining = max(peak_draining, draining)
            time.sleep(0.02)
            with lock:
                draining -= 1
                active -= 1
            return ('case passed\n', None)

    verdicts = Verdicts()
    with patch.object(hermetic, 'files', return_value=names), \
            patch.object(hermetic, 'hostile_home', return_value=('/hostile', {'MOP_SSH_PORT': '2222'})), \
            patch.object(hermetic, 'hostile_repo', return_value='/copy'), \
            patch.object(hermetic, 'STRICT', True), \
            patch.object(hermetic.subprocess, 'Popen', Process):
        hermetic.check_runs(verdicts)
    c.check(f'bounded hermetic launches at most four children, observed {peak}', 1 < peak <= 4)
    c.check(f'communicate drains concurrent children, observed {peak_draining}', peak_draining > 1)
    expected = {(root, os.path.basename(name)) for root in (hermetic.ROOT, '/copy') for name in names}
    actual = [(kw['cwd'], os.path.basename(argv[-1])) for argv, kw in launches]
    c.check('every file runs exactly once in both homes', len(actual) == len(expected) == 24
            and set(actual) == expected)
    c.check('strict mode reaches every child', all(kw['env'].get('MOP_TESTS_STRICT') == '1'
            for _, kw in launches))
    c.check('hostile settings and HOME survive case planning', all(
        kw['env']['HOME'] == '/hostile' and kw['env']['MOP_SSH_PORT'] == '2222'
        for _, kw in launches if kw['cwd'] == '/copy'))
    c.check('all verdicts retained and no live child remains', len(verdicts.rows) == 24
            and all(ok for _, ok in verdicts.rows) and active == 0)


def check_failures(c):
    trace = 'Traceback: original failure\n' + '\n'.join(f'frame {n}' for n in range(20))

    class Process:
        returncode = 7

        def __init__(self, argv, **kwargs):
            if argv[-1].endswith('missing.py'):
                raise OSError('start refused: original reason')

        def communicate(self):
            return trace, None

    verdicts = Verdicts()
    with patch.object(hermetic, 'files', return_value=['/tests/missing.py', '/tests/bad.py']), \
            patch.object(hermetic, 'hostile_home', return_value=('/hostile', {})), \
            patch.object(hermetic, 'hostile_repo', return_value='/copy'), \
            patch.object(hermetic.subprocess, 'Popen', Process):
        hermetic.check_runs(verdicts)
    c.check('start failures and nonzero exits fail each case in both homes',
            len(verdicts.rows) == 4 and not any(ok for _, ok in verdicts.rows))
    c.check('nonzero exit preserves the complete original traceback', all(
        'exit 7' in what and all(line in what for line in trace.splitlines())
        for what, _ in verdicts.rows if 'bad.py' in what))
    c.check('start failure preserves its exception traceback and reason', all(
        'Traceback (most recent call last)' in what and 'start refused: original reason' in what
        for what, _ in verdicts.rows if 'missing.py' in what))


def check_cancellation(c):
    ready = threading.Event()
    released = threading.Event()
    lock = threading.Lock()
    started, drained, killed = [], [], []

    class Process:
        returncode = -signal.SIGKILL

        def __init__(self, argv, **kwargs):
            with lock:
                self.pid = 10000 + len(started)
                started.append(self.pid)
                if len(started) == 4:
                    ready.set()
            c.check('owned child starts in a new session', kwargs.get('start_new_session') is True)

        def communicate(self):
            if not released.wait(5):
                raise RuntimeError('cleanup did not release the owned child')
            with lock:
                drained.append(self.pid)
            return '', None

    class InterruptedPool(ThreadPoolExecutor):
        def map(self, *args, **kwargs):
            super().map(*args, **kwargs)
            if not ready.wait(5):
                raise RuntimeError('bounded workers did not start')
            raise KeyboardInterrupt('caller cancellation')

    def killpg(pid, sig):
        with lock:
            killed.append((pid, sig))
        released.set()

    cases = [('clean', f'/case_{n}.py', '/', {}) for n in range(24)]
    interrupted = False
    with patch.object(hermetic.subprocess, 'Popen', Process), \
            patch.object(hermetic, 'ThreadPoolExecutor', InterruptedPool), \
            patch.object(hermetic.os, 'killpg', killpg):
        try:
            hermetic.run_cases(cases)
        except KeyboardInterrupt as exc:
            interrupted = str(exc) == 'caller cancellation'
    c.check('caller cancellation remains visible', interrupted)
    c.check('cancellation prevents queued cases from launching', len(started) == 4)
    c.check('cancellation kills only owned groups and reaps every child',
            {pid for pid, sig in killed if sig == signal.SIGKILL} == set(started)
            and sorted(drained) == sorted(started))


def check_communication_failure(c):
    calls, killed = [], []

    class Process:
        pid = 12345

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self):
            calls.append('communicate')
            if len(calls) == 1:
                raise RuntimeError('reader failure')
            return '', None

    raised = False
    with patch.object(hermetic.subprocess, 'Popen', Process), \
            patch.object(hermetic.os, 'killpg', side_effect=lambda pid, sig: killed.append((pid, sig))):
        try:
            hermetic.run_cases([('clean', '/test.py', '/', {})])
        except RuntimeError as exc:
            raised = str(exc) == 'reader failure'
    c.check('reader failure propagates after owned kill and reap', raised and len(calls) == 2
            and killed == [(12345, signal.SIGKILL)])


def check_real_pipes(c):
    # Настоящие дети, больше ёмкости PIPE: проверяет communicate, не только fake.
    with tempfile.TemporaryDirectory() as root:
        script = Path(root) / 'output.py'
        script.write_text("import sys\nprint('x' * 262144)\nsys.exit(3)\n")
        results = hermetic.run_cases([('clean', str(script), root, hermetic.child_env({}))
                                     for _ in range(9)])
    c.check('real pipes drain fully and every nonzero child is reaped', len(results) == 9
            and all(code == 3 and text == 'x' * 262144 + '\n' for _, _, code, text in results))


def check_real_cancellation(c):
    # Только созданные здесь процессы; не ищем чужие PID и не трогаем пул.
    ready = threading.Event()
    lock = threading.Lock()
    owned = []
    popen = hermetic.subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        with lock:
            owned.append(process)
            if len(owned) == 4:
                ready.set()
        return process

    class InterruptedPool(ThreadPoolExecutor):
        def map(self, *args, **kwargs):
            super().map(*args, **kwargs)
            if not ready.wait(5):
                raise RuntimeError('real children did not start')
            raise KeyboardInterrupt('real cancellation')

    interrupted = False
    with tempfile.TemporaryDirectory() as root:
        script = Path(root) / 'wait.py'
        script.write_text('import time\ntime.sleep(30)\n')
        cases = [('clean', str(script), root, hermetic.child_env({})) for _ in range(9)]
        with patch.object(hermetic.subprocess, 'Popen', launch), \
                patch.object(hermetic, 'ThreadPoolExecutor', InterruptedPool):
            try:
                hermetic.run_cases(cases)
            except KeyboardInterrupt as exc:
                interrupted = str(exc) == 'real cancellation'
    c.check('real cancellation kills and reaps exactly its four owned children',
            interrupted and len(owned) == 4 and all(p.returncode == -signal.SIGKILL for p in owned))


# HYPOTHESIS (#692): прямой python check.py в HOME matrix обходит strict
# adapter, и unittest.skipTest остаётся зелёным только в этих подпрогонах.
# SOLUTION: тот же stdlib bridge, без изменения полного matrix или bound=4.
# RESULT: RED — exit 0 с одним skip; GREEN — exit 1 и исходный ID/reason,
# прежние real PIPE/cancellation и deep-TMPDIR probes также проходят.
# STATUS: FIXED — see #692

def check_strict_unittest_subruns_692(c):
    with tempfile.TemporaryDirectory() as root:
        script = Path(root) / 'skip_probe.py'
        script.write_text('import unittest\nclass Probe(unittest.TestCase):\n'
                          '    @unittest.skip("owned matrix skip reason")\n'
                          '    def test_boundary(self): pass\n'
                          'if __name__ == "__main__": unittest.main()\n')
        for strict in (False, True):
            rows = hermetic.run_cases([('clean', str(script), root,
                                        hermetic.child_env({}, strict=strict))])
            c.expect(f'#692 HOME matrix rejects unittest skips exactly in strict={strict}',
                     [code for _, _, code, _ in rows], [int(strict)])
            if strict:
                c.check('#692 HOME matrix retains the skipped test identity and reason',
                        'Probe.test_boundary' in rows[0][3] and
                        'owned matrix skip reason' in rows[0][3] and
                        'strict mode rejects skipped test' in rows[0][3], rows[0][3])


# HYPOTHESIS: вложенный _lib.RUN_ROOT удлиняет state/service.sock сверх
# production-предела; standalone installer-проверки скрывают ошибку (#677).
# SPEC: оба полных файла проходят с глубоким owned TMPDIR и настоящими
# clean/hostile HOME; bootstrap validation, daemon peers и ACL не подменяются.
# SOLUTION: короткий private runtime принадлежит общему FreshPlanFixture,
# а не HOME, всему tempfile-процесса или production socket validator.
# RESULT: RED -- оба файла отвергли endpoint в обоих домах; GREEN -- те же
# полные проверки и реальный bind/cleanup проходят без изменения production.
# STATUS: FIXED — see #677.
def check_installer_deep_tmpdir_677(c):
    child_env = hermetic.child_env
    names = [str(Path(hermetic.TESTS) / name)
             for name in ('fresh_install.py', 'install_provision.py')]
    with tempfile.TemporaryDirectory(prefix='mop-installer-parent-') as parent:
        deep = Path(parent) / ('nested-' + 'x' * 80)
        deep.mkdir(mode=0o700)
        c.check('#677 inherited TMPDIR alone exceeds the credential socket bound',
                len(os.fsencode(deep)) > 100)
        verdicts = Verdicts()
        # Только координата owned child; ambient TMPDIR/tempfile.tempdir не меняем.
        with patch.object(hermetic, 'files', return_value=names), \
                patch.object(hermetic, 'child_env', side_effect=lambda extra:
                    child_env({**extra, 'TMPDIR': str(deep)}, strict=True)):
            hermetic.check_runs(verdicts)
        c.expect('#677 the same two complete files run in both actual HOME contexts',
                 len(verdicts.rows), 4)
        for what, ok in verdicts.rows:
            c.check('#677 ' + what, ok)
        c.expect('#677 nested test processes clean their owned TMPDIR', list(deep.iterdir()), [])


def main():
    c = Checks()
    check_bounded_runs(c)
    check_failures(c)
    check_cancellation(c)
    check_communication_failure(c)
    check_real_pipes(c)
    check_real_cancellation(c)
    check_strict_unittest_subruns_692(c)
    check_installer_deep_tmpdir_677(c)
    return c.report('hermetic_runs')


if __name__ == '__main__':
    raise SystemExit(main())
