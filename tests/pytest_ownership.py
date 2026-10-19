"""Настоящий lifecycle legacy item и HOME workers (#769).

HYPOTHESIS: SIGKILL внешней группы убивает scheduler до его finally, а
start_new_session потомки остаются живыми даже после успешного parent exit.
SPEC: pytest finalizer дожидается владельца; только собственные PID/start/
group/namespace, четыре слота, bounded cleanup и nonzero при отказе.
SOLUTION: отдельный subreaper + pidfd, cooperative SIGINT, bounded TERM/KILL,
приватный startup/quiescence FD и стандартный pytest yield finalizer.
RESULT: RED — все четыре настоящих HOME worker и detached descendant живы;
GREEN Python 3.12/PG17 — ноль живых/зомби, scheduler finally/join выполнен,
чужая группа жива; refusal и exit 0 без receipt дают nonzero. Отдельный RED
показал потерянный cleanup error в joined future; теперь он достигает caller.
STATUS: FIXED — see #769
"""
import contextlib
import ctypes
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import hermetic
from _lib import run_command
from _controller_fixture import main
from _pytest_runtime import load_pytest
from mop.cli.dev import test as command

pytest = load_pytest()


@pytest.fixture(autouse=True)
def lifecycle_evidence(request, capsys):
    # Capture переключается именно в call phase, а не лишь в setup fixture.
    def evidence(message):
        with capsys.disabled():
            print(message, flush=True)
    request.instance.evidence = evidence


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start': fields[19], 'group': int(fields[2]),
                'session': int(fields[3]), 'namespace': os.readlink(f'/proc/{pid}/ns/pid')}
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def probe_children():
    # Только точные дети этой проверки. Subreaper позволяет собрать RED-сирот,
    # не отдавая их container PID 1 и не перечисляя чужие процессы машины.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'probe subreaper unavailable')
    owned = []

    def register(pid):
        before = identity(pid)
        fd = os.pidfd_open(pid)
        if before != identity(pid):
            os.close(fd)
            raise AssertionError('probe PID changed during registration')
        owned.append((before, fd))
        return before

    try:
        yield register
    finally:
        for row, fd in owned:
            try:
                if identity(row['pid']) == row:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                expires = time.monotonic() + 5
                while identity(row['pid']) == row and time.monotonic() < expires:
                    try:
                        os.waitpid(row['pid'], os.WNOHANG)
                    except ChildProcessError:
                        pass
                    time.sleep(.01)
                if identity(row['pid']) == row:
                    raise AssertionError(f'probe finalizer could not reap {row}')
            finally:
                os.close(fd)
        if libc.prctl(36, previous.value, 0, 0, 0):
            raise OSError(ctypes.get_errno(), 'probe subreaper restore failed')


