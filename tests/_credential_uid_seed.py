"""Private stdout и lifecycle только installed UID seed (#754), не runtime adapter."""
import json
import math
import os
import re
import signal
import subprocess


_PHASES = frozenset({'import-runtime', 'configure', 'prepare-database', 'import-models',
    'create-user', 'create-identity', 'create-project', 'add-permission', 'issue-token',
    'close-pools', 'maintenance-migrate', 'maintenance-initialize',
    'maintenance-collectstatic', 'maintenance-check', 'threads'})


def _diagnostic(line):
    try:
        value = json.loads(line)
    except ValueError:
        return None
    if (type(value) is not dict or type(value.get('phase')) is not str
            or value['phase'] not in _PHASES or type(value.get('event')) is not str
            or value['event'] not in {'start', 'done', 'error', 'snapshot'}
            or type(value.get('elapsed')) not in (int, float)
            or not math.isfinite(value['elapsed']) or value['elapsed'] < 0):
        return None
    record = {key: value[key] for key in ('phase', 'event', 'elapsed')}
    error = value.get('error_type')
    if type(error) is str and re.fullmatch(r'[A-Z][A-Za-z0-9_]{0,63}', error):
        record['error_type'] = error
    state = value.get('sqlstate')
    if type(state) is str and re.fullmatch(r'[0-9A-Z]{5}', state):
        record['sqlstate'] = state
    stacks = value.get('stacks')
    if value['phase'] == 'threads' and type(stacks) is list:
        # Только имена source-файлов/функций и номера строк, никогда locals/SQL.
        record['stacks'] = [[item for item in stack if type(item) is list and len(item) == 3
            and type(item[0]) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}\.py|<string>', item[0])
            and type(item[1]) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}|<module>', item[1])
            and type(item[2]) is int and 0 < item[2] < 100000]
            for stack in stacks[:32] if type(stack) is list]
    return record


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_seed(argv, *, cwd, env, activity, timeout=90):
    """Никогда не выводит stdout ребёнка или сырые stderr/исключения subprocess."""
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True)
    timed_out = False
    try:
        output, error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            print('installed seed owned activity:', json.dumps(activity()), flush=True)
        except Exception:
            # Ошибка observer не заменяет исходный timeout и не печатает DSN.
            print('installed seed diagnostic unavailable', flush=True)
        try:
            os.kill(process.pid, signal.SIGUSR1)
        except ProcessLookupError:
            pass
        try:
            output, error = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            output, error = process.communicate()
    finally:
        if process.poll() is None:
            _kill_group(process)
            process.communicate()
    for line in error.splitlines():
        record = _diagnostic(line)
        if record is not None:
            print('installed seed phase:', json.dumps(record), flush=True)
    if timed_out:
        raise AssertionError('installed seed exceeded its time budget (see safe phase/activity metadata)')
    if process.returncode:
        raise AssertionError('installed seed failed (see safe phase metadata)')
    return json.loads(output)
