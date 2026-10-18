"""Безопасность диагностики и owned subprocess installed UID seed (#754)."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest

import _lib  # noqa: F401 — lifetime собственных файлов
from _credential_uid_seed import run_seed


class InstalledSeedDiagnostics(unittest.TestCase):
    def invoke(self, code, directory, *, activity=lambda: [], timeout=2):
        return run_seed([sys.executable, '-I', '-c', code], cwd=directory,
                        env={}, activity=activity, timeout=timeout)

    def test_private_stdout_is_returned_without_printing_token(self):
        token = 'fixture-only-private-token-754'
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(stream):
            auth = self.invoke('import json; print(json.dumps(' + repr({'token': token}) + '))', directory)
        self.assertEqual(auth, {'token': token})
        self.assertNotIn(token, stream.getvalue())

    def test_failure_reports_phase_sqlstate_but_not_raw_output(self):
        token = 'fixture-only-private-token-754'
        record = {'phase': 'create-user', 'event': 'error', 'elapsed': 0.1,
                  'error_type': 'OperationalError', 'sqlstate': '55P03'}
        code = ('import sys; print(' + repr(token) + '); print(' + repr(token) + ', file=sys.stderr); '
                'print(' + repr(json.dumps(record)) + ', file=sys.stderr); sys.exit(1)')
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(stream):
            with self.assertRaises(AssertionError) as caught:
                self.invoke(code, directory)
        self.assertIn('55P03', stream.getvalue())
        self.assertIn('create-user', stream.getvalue())
        self.assertNotIn(token, stream.getvalue() + str(caught.exception))

    # HYPOTHESIS: allowlist только ключей JSON допускает credential в phase;
    # ошибка observer тоже не должна пробить безопасную границу диагностики.
    # SOLUTION: проверяем значения metadata, изолируем ошибку observer, stdout private.
    # RESULT: наблюдались FAIL (phase leak) и ERROR (observer); затем оба green.
    # STATUS: diagnostic checks green; исходный seed timeout не объяснён — see #754.
    def test_diagnostics_reject_unrecognized_phase_values(self):
        token = 'fixture-only-private-token-754'
        code = 'import sys; print(' + repr(json.dumps({'phase': token})) + ', file=sys.stderr); sys.exit(1)'
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(stream):
            with self.assertRaises(AssertionError) as caught:
                self.invoke(code, directory)
        self.assertNotIn(token, stream.getvalue() + str(caught.exception))

    def test_observer_failure_does_not_replace_timeout_or_emit_secrets(self):
        token = 'fixture-only-private-token-754'
        def unavailable():
            raise RuntimeError(token)
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(stream):
            with self.assertRaisesRegex(AssertionError, 'time budget') as caught:
                self.invoke('import time; time.sleep(30)', directory, activity=unavailable, timeout=.1)
        self.assertIn('diagnostic unavailable', stream.getvalue())
        self.assertNotIn(token, stream.getvalue() + str(caught.exception))

    # HYPOTHESIS 1 (WRONG): zombie leader исчезнет до kill(pid). communicate
    # ещё не сделал wait, поэтому baseline уже сохраняет process group.
    # Это проверка cleanup, НЕ воспроизведение исходного seed timeout.
    def test_timeout_reaps_owned_group_even_when_leader_already_exited(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'owned-pids.json'
            code = '''
import json, os, signal, time
from pathlib import Path
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    time.sleep(30)
    os._exit(0)
Path('owned-pids.json').write_text(json.dumps([os.getpid(), pid]))
os._exit(0)
'''
            try:
                with redirect_stdout(stream), self.assertRaisesRegex(AssertionError, 'time budget'):
                    self.invoke(code, directory, timeout=.2)
                leader, child = json.loads(marker.read_text())
                # Завершившийся descendant может ещё ждать reaper как zombie;
                # это не работающий процесс/открытый pipe/socket.
                status = Path('/proc') / str(child) / 'stat'
                if status.exists():
                    self.assertEqual(status.read_text().split()[2], 'Z')
            finally:
                if marker.exists():
                    leader, child = json.loads(marker.read_text())
                    try:
                        os.killpg(leader, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    unittest.main()