class ItemProbe:
    def invoke(self, root):
        with patch.object(command, 'root', return_value=str(root)):
            return run_command(command.main, ['boundary', '--strict'])

    def source(self, root, text):
        (root / 'tests').mkdir()
        (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\npython_files = ["*.py"]\n')
        parent = root / 'tests/boundary.py'
        parent.write_text(text)
        return parent


class OwnedProcesses(ItemProbe, unittest.TestCase):
    def test_outer_reader_cancellation_reaps_four_actual_separate_home_workers(self):
        original = subprocess.Popen
        with tempfile.TemporaryDirectory(prefix='mop-769-cancel-') as directory, probe_children() as register:
            root = Path(directory)
            pids = root / 'pids'
            pids.mkdir()
            worker = root / 'worker.py'
            worker.write_text('import os, pathlib, time\n'
                'pathlib.Path(' + repr(str(pids)) + ', str(os.getpid())).write_text(str(os.getpid()))\n'
                'time.sleep(60)\n')
            joined = root / 'scheduler-joined'
            parent = self.source(root, 'import sys, pathlib\nsys.path.insert(0, ' + repr(hermetic.TESTS) + ')\n'
                'import hermetic\ntry:\n'
                '    hermetic.run_cases([("clean", ' + repr(str(worker)) + ', ' + repr(str(root)) +
                ', hermetic.child_env({})) for _ in range(12)])\n'
                'finally:\n    pathlib.Path(' + repr(str(joined)) + ').write_text("scheduler finally completed")\n')
            observed = []
            supervisors = []
            unrelated = original([sys.executable, '-c', 'import time; time.sleep(60)'],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            outsider = register(unrelated.pid)

            class Reader:
                def __init__(self, *args, **kwargs):
                    self.process = original(*args, **kwargs)
                    self.first = args[0][-1] == str(parent)

                def __getattr__(self, name):
                    return getattr(self.process, name)

                def communicate(self, *args, **kwargs):
                    if self.first:
                        self.first = False
                        expires = time.monotonic() + 15
                        while time.monotonic() < expires:
                            paths = list(pids.iterdir())
                            if len(paths) == 4:
                                observed.extend(register(int(path.read_text())) for path in paths)
                                supervisors.extend(register(int(Path(f"/proc/{row['pid']}/stat").read_text()
                                    .rsplit(')', 1)[1].split()[1])) for row in observed)
                                raise KeyboardInterrupt('owned outer reader cancellation')
                            if self.process.poll() is not None:
                                raise AssertionError('scheduler exited before readiness: ' + self.process.communicate()[0])
                            time.sleep(.02)
                        raise AssertionError('four actual HOME workers did not start')
                    return self.process.communicate(*args, **kwargs)

            started = time.monotonic()
            with patch.object(subprocess, 'Popen', Reader):
                out, err, code = self.invoke(root)
            elapsed = time.monotonic() - started
            self.assertEqual(code, 2, out + err)
            self.assertEqual(len(observed), 4)
            self.assertEqual(len(list(pids.iterdir())), 4, 'queued cases launched after cancellation')
            self.assertEqual(len({row['group'] for row in observed}), 4)
            self.assertTrue(all(row['session'] == row['group'] == row['pid'] for row in observed),
                            'the actual HOME workers must lead four distinct sessions')
            self.assertEqual(identity(unrelated.pid), outsider, 'an unrelated group was touched')
            self.assertTrue(joined.exists(), 'cancellation did not complete scheduler finally/join')
            self.assertEqual(len({row['pid'] for row in supervisors}), 4)
            self.assertTrue(all(row['session'] == row['group'] == row['pid'] for row in supervisors))
            self.assertEqual([row for row in supervisors if identity(row['pid']) == row], [],
                             'a separately owned worker supervisor was not reaped')
            survivors = [row for row in observed if identity(row['pid']) == row]
            self.evidence(f'#769 cancellation: workers={json.dumps(observed)}, surviving={json.dumps(survivors)}, '
                  f'supervisors={json.dumps(supervisors)}, supervisor_surviving=[], '
                  f'scheduler_finally_completed=True, unrelated_group_survived=True, elapsed={elapsed:.3f}s')
            self.assertLess(elapsed, 12, 'outer cancellation cleanup was not bounded')
            self.assertEqual(survivors, [], 'outer cleanup orphaned the four actual HOME workers')
            unrelated.wait(timeout=5) if unrelated.poll() is not None else None

    def test_successful_item_reaps_detached_descendant_before_reporting_success(self):
        with tempfile.TemporaryDirectory(prefix='mop-769-success-') as directory, probe_children() as register:
            root = Path(directory)
            pidfile = root / 'descendant.pid'
            self.source(root, 'import pathlib, subprocess, sys\n'
                'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], '
                'start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                'pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(child.pid))\n')
            # Регистрация до завершения item нужна только аварийному RED finalizer.
            original = subprocess.Popen
            rows = []

            class Reader:
                def __init__(self, *args, **kwargs):
                    self.process = original(*args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self.process, name)

                def communicate(self, *args, **kwargs):
                    output = self.process.communicate(*args, **kwargs)
                    if pidfile.exists() and not rows:
                        pid = int(pidfile.read_text())
                        if identity(pid) is not None:
                            rows.append(register(pid))
                    return output

            started = time.monotonic()
            with patch.object(subprocess, 'Popen', Reader):
                out, err, code = self.invoke(root)
            self.assertEqual(code, 0, out + err)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text())
            survivor = identity(pid)
            self.evidence(f'#769 successful item: descendant={pid}, surviving={survivor}, '
                  f'elapsed={time.monotonic() - started:.3f}s')
            self.assertIsNone(survivor, 'successful parent exit left an owned detached descendant')


class EscalationAndIdentity(ItemProbe, unittest.TestCase):

    def test_ignored_interrupt_and_term_escalate_and_reap_the_detached_tree(self):
        from _pytest_process import OwnedProcess
        with tempfile.TemporaryDirectory(prefix='mop-769-escalate-') as directory, probe_children() as register:
            root = Path(directory)
            pids = root / 'pids'
            pids.mkdir()
            body = root / 'body.py'
            child = root / 'child.py'
            waiting = ('import os, pathlib, signal, time\n'
                'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                'pathlib.Path(' + repr(str(pids)) + ', str(os.getpid())).write_text(str(os.getpid()))\n')
            child.write_text(waiting + 'time.sleep(60)\n')
            body.write_text(waiting + 'import subprocess, sys\n'
                'subprocess.Popen([sys.executable, ' + repr(str(child)) + '], start_new_session=True, '
                'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                'time.sleep(60)\n')
            process = OwnedProcess(body, cwd=root, env=hermetic.child_env({}))
            try:
                expires = time.monotonic() + 5
                while len(list(pids.iterdir())) != 2 and time.monotonic() < expires:
                    time.sleep(.02)
                self.assertEqual(len(list(pids.iterdir())), 2)
                rows = [register(int(path.read_text())) for path in pids.iterdir()]
                started = time.monotonic()
                process.close()
                elapsed = time.monotonic() - started
                self.assertNotEqual(process.returncode, 0)
                self.assertGreater(elapsed, 2, 'ignored SIGINT/SIGTERM did not exercise escalation')
                self.assertLess(elapsed, 6)
                self.assertEqual([row for row in rows if identity(row['pid']) == row], [])
                self.assertIsNotNone(process.process.poll(), 'supervisor was not joined')
                self.evidence(f'#769 bounded escalation: elapsed={elapsed:.3f}s, reaped={json.dumps(rows)}, '
                      'surviving=[], supervisor_joined=True')
            finally:
                process.close()

    def test_supervisor_exit_zero_without_quiescence_receipt_is_a_cleanup_failure(self):
        import _pytest_process as lifecycle
        original = subprocess.Popen
        launched, homes = [], []

        def no_receipt(argv, **kwargs):
            code = ('import socket, sys; channel = socket.socket(fileno=int(sys.argv[1])); '
                    'channel.send(b"ready"); channel.recv(16)')
            child = original([sys.executable, '-c', code, argv[3]], **kwargs)
            launched.append(child)
            homes.append(kwargs['env'].get('TMPDIR'))
            return child

        with tempfile.TemporaryDirectory(prefix='mop-769-no-receipt-') as directory:
            with patch.object(lifecycle.subprocess, 'Popen', side_effect=no_receipt):
                process = lifecycle.OwnedProcess('/not-executed.py', cwd=directory, env=hermetic.child_env({}))
            process.communicate()
            self.assertEqual(process.returncode, 0)
            with self.assertRaisesRegex(RuntimeError, 'quiescence receipt'):
                process.close()
            root = Path(directory)
            self.source(root, 'raise AssertionError("unacknowledged owner must not run a check")\n')
            with patch.object(lifecycle.subprocess, 'Popen', side_effect=no_receipt):
                out, err, code = self.invoke(root)
            home = Path(homes[-1])
            try:
                self.assertNotEqual(code, 0, out + err)
                self.assertIn('quiescence receipt', out + err)
                self.assertEqual(launched[-1].returncode, 0)
                self.assertTrue(home.exists(), 'fixture removed HOME without acknowledged quiescence')
                self.evidence(f'#769 missing receipt: owner_exit=0, gate_exit={code}, HOME_retained=True')
            finally:
                # Только synthetic no-child helper уже reaped нашим reader-ом.
                self.assertEqual(launched[-1].poll(), 0)
                shutil.rmtree(home)

    def test_registered_pid_start_group_and_namespace_mismatch_never_signal_an_unrelated_group(self):
        import _pytest_process as lifecycle
        with probe_children() as register:
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            row = register(unrelated.pid)
            handle = lifecycle.OwnedPid(unrelated.pid, os.getpid())
            try:
                for field, changed in (('start', 'reused'), ('group', os.getpgrp()),
                                       ('namespace', 'pid:[not-ours]')):
                    with self.subTest(field=field), \
                            patch.object(lifecycle, 'snapshot', return_value={**handle.row, field: changed}), \
                            patch.object(lifecycle.signal, 'pidfd_send_signal') as send:
                        with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                            handle.send(signal.SIGKILL)
                        send.assert_not_called()
                    self.assertEqual(identity(unrelated.pid), row)
                # После смерти исходного процесса pidfd readable. Даже если
                # /proc под тем же PID показывает нового владельца, не сигналим.
                exited = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.1)'],
                    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                dead = lifecycle.OwnedPid(exited.pid, os.getpid())
                try:
                    exited.wait(timeout=5)
                    with patch.object(lifecycle, 'snapshot', return_value={**dead.row, 'start': 'reused'}), \
                            patch.object(lifecycle.signal, 'pidfd_send_signal') as send:
                        dead.send(signal.SIGKILL)
                        send.assert_not_called()
                    self.assertEqual(identity(unrelated.pid), row)
                finally:
                    dead.close()
                self.evidence('#769 identity mismatch: start/group/namespace and PID reuse refused; unrelated group survived')
            finally:
                handle.close()

    def test_actual_cleanup_refusal_is_nonzero_even_after_escalation_reaps_every_child(self):
        with tempfile.TemporaryDirectory(prefix='mop-769-refusal-') as directory, probe_children() as register:
            root = Path(directory)
            pidfile = root / 'child.pid'
            waiting = root / 'waiting.py'
            waiting.write_text('import os, pathlib, signal, time\n'
                'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                'pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()))\n'
                'time.sleep(60)\n')
            spawning = root / 'spawning.py'
            spawning.write_text('import pathlib, subprocess, sys, time\n'
                'subprocess.Popen([sys.executable, ' + repr(str(waiting)) + '], start_new_session=True, '
                'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                'while not pathlib.Path(' + repr(str(pidfile)) + ').exists(): time.sleep(.01)\n')
            refusing = root / 'refusing.py'
            refusing.write_text('import sys, signal\nsys.path.insert(0, ' + repr(hermetic.TESTS) + ')\n'
                'import _pytest_process as lifecycle\noriginal = lifecycle.OwnedPid.send\n'
                'def refuse(handle, sig):\n'
                '    if sig == signal.SIGTERM: raise PermissionError("owned signal refusal probe")\n'
                '    return original(handle, sig)\n'
                'lifecycle.OwnedPid.send = refuse\n'
                'raise SystemExit(lifecycle.supervise([sys.executable, ' +
                repr(str(Path(hermetic.TESTS) / '_pytest_legacy.py')) + ', "--body", sys.argv[1]]))\n')
            self.source(root, 'import subprocess, sys\n'
                'raise SystemExit(subprocess.call([sys.executable, ' + repr(str(refusing)) + ', ' + repr(str(spawning)) + ']))\n')
            started = time.monotonic()
            out, err, code = self.invoke(root)
            self.assertNotEqual(code, 0, out + err)
            self.assertIn('owned process cleanup refused', out + err)
            self.assertIn('owned signal refusal probe', out + err)
            pid = int(pidfile.read_text())
            if identity(pid) is not None:
                register(pid)
            self.assertIsNone(identity(pid), 'refusal swallowed a pending child instead of reaping it')
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 6)
            self.evidence(f'#769 cleanup refusal: gate_exit={code}, elapsed={elapsed:.3f}s, surviving=[], '
                  'original_reason_retained=True')


