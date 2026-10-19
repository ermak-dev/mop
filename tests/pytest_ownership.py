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
        joined = []

        class Owner:
            returncode = 0

            def __init__(self, *args, **kwargs):
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
    def prepare(self, root, *, close_streams, kill_owner=True):
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
        '''))
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
    def test_missing_receipt_retains_live_body_home_and_tempdir_after_actual_cli_exit(self):
        result = child = None
        try:
            with tempfile.TemporaryDirectory(prefix='mop-776-retained-') as directory, probe_children() as register:
                root = Path(directory)
                driver, child_record, owner_record, result_record = self.prepare(root, close_streams=True)
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
                    self.evidence('#776 post-interpreter-exit retention: ' + json.dumps({
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

    def remove_probe_root(self, result, child):
        import _lib
        self.assertIsNone(identity(child['pid']), 'retained directories cannot be removed while body exists')
        root = Path(result['run_root'])
        self.assertEqual(root.parent, Path(_lib.RUN_ROOT), 'probe may clean only its own child run root')
        if root.exists():
            shutil.rmtree(root)

    def test_successful_fixture_still_removes_its_owned_root_after_actual_cli_exit(self):
        with tempfile.TemporaryDirectory(prefix='mop-776-safe-') as directory:
            root = Path(directory)
            foreign = root / 'unrelated-directory'
            foreign.mkdir()
            (foreign / 'sentinel').write_text('not owned by the CLI lifetime')
            driver, child_record, _, result_record = self.prepare(root, close_streams=False, kill_owner=False)
            process = self.launch(driver, root)
            output = process.communicate(timeout=15)[0]
            result = json.loads(result_record.read_text())
            child = json.loads(child_record.read_text())
            self.assertEqual(process.returncode, 0, output)
            self.assertIsNone(identity(child['pid']))
            for name in ('home', 'tmpdir', 'run_root'):
                self.assertFalse(Path(result[name]).exists(), (name, result))
            self.assertEqual((foreign / 'sentinel').read_text(), 'not owned by the CLI lifetime')
            self.evidence('#776 successful post-exit cleanup: ' + json.dumps({
                **result, 'outer_exit': process.returncode, 'body_reaped': True,
                'home_after_exit': False, 'tmpdir_after_exit': False, 'run_root_after_exit': False,
                'unrelated_directory_survived': True}))


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:] or ['pytest_ownership']))