class SchedulerFinalization(unittest.TestCase):
    # HYPOTHESIS: shutdown(wait=True) joins workers, но исключение close в
    # future теряется при KeyboardInterrupt читателя map. Это не quiescence.
    def test_cancellation_cannot_hide_a_joined_workers_cleanup_refusal(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        ready, released = threading.Event(), threading.Event()
        joined, directories = [], []

        class Owner:
            returncode = 0

            def __init__(self, *args, **kwargs):
                from types import SimpleNamespace
                self.handle = SimpleNamespace(row={'pid': 1, 'start': 'synthetic',
                    'group': 1, 'namespace': 'synthetic'})
                directories.append(kwargs['env']['TMPDIR'])
                ready.set()

            def communicate(self):
                if not released.wait(5):
                    raise RuntimeError('probe cancellation did not reach its owner')
                return '', None

            def cancel(self):
                released.set()

            def close(self):
                joined.append(True)
                raise RuntimeError('owned joined worker cleanup refused')

        class InterruptedPool(ThreadPoolExecutor):
            def map(self, *args, **kwargs):
                super().map(*args, **kwargs)
                if not ready.wait(5):
                    raise AssertionError('worker did not start')
                raise KeyboardInterrupt('outer scheduler reader cancellation')

        with patch.object(hermetic, 'OwnedProcess', Owner), \
                patch.object(hermetic, 'ThreadPoolExecutor', InterruptedPool):
            with self.assertRaisesRegex(ExceptionGroup, 'owned HOME worker cleanup refused') as caught:
                hermetic.run_cases([('clean', '/owned.py', '/', {})])
        self.assertEqual(joined, [True], 'the worker must actually finish/join')
        self.assertIn('owned joined worker cleanup refused', str(caught.exception.exceptions[0]))
        for directory in directories:
            self.assertTrue(Path(directory).exists(), 'scheduler finally removed a refused directory')
            # Только synthetic Owner этой проверки: настоящий body не стартовал.
            shutil.rmtree(directory)


class FreshOwnerDeath(unittest.TestCase):
    # BUG: смерть owner не ограничивает communicate, пока body держит PIPE.
    # HYPOTHESIS: deadline ставит только внешний cancel, а EOF не является
    # наблюдением смерти owner. Проверка убивает настоящий owner через pidfd.
    # SOLUTION: pidfd death запускает прежний bounded drain без внешнего
    # cancel. По deadline закрывается лишь reader, а не живые дети; exit owner
    # не заменяет receipt и не разрешает удаление принадлежащего body HOME.
    # RESULT: RED — pending после 8 s, только probe kill освобождает PIPE;
    # GREEN — CLI exit 1 через 6.319 s без probe kill, тот же body жив,
    # чужая группа не тронута; 39 focused + 5 UID на Python 3.12/PG17.
    # STATUS: FIXED — see #775
    def prepare(self, root, *, close_streams, kill_owner=True, home_slot=False):
        import textwrap
        (root / 'tests').mkdir()
        (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\npython_files = ["*.py"]\n')
        child_record, owner_record, result_record = [root / name for name in
            ('child.json', 'owner.json', 'result.json')]
        streams = ('fd = os.open(os.devnull, os.O_WRONLY)\n'
                   'os.dup2(fd, 1)\nos.dup2(fd, 2)\nos.close(fd)\n') if close_streams else ''
        (root / 'tests/boundary.py').write_text('import os, sys, json, pathlib, time\n'
            f'sys.path.insert(0, {hermetic.TESTS!r})\nimport hermetic\n' + streams +
            f'path = pathlib.Path({str(child_record)!r})\n'
            'path.with_suffix(".tmp").write_text(json.dumps({"pid": os.getpid(), "home": hermetic.HOME}))\n'
            f'path.with_suffix(".tmp").replace(path)\ntime.sleep({60 if kill_owner else .1})\n')
        invocation = "code = command.main(['boundary', '--strict'])"
        if home_slot:
            invocation = ("code = 0\ntry:\n"
                "    hermetic.run_cases([('clean', str(pathlib.Path(command.root()) / 'tests/boundary.py'), "
                "command.root(), hermetic.child_env({'TMPDIR': __import__('_lib').RUN_ROOT}))])\n"
                "except BaseException as error:\n"
                "    __import__('traceback').print_exception(error)\n    code = 1")
        driver = root / 'driver.py'
        driver.write_text(textwrap.dedent(f'''
            import json, os, pathlib, signal, subprocess, sys, time
            sys.path.insert(0, {hermetic.TESTS!r})
            import hermetic
            import _pytest_process as lifecycle
            from mop.cli.dev import test as command
            original = subprocess.Popen
            homes = []
            class Reader:
                def __init__(self, *args, **kwargs):
                    self.process = original(*args, **kwargs)
                    self.first = True
                    homes.append(kwargs['env']['TMPDIR'])
                def __getattr__(self, name):
                    return getattr(self.process, name)
                def communicate(self, *args, **kwargs):
                    if self.first and {kill_owner!r}:
                        self.first = False
                        expires = time.monotonic() + 10
                        while not pathlib.Path({str(child_record)!r}).exists():
                            if time.monotonic() >= expires:
                                raise AssertionError('real body did not start')
                            time.sleep(.01)
                        row = lifecycle.snapshot(self.process.pid)
                        fd = os.pidfd_open(self.process.pid)
                        try:
                            if lifecycle.snapshot(self.process.pid) != row:
                                raise AssertionError('probe owner identity changed')
                            signal.pidfd_send_signal(fd, signal.SIGKILL)
                        finally:
                            os.close(fd)
                        pathlib.Path({str(owner_record)!r}).write_text(json.dumps(row))
                    return self.process.communicate(*args, **kwargs)
            lifecycle.subprocess.Popen = Reader
            command.root = lambda: {str(root)!r}
            code = command.main(['boundary', '--strict'])
            child = json.loads(pathlib.Path({str(child_record)!r}).read_text())
            pathlib.Path({str(result_record)!r}).write_text(json.dumps({{
                'code': code, 'tmpdir': homes[0], 'home': child['home'],
                'run_root': __import__('_lib').RUN_ROOT,
                'home_before_exit': pathlib.Path(child['home']).exists(),
                'tmpdir_before_exit': pathlib.Path(homes[0]).exists(),
            }}))
            raise SystemExit(code)
        ''').replace("code = command.main(['boundary', '--strict'])", invocation))
        return driver, child_record, owner_record, result_record

    def wait_record(self, path, process):
        expires = time.monotonic() + 12
        while not path.exists():
            if process.poll() is not None:
                self.fail('probe exited before readiness: ' + process.communicate()[0])
            if time.monotonic() >= expires:
                self.fail('probe did not create readiness record')
            time.sleep(.01)
        return json.loads(path.read_text())

    def launch(self, driver, root):
        return subprocess.Popen([sys.executable, str(driver)], cwd=root,
            env=hermetic.child_env({'PYTHONPATH': os.pathsep.join((hermetic.ROOT, hermetic.TESTS))}),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def test_owner_death_is_bounded_while_live_body_holds_stdout_without_external_cancel(self):
        with tempfile.TemporaryDirectory(prefix='mop-775-pipe-') as directory, probe_children() as register:
            root = Path(directory)
            driver, child_record, owner_record, result_record = self.prepare(root, close_streams=False)
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            outsider = register(unrelated.pid)
            process = self.launch(driver, root)
            child = self.wait_record(child_record, process)
            body = register(child['pid'])
            owner = self.wait_record(owner_record, process)
            stuck = False
            started = time.monotonic()
            try:
                try:
                    output = process.communicate(timeout=8)[0]
                except subprocess.TimeoutExpired:
                    stuck = True
                    # RED finalizer освобождает только зарегистрированный body,
                    # не посылает cancel читателю и не сигналит чужую группу.
                    self.assertEqual(identity(child['pid']), body)
                    fd = os.pidfd_open(child['pid'])
                    try:
                        self.assertEqual(identity(child['pid']), body)
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally:
                        os.close(fd)
                    output = process.communicate(timeout=10)[0]
                result = json.loads(result_record.read_text())
                state = Path(f"/proc/{child['pid']}/stat").read_text().rsplit(')', 1)[1].split()[0]
                live = identity(child['pid']) == body and state not in ('Z', 'X')
                self.evidence('#775 actual owner death: ' + json.dumps({
                    'owner': owner, 'owner_gone': identity(owner['pid']) is None,
                    'body': body, 'body_alive_at_outer_exit': live,
                    'pending_after_8s': stuck, 'outer_exit': process.returncode,
                    'elapsed': round(time.monotonic() - started, 3),
                    'tmpdir_before_exit': result['tmpdir_before_exit'],
                    'missing_receipt_reported': 'quiescence receipt' in output,
                    'unrelated_group_survived': identity(unrelated.pid) == outsider}))
                self.assertEqual(process.returncode, 1, output)
                self.assertIn('quiescence receipt', output)
                self.assertIsNone(identity(owner['pid']))
                self.assertEqual(identity(unrelated.pid), outsider)
                self.assertFalse(stuck, 'dead owner was not observed independently of a live body output pipe')
                self.assertTrue(live, 'probe or runner killed the body instead of bounding the reader')
                self.assertTrue(result['tmpdir_before_exit'], result)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    process.communicate(timeout=15)
        self.remove_probe_root(result, child)

    # BUG: fixture сохраняет pending HOME, но atexit удаляет его RUN_ROOT.
    # HYPOTHESIS: quiescence должна владеть и lifetime предков каталога,
    # а не только локальным finally. До/после в одном процессе недостаточно.
    # SOLUTION: shared directory lifetime держит durable process lease до
    # успешного close/receipt. Atexit видит незакрытые leases и в дочерних
    # RUN_ROOT, сохраняя metadata и всё ещё используемые HOME/TMPDIR.
    # RESULT: RED — CLI 1, body S, HOME/TMPDIR/root исчезли после exit;
    # GREEN — CLI 1 и тот же живой body S, все три пути и owner evidence
    # сохранены. Safe CLI 0 удаляет свои пути, не чужой каталог. 72 focused
    # и 5 UID checks прошли на Python 3.12/PostgreSQL 17.
    # STATUS: FIXED — see #776
    # BUG: direct HOME slot не создаёт lease, и ancestor atexit удаляет HOME.
    # HYPOTHESIS: lifetime защищает лишь pytest fixture, не второго caller.
    # SOLUTION: оба caller запускают body через shared directory.start;
    # pending lease появляется до старта, а slot finally требует quiescence.
    # RESULT: RED — scheduler 1, body S, HOME/TMP/root исчезли после exit;
    # GREEN — те же PID/group живы, все три пути и lease сохранены. Safe slot
    # удаляет только свои quiet paths; 74 focused + 5 UID на Python 3.12/PG17.
    # STATUS: FIXED — see #779
    def test_direct_home_slot_retains_live_body_directories_after_scheduler_exit(self):
        self.test_missing_receipt_retains_live_body_home_and_tempdir_after_actual_cli_exit(home_slot=True)

    def test_missing_receipt_retains_live_body_home_and_tempdir_after_actual_cli_exit(self, *, home_slot=False):
        result = child = None
        try:
            with tempfile.TemporaryDirectory(prefix='mop-776-retained-') as directory, probe_children() as register:
                root = Path(directory)
                foreign = root / 'unrelated-directory'
                foreign.mkdir()
                (foreign / 'sentinel').write_text('not owned by the process directory')
                driver, child_record, owner_record, result_record = self.prepare(
                    root, close_streams=True, home_slot=home_slot)
                unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                outsider = register(unrelated.pid)
                process = self.launch(driver, root)
                child = self.wait_record(child_record, process)
                body = register(child['pid'])
                owner = self.wait_record(owner_record, process)
                try:
                    output = process.communicate(timeout=15)[0]
                    result = json.loads(result_record.read_text())
                    state = Path(f"/proc/{child['pid']}/stat").read_text().rsplit(')', 1)[1].split()[0]
                    self.evidence(('#779 direct HOME slot' if home_slot else '#776 fixture') +
                        ' post-interpreter-exit retention: ' + json.dumps({
                        **result, 'outer_exit': process.returncode, 'owner': owner,
                        'owner_gone': identity(owner['pid']) is None, 'body': body, 'body_state': state,
                        'same_body_after_exit': identity(child['pid']) == body,
                        'home_after_exit': Path(result['home']).exists(),
                        'tmpdir_after_exit': Path(result['tmpdir']).exists(),
                        'run_root_after_exit': Path(result['run_root']).exists(),
                        'unrelated_group_survived': identity(unrelated.pid) == outsider}))
                    self.assertEqual(process.returncode, 1, output)
                    self.assertIn('quiescence receipt', output)
                    self.assertIsNone(identity(owner['pid']))
                    self.assertEqual(identity(child['pid']), body)
                    self.assertNotIn(state, ('Z', 'X'), 'a zombie is not evidence of a live body')
                    self.assertEqual(identity(unrelated.pid), outsider)
                    self.assertEqual((foreign / 'sentinel').read_text(), 'not owned by the process directory')
                    self.assertTrue(result['home_before_exit'], result)
                    self.assertTrue(result['tmpdir_before_exit'], result)
                    self.assertTrue(Path(result['home']).exists(),
                        'actual interpreter atexit deleted the live body HOME after cleanup refusal')
                    self.assertTrue(Path(result['tmpdir']).exists(), result)
                    self.assertTrue(Path(result['run_root']).exists(), result)
                    metadata = json.loads((Path(result['tmpdir']) / '.mop-owned-process.json').read_text())
                    self.assertEqual(metadata['owner'], owner)
                    self.assertEqual(metadata['state'], 'retained')
                    self.assertIn('quiescence receipt', metadata['error'])
                    self.assertIn('retained owned process directories', output)
                    self.evidence('#776 retained process lease: ' + json.dumps(metadata))
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        process.communicate(timeout=15)
        finally:
            # Только после exact PID wait/reap probe_children. Это внешняя
            # аварийная уборка проверки, не признание quiescence CLI-ом.
            if result is not None:
                self.remove_probe_root(result, child)

    # BUG: один close теряет pidfd/control при bounded drain exception.
    # HYPOTHESIS: communicate стоит до finalization; closed-FD flag сам по
    # себе не доказывает quiescence и не должен освобождать retained lease.
    # SOLUTION: cancel/drain входят в общий finally всех локальных ресурсов;
    # исходный отказ сохранён отдельно от closed и повторно выбрасывается.
    # RESULT: до правки первый close оставлял pidfd/control открытыми; теперь
    # все три FD дают EBADF, repeat сохраняет исходную ошибку и lease, а HOME,
    # TMPDIR и RUN_ROOT существуют после выхода настоящего интерпретатора.
    # STATUS: FIXED — see #780
    def test_one_direct_close_finalizes_fds_but_repeat_preserves_refusal_after_outer_exit(self):
        import errno
        import textwrap
        result = child = None
        try:
            with tempfile.TemporaryDirectory(prefix='mop-780-close-') as directory, probe_children() as register:
                root = Path(directory)
                foreign = root / 'unrelated-directory'
                foreign.mkdir()
                (foreign / 'sentinel').write_text('not owned by close')
                driver, child_record, owner_record, result_record = self.prepare(root, close_streams=False)
                driver.write_text(textwrap.dedent(f'''
                    import errno, json, os, pathlib, signal, sys, time
                    sys.path.insert(0, {hermetic.TESTS!r})
                    import hermetic, _lib, _pytest_process as lifecycle
                    directory = _lib.OwnedProcessDirectory({str(root / 'tests/boundary.py')!r})
                    process = directory.start(lifecycle.OwnedProcess, cwd={str(root)!r}, env=hermetic.child_env({{}}))
                    expires = time.monotonic() + 10
                    while not pathlib.Path({str(child_record)!r}).exists():
                        if time.monotonic() >= expires: raise AssertionError('direct close body did not start')
                        time.sleep(.01)
                    child = json.loads(pathlib.Path({str(child_record)!r}).read_text())
                    owner = dict(process.handle.row)
                    pathlib.Path({str(owner_record)!r}).write_text(json.dumps(owner))
                    if lifecycle.snapshot(owner['pid']) != owner: raise AssertionError('probe owner identity changed')
                    fds = {{'pidfd': process.handle.fd, 'control': process.channel.fileno(),
                            'stdout': process.process.stdout.fileno()}}
                    signal.pidfd_send_signal(process.handle.fd, signal.SIGKILL)
                    started = time.monotonic()
                    first_error = None
                    try:
                        directory.close()
                    except BaseException as error:
                        first_error = error
                    # Первая запись сделана до любого repeat close: это именно
                    # прямой close, а не communicate + fixture-finalizer.
                    opened, closed_errno = [], {{}}
                    for name, fd in fds.items():
                        try: os.fstat(fd)
                        except OSError as error: closed_errno[name] = error.errno
                        else: opened.append(name)
                    lease = pathlib.Path(directory.path, _lib.PROCESS_LEASE_NAME)
                    first = {{'open_fds': opened, 'closed_errno': closed_errno,
                        'stdout_closed': process.process.stdout.closed, 'process_closed': process.closed,
                        'owner_exit': process.returncode, 'owner_reaped': lifecycle.snapshot(owner['pid']) is None,
                        'error': str(first_error), 'error_type': type(first_error).__name__,
                        'lease': json.loads(lease.read_text()) if lease.exists() else None}}
                    second_error = None
                    try:
                        directory.close()
                    except BaseException as error:
                        second_error = error
                    pathlib.Path({str(result_record)!r}).write_text(json.dumps({{
                        'first': first, 'second_error': str(second_error),
                        'same_error': second_error is first_error,
                        'lease_after_repeat': json.loads(lease.read_text()) if lease.exists() else None,
                        'elapsed': time.monotonic() - started, 'home': child['home'],
                        'tmpdir': directory.path, 'run_root': _lib.RUN_ROOT,
                        'home_before_exit': pathlib.Path(child['home']).exists(),
                        'tmpdir_before_exit': pathlib.Path(directory.path).exists(),
                    }}))
                    raise SystemExit(1 if first_error is not None else 0)
                '''))
                unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                outsider = register(unrelated.pid)
                process = self.launch(driver, root)
                child = self.wait_record(child_record, process)
                body = register(child['pid'])
                owner = self.wait_record(owner_record, process)
                try:
                    output = process.communicate(timeout=15)[0]
                    result = json.loads(result_record.read_text())
                    first = result['first']
                    state = Path(f"/proc/{child['pid']}/stat").read_text().rsplit(')', 1)[1].split()[0]
                    self.evidence('#780 direct close and actual outer exit: ' + json.dumps({
                        **result, 'outer_exit': process.returncode, 'owner': owner, 'body': body,
                        'body_state': state, 'same_body_alive': identity(child['pid']) == body,
                        'home_after_exit': Path(result['home']).exists(),
                        'tmpdir_after_exit': Path(result['tmpdir']).exists(),
                        'run_root_after_exit': Path(result['run_root']).exists(),
                        'unrelated_group_survived': identity(unrelated.pid) == outsider}))
                    self.assertEqual(process.returncode, 1, output)
                    self.assertEqual(first['error_type'], 'RuntimeError')
                    self.assertIn('cleanup deadline exceeded', first['error'])
                    self.assertIn('quiescence receipt', first['error'])
                    self.assertEqual(first['owner_exit'], -signal.SIGKILL)
                    self.assertTrue(first['owner_reaped'])
                    self.assertIsNone(identity(owner['pid']))
                    self.assertEqual(identity(child['pid']), body)
                    self.assertNotIn(state, ('Z', 'X'))
                    self.assertEqual(first['open_fds'], [], 'one direct close must finalize every local FD before repeat')
                    self.assertEqual(first['closed_errno'], dict.fromkeys(('pidfd', 'control', 'stdout'), errno.EBADF))
                    self.assertTrue(first['stdout_closed'])
                    self.assertTrue(first['process_closed'])
                    self.assertTrue(result['same_error'], 'repeat close must raise the original refusal, not release its lease')
                    self.assertEqual(result['second_error'], first['error'])
                    self.assertEqual(result['lease_after_repeat'], first['lease'])
                    self.assertEqual(first['lease']['state'], 'retained')
                    self.assertEqual(first['lease']['owner'], owner)
                    self.assertEqual(first['lease']['error'], first['error'])
                    self.assertTrue(result['home_before_exit'])
                    self.assertTrue(result['tmpdir_before_exit'])
                    for name in ('home', 'tmpdir', 'run_root'):
                        self.assertTrue(Path(result[name]).exists(), (name, result))
                    self.assertIn('retained owned process directories', output)
                    self.assertEqual(identity(unrelated.pid), outsider)
                    self.assertEqual((foreign / 'sentinel').read_text(), 'not owned by close')
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        process.communicate(timeout=15)
        finally:
            if result is not None:
                self.remove_probe_root(result, child)

    # BUG: поздний нормальный exit body удаляет HOME живого потомка, хотя
    # enclosing lease его slot остаётся retained после exit scheduler-а.
    # HYPOTHESIS: _cleanup_run_root ищет leases только вниз от своего RUN_ROOT,
    # а lease владельца лежит в TMPDIR снаружи вложенного корня body.
    # SOLUTION: корень, созданный прямо в каталоге lease, принадлежит lifetime
    # владельца: body atexit его не удаляет, каталог целиком удаляет owner
    # после receipt; без enclosing lease cleanup прежний.
    # RESULT: RED — scheduler/CLI 1, после нормального exit 0 body HOME исчез,
    # живой потомок получил FileNotFoundError; GREEN — тот же потомок S читает
    # свои данные, HOME/TMPDIR/RUN_ROOT и retained lease на месте. Safe slot и
    # fixture по-прежнему удаляют свои пути; 80 focused + 5 UID, Python 3.12/PG17.
    # STATUS: FIXED — see #787
    def test_late_body_exit_keeps_live_descendant_home_in_retained_home_slot(self):
        self.test_late_body_exit_keeps_live_descendant_home_in_retained_fixture(home_slot=True)

    def test_late_body_exit_keeps_live_descendant_home_in_retained_fixture(self, *, home_slot=False):
        result = child = None
        expected = 'still used by the live descendant'
        try:
            with tempfile.TemporaryDirectory(prefix='mop-787-late-') as directory, probe_children() as register:
                root = Path(directory)
                foreign = root / 'unrelated-directory'
                foreign.mkdir()
                (foreign / 'sentinel').write_text('not owned by the body')
                driver, child_record, owner_record, result_record = self.prepare(
                    root, close_streams=True, home_slot=home_slot)
                descendant_record, descendant_io = root / 'descendant.json', root / 'descendant-io.json'
                release_body, release_descendant = root / 'release-body', root / 'release-descendant'
                # Detached потомок body: cwd и данные в HOME, который body
                # получил от hermetic внутри своего RUN_ROOT в owned TMPDIR.
                descendant = ('import json, os, pathlib, sys, time\n'
                    'home = pathlib.Path(sys.argv[1])\n'
                    f'(home / "live-descendant-data").write_text({expected!r})\n'
                    f'record = pathlib.Path({str(descendant_record)!r})\n'
                    'record.with_suffix(".tmp").write_text(json.dumps({"pid": os.getpid(), "home": str(home), '
                    '"cwd": os.getcwd()}))\nrecord.with_suffix(".tmp").replace(record)\n'
                    'expires = time.monotonic() + 60\n'
                    f'while not pathlib.Path({str(release_descendant)!r}).exists() and time.monotonic() < expires:\n'
                    '    time.sleep(.01)\nresult = {"home_exists": home.exists()}\n'
                    'try: result["data"] = (home / "live-descendant-data").read_text()\n'
                    'except OSError as error: result["error"] = type(error).__name__ + ": " + str(error)\n'
                    f'io = pathlib.Path({str(descendant_io)!r})\n'
                    'io.with_suffix(".tmp").write_text(json.dumps(result))\nio.with_suffix(".tmp").replace(io)\n'
                    'time.sleep(60)\n')
                (root / 'tests/boundary.py').write_text('import json, os, pathlib, subprocess, sys, time\n'
                    f'sys.path.insert(0, {hermetic.TESTS!r})\nimport hermetic, _lib\n'
                    'fd = os.open(os.devnull, os.O_WRONLY)\nos.dup2(fd, 1)\nos.dup2(fd, 2)\nos.close(fd)\n'
                    f'subprocess.Popen([sys.executable, "-c", {descendant!r}, hermetic.HOME], cwd=hermetic.HOME, '
                    'start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, '
                    'stderr=subprocess.DEVNULL)\nexpires = time.monotonic() + 10\n'
                    f'while not pathlib.Path({str(descendant_record)!r}).exists():\n'
                    '    if time.monotonic() >= expires: raise AssertionError("descendant did not start")\n'
                    '    time.sleep(.01)\n'
                    f'path = pathlib.Path({str(child_record)!r})\n'
                    'path.with_suffix(".tmp").write_text(json.dumps({"pid": os.getpid(), "home": hermetic.HOME, '
                    '"body_root": _lib.RUN_ROOT}))\npath.with_suffix(".tmp").replace(path)\n'
                    'expires = time.monotonic() + 60\n'
                    f'while not pathlib.Path({str(release_body)!r}).exists() and time.monotonic() < expires:\n'
                    '    time.sleep(.01)\n')
                unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                outsider = register(unrelated.pid)
                process = self.launch(driver, root)
                child = self.wait_record(child_record, process)
                body = register(child['pid'])
                grandchild = self.wait_record(descendant_record, process)
                late = register(grandchild['pid'])
                owner = self.wait_record(owner_record, process)
                try:
                    output = process.communicate(timeout=15)[0]
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        process.communicate(timeout=15)
                result = json.loads(result_record.read_text())
                home = Path(result['home'])
                retained = {'home': home.exists(), 'data': (home / 'live-descendant-data').read_text()
                            if (home / 'live-descendant-data').exists() else None,
                            'tmpdir': Path(result['tmpdir']).exists(), 'run_root': Path(result['run_root']).exists()}
                # Body adopted probe subreaper-ом после смерти owner: release —
                # обычный exit body с его atexit, reap — по точному PID/start.
                release_body.touch()
                body_exit, expires = None, time.monotonic() + 10
                while identity(child['pid']) == body and time.monotonic() < expires:
                    try:
                        pid, status = os.waitpid(child['pid'], os.WNOHANG)
                    except ChildProcessError:
                        pid = 0
                    if pid == child['pid']:
                        body_exit = os.waitstatus_to_exitcode(status)
                    time.sleep(.01)
                body_gone = identity(child['pid']) is None
                state = Path(f"/proc/{grandchild['pid']}/stat").read_text().rsplit(')', 1)[1].split()[0]
                after_body = {name: Path(result[name]).exists() for name in ('home', 'tmpdir', 'run_root')}
                release_descendant.touch()
                expires = time.monotonic() + 10
                while not descendant_io.exists() and time.monotonic() < expires:
                    time.sleep(.01)
                io = json.loads(descendant_io.read_text()) if descendant_io.exists() else None
                lease_path = Path(result['tmpdir']) / '.mop-owned-process.json'
                lease = json.loads(lease_path.read_text()) if lease_path.exists() else None
                self.evidence(('#787 direct HOME slot' if home_slot else '#787 fixture') +
                    ' late body exit under retained lease: ' + json.dumps({
                    **result, 'outer_exit': process.returncode, 'owner': owner,
                    'owner_gone': identity(owner['pid']) is None, 'body': body, 'body_root': child['body_root'],
                    'body_exit': body_exit, 'body_reaped': body_gone, 'descendant': late,
                    'descendant_cwd': grandchild['cwd'], 'same_descendant_alive': identity(grandchild['pid']) == late,
                    'descendant_state': state, 'retained_at_outer_exit': retained,
                    'paths_after_late_body_exit': after_body, 'descendant_io': io, 'lease': lease,
                    'unrelated_group_survived': identity(unrelated.pid) == outsider}))
                self.assertEqual(process.returncode, 1, output)
                self.assertIn('quiescence receipt', output)
                self.assertIsNone(identity(owner['pid']))
                self.assertEqual(retained, {'home': True, 'data': expected, 'tmpdir': True, 'run_root': True})
                self.assertEqual(body_exit, 0, 'the body must exit normally through its own atexit')
                self.assertTrue(body_gone)
                self.assertEqual(identity(grandchild['pid']), late)
                self.assertNotIn(state, ('Z', 'X'))
                self.assertEqual(grandchild['cwd'], os.path.realpath(result['home']))
                self.assertTrue(after_body['home'],
                    'late body atexit deleted HOME while its registered descendant remained alive')
                self.assertEqual(after_body, {'home': True, 'tmpdir': True, 'run_root': True})
                self.assertEqual(io, {'home_exists': True, 'data': expected})
                self.assertEqual(lease['state'], 'retained')
                self.assertEqual(lease['owner'], owner)
                self.assertIn('quiescence receipt', lease['error'])
                self.assertEqual(identity(unrelated.pid), outsider)
                self.assertEqual((foreign / 'sentinel').read_text(), 'not owned by the body')
        finally:
            if result is not None:
                self.remove_probe_root(result, child)

    # BUG: lease persistence в bind() падает уже после настоящего старта, и
    # полученный process остаётся без владельца: supervisor и body живы после
    # exit драйвера, его pidfd/control/stdout открыты.
    # HYPOTHESIS: start() записывает retention и raise, не закрывая process;
    # scheduler возвращает startup verdict до регистрации в active, а fixture
    # не доходит до своего finally: handoff ещё не состоялся.
    # SOLUTION: start() сам финализирует полученный, но не переданный process,
    # сохраняя исходную startup error; quiescence receipt освобождает пути, а
    # отказ drain удерживает lease и дописывает cleanup, не заменяя ошибку.
    # RESULT: RED на трёх сценариях — process_closed=false, pidfd/control/stdout
    # открыты, supervisor и body в S после exit драйвера. GREEN Python 3.12/PG17:
    # closed, FD закрыты, owner reaped, body reaped и пути удалены; при смерти
    # owner до receipt — body S, lease retained, HOME/TMPDIR/RUN_ROOT живы;
    # чужая группа и каталог целы. 18 focused + 5 UID checks.
    # STATUS: FIXED — see #788
    def test_bind_failure_finalizes_the_started_home_slot_process(self):
        self.test_bind_failure_finalizes_the_started_fixture_process(home_slot=True)

    def test_bind_failure_with_dead_owner_retains_home_slot_directories(self):
        self.test_bind_failure_finalizes_the_started_fixture_process(home_slot=True, owner_dies=True)

    def test_bind_failure_finalizes_the_started_fixture_process(self, *, home_slot=False, owner_dies=False):
        import textwrap
        result = child = None
        injected = 'injected lease persistence failure after actual start'
        try:
            with tempfile.TemporaryDirectory(prefix='mop-788-bind-') as directory, probe_children() as register:
                root = Path(directory)
                foreign = root / 'unrelated-directory'
                foreign.mkdir()
                (foreign / 'sentinel').write_text('not owned by the failed handoff')
                driver, child_record, owner_record, result_record = self.prepare(root, close_streams=True)
                registered = root / 'registered'
                driver.write_text(textwrap.dedent(f'''
                    import json, os, pathlib, signal, sys, time, traceback
                    sys.path.insert(0, {hermetic.TESTS!r})
                    import hermetic, _lib
                    from mop.cli.dev import test as command
                    command.root = lambda: {str(root)!r}
                    record = _lib.OwnedProcessDirectory._record
                    directories, injected = [], {{}}

                    def links():
                        # Только FD этого драйвера: номер мог быть переиспользован.
                        table = {{}}
                        for name in os.listdir('/proc/self/fd'):
                            try:
                                table[int(name)] = os.readlink('/proc/self/fd/' + name)
                            except OSError:
                                pass
                        return table

                    def refuse_bind_once(directory):
                        if directory not in directories:
                            directories.append(directory)
                        if directory.process is None or injected:
                            return record(directory)
                        process = directory.process
                        expires = time.monotonic() + 10
                        while not pathlib.Path({str(child_record)!r}).exists():
                            if time.monotonic() >= expires:
                                raise AssertionError('bind body did not start')
                            time.sleep(.01)
                        fds = {{'pidfd': process.handle.fd, 'control': process.channel.fileno(),
                                'stdout': process.process.stdout.fileno()}}
                        table = links()
                        injected.update(fds=fds, links={{name: table[fd] for name, fd in fds.items()}},
                                        owner=dict(process.handle.row))
                        path = pathlib.Path({str(owner_record)!r})
                        path.with_suffix('.tmp').write_text(json.dumps(injected['owner']))
                        path.with_suffix('.tmp').replace(path)
                        # Probe регистрирует owner и body по pidfd до финализации.
                        expires = time.monotonic() + 10
                        while not pathlib.Path({str(registered)!r}).exists():
                            if time.monotonic() >= expires:
                                raise AssertionError('probe did not register the started process')
                            time.sleep(.01)
                        if {owner_dies!r}:
                            signal.pidfd_send_signal(process.handle.fd, signal.SIGKILL)
                        raise OSError({injected!r})

                    _lib.OwnedProcessDirectory._record = refuse_bind_once
                    rows = caught = None
                    try:
                        if {home_slot!r}:
                            rows = hermetic.run_cases([('clean', {str(root / 'tests/boundary.py')!r},
                                                        {str(root)!r}, hermetic.child_env({{}}))])
                            code = int(any(row[2] for row in rows))
                        else:
                            code = command.main(['boundary', '--strict'])
                    except BaseException as error:
                        caught = error
                        traceback.print_exception(error)
                        code = 1
                    directory = directories[0]
                    process = directory.process
                    table = links()
                    lease = pathlib.Path(directory.path, _lib.PROCESS_LEASE_NAME)
                    child = json.loads(pathlib.Path({str(child_record)!r}).read_text())
                    pathlib.Path({str(result_record)!r}).write_text(json.dumps({{
                        'code': code, 'caught': repr(caught) if caught is not None else None,
                        'verdict': rows[0][2] if rows else None, 'verdict_text': rows[0][3] if rows else None,
                        'process_closed': process.closed, 'stdout_closed': process.process.stdout.closed,
                        'open_fds': sorted(name for name, link in injected['links'].items()
                                           if table.get(injected['fds'][name]) == link),
                        'open_pidfds': sorted(fd for fd, link in table.items() if link == 'anon_inode:[pidfd]'),
                        'owner_exit': process.returncode,
                        'lease': json.loads(lease.read_text()) if lease.exists() else None,
                        'tmpdir': directory.path, 'home': child['home'], 'run_root': _lib.RUN_ROOT,
                        'home_before_exit': pathlib.Path(child['home']).exists(),
                        'tmpdir_before_exit': pathlib.Path(directory.path).exists(),
                    }}))
                    raise SystemExit(code)
                '''))
                unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                outsider = register(unrelated.pid)
                process = self.launch(driver, root)
                child = self.wait_record(child_record, process)
                body = register(child['pid'])
                owner = self.wait_record(owner_record, process)
                supervisor = register(owner['pid'])
                self.assertEqual({key: supervisor[key] for key in ('pid', 'start', 'group', 'namespace')},
                                 {key: owner[key] for key in ('pid', 'start', 'group', 'namespace')})
                registered.touch()
                try:
                    output = process.communicate(timeout=30)[0]
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        process.communicate(timeout=15)
                result = json.loads(result_record.read_text())
                states = {name: (Path(f"/proc/{row['pid']}/stat").read_text().rsplit(')', 1)[1].split()[0]
                                 if identity(row['pid']) == row else None)
                          for name, row in (('owner', supervisor), ('body', body))}
                paths = {name: Path(result[name]).exists() for name in ('home', 'tmpdir', 'run_root')}
                self.evidence('#788 ' + ('direct HOME slot' if home_slot else 'fixture') +
                    (' dead-owner' if owner_dies else '') + ' bind failure after actual start: ' + json.dumps({
                    **result, 'outer_exit': process.returncode, 'owner': supervisor, 'body': body,
                    'states_after_exit': states, 'paths_after_exit': paths,
                    'unrelated_group_survived': identity(unrelated.pid) == outsider}))
                self.assertEqual(process.returncode, 1, output)
                if home_slot:
                    self.assertEqual(result['verdict'], -1, result)
                    self.assertIn(injected, result['verdict_text'])
                    self.assertIn('Traceback (most recent call last)', result['verdict_text'])
                else:
                    self.assertIn(injected, output)
                self.assertTrue(result['process_closed'], 'bind failure lost the only owner of the started process')
                self.assertTrue(result['stdout_closed'])
                self.assertEqual(result['open_fds'], [], 'pidfd/control/stdout of the started process stayed open')
                self.assertEqual(result['open_pidfds'], [])
                self.assertIsNotNone(result['owner_exit'], 'the started supervisor was not joined')
                self.assertIsNone(states['owner'], 'the started supervisor outlived the driver')
                self.assertIsNone(identity(owner['pid']))
                if owner_dies:
                    # Owner умер до receipt: потомков не убиваем, каталоги и
                    # lease удерживаются после выхода настоящего интерпретатора.
                    self.assertEqual(identity(child['pid']), body)
                    self.assertNotIn(states['body'], ('Z', 'X', None))
                    self.assertIn('quiescence receipt', result['verdict_text'])
                    self.assertEqual(paths, {'home': True, 'tmpdir': True, 'run_root': True})
                    self.assertEqual(result['lease']['state'], 'retained')
                    self.assertEqual(result['lease']['error'], injected)
                    self.assertEqual(result['lease']['owner'], owner)
                    self.assertIn('quiescence receipt', result['lease']['cleanup'])
                    self.assertIn('retained owned process directories', output)
                else:
                    self.assertIsNone(states['body'], 'the started body outlived the driver')
                    self.assertIsNone(identity(child['pid']))
                    # Quiescence доказана receipt-ом: owned пути удалены.
                    self.assertIsNone(result['lease'])
                    self.assertEqual(paths, {'home': False, 'tmpdir': False, 'run_root': False})
                self.assertEqual(identity(unrelated.pid), outsider)
                self.assertEqual((foreign / 'sentinel').read_text(), 'not owned by the failed handoff')
        finally:
            if result is not None:
                self.remove_probe_root(result, child)

    def remove_probe_root(self, result, child):
        import _lib
        self.assertIsNone(identity(child['pid']), 'retained directories cannot be removed while body exists')
        root = Path(result['run_root'])
        self.assertEqual(root.parent, Path(_lib.RUN_ROOT), 'probe may clean only its own child run root')
        if root.exists():
            shutil.rmtree(root)

    def test_successful_direct_home_slot_removes_only_its_owned_quiet_paths_after_exit(self):
        self.test_successful_fixture_still_removes_its_owned_root_after_actual_cli_exit(home_slot=True)

    def test_successful_fixture_still_removes_its_owned_root_after_actual_cli_exit(self, *, home_slot=False):
        with tempfile.TemporaryDirectory(prefix='mop-776-safe-') as directory:
            root = Path(directory)
            foreign = root / 'unrelated-directory'
            foreign.mkdir()
            (foreign / 'sentinel').write_text('not owned by the CLI lifetime')
            driver, child_record, _, result_record = self.prepare(
                root, close_streams=False, kill_owner=False, home_slot=home_slot)
            process = self.launch(driver, root)
            output = process.communicate(timeout=15)[0]
            result = json.loads(result_record.read_text())
            child = json.loads(child_record.read_text())
            self.assertEqual(process.returncode, 0, output)
            self.assertIsNone(identity(child['pid']))
            for name in ('home', 'tmpdir', 'run_root'):
                self.assertFalse(Path(result[name]).exists(), (name, result))
            self.assertEqual((foreign / 'sentinel').read_text(), 'not owned by the CLI lifetime')
            self.evidence(('#779 direct HOME slot' if home_slot else '#776 fixture') +
                ' successful post-exit cleanup: ' + json.dumps({
                **result, 'outer_exit': process.returncode, 'body_reaped': True,
                'home_after_exit': False, 'tmpdir_after_exit': False, 'run_root_after_exit': False,
                'unrelated_directory_survived': True}))


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:] or ['pytest_ownership']))
