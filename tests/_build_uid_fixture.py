"""Только owned Docker fixture: настоящий executor uid отличается от caller uid.

Запускается SETUP до su ci. Никаких host users, PVE, pool или секретов установки.
"""
import os
import sys
from pathlib import Path
import multiprocessing
import tempfile
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def executor(path, connection, caller_uid, ids):
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    from mop.server.builds.contracts import BuildClaim, StageResult
    from mop.server.builds.executor import PrivilegedExecutor
    from mop.server.builds.subprocess import SafeAnsibleSubprocess, TrustedInvocation
    claim = BuildClaim(ids[0], ids[1], ids[2], ids[3])
    ticket = SimpleNamespace(id=ids[4], stage='build', claim=claim)
    counts = {'apply':0, 'inspect':0, 'record':0}
    class Port:
        capabilities = frozenset({'image_builder','node_executor','inspect'})
        def apply(self, value):
            counts['apply'] += 1
            logs = Path(path).parent/'logs'
            logs.mkdir(mode=0o700, exist_ok=True)
            runner = SafeAnsibleSubprocess(private_directory=str(logs), known_secrets=('fixture-secret',))
            secret = logs/'secret.txt'
            secret.write_text('fixture-secret')
            secret.chmod(0o600)
            result = runner.run(TrustedInvocation((sys.executable, '-I', '-c',
                'import os; print(os.getuid()); print(open("secret.txt").read())'), str(logs), ()), timeout=5)
            assert 'fixture-secret' not in result.tail
            assert str(os.getuid()) in result.tail
            return StageResult(result.outcome, result.quiescent, 'isolated subprocess uid '+str(os.getuid()))
        def inspect(self, value):
            counts['inspect'] += 1
            return StageResult('succeeded', True, 'isolated readback confirmed')
    dispatched = False
    def resolve(**request):
        nonlocal dispatched
        if any(request[key] != value for key, value in dict(intent_id=ids[4], step_id=ids[1], token=ids[2], epoch=ids[3]).items()):
            raise PermissionError('fixture claim mismatch')
        if request['verb']=='apply':
            if dispatched:
                raise PermissionError('fixture dispatch already consumed')
            dispatched = True
        return ticket
    def record(*args):
        counts['record'] += 1
    server = PrivilegedExecutor(path=path, caller_uid=caller_uid, resolve=resolve, record=record,
        image_builder=Port(), node_executor=Port())
    server.open()
    connection.send(('ready', os.getuid()))
    try:
        for _ in range(5):
            server.serve_one()
        connection.send(counts)
    finally:
        server.close()
        connection.close()


def worker_client(path, ids):
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65533)
    from mop.server.builds.executor import UnixPrivilegedClient
    from mop.server.builds.contracts import BuildClaim
    client = UnixPrivilegedClient(path=path, executor_uid=65534, timeout=10)
    ticket = SimpleNamespace(id=ids[4], claim=BuildClaim(ids[0], ids[1], ids[2], ids[3]))
    first = client.apply(ticket)
    assert first.outcome == 'succeeded' and 'uid 65534' in first.reason, first
    try:
        (Path(path).parent/'logs'/'secret.txt').read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('ordinary worker could read privileged private input')
    assert client.apply(ticket).outcome == 'unknown', 'dispatch replay reached executor'
    assert client.inspect(ticket).outcome == 'succeeded'
    wrong = SimpleNamespace(id=ids[4], claim=BuildClaim(ids[0], ids[1], uuid4(), ids[3]))
    assert client.apply(wrong).outcome == 'unknown'


def main():
    if os.getuid() != 0:
        raise RuntimeError('UID fixture must run inside the owned Docker SETUP before su ci')
    from mop.server.builds.executor import UnixPrivilegedClient
    from mop.server.builds.contracts import BuildClaim
    ids = tuple(uuid4() for _ in range(5))
    with tempfile.TemporaryDirectory(prefix='mop-build-executor-fixture-') as directory:
        os.chown(directory, 65534, 65534)
        os.chmod(directory, 0o770)
        path = str(Path(directory)/'executor.sock')
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=executor, args=(path, child, 65533, ids))
        process.start()
        try:
            assert parent.poll(15), 'executor did not open its socket'
            assert parent.recv() == ('ready', 65534)
            worker = os.fork()
            if worker == 0:
                try:
                    worker_client(path, ids)
                except BaseException as error:
                    print('isolated non-root worker failed:', type(error).__name__, flush=True)
                    os._exit(1)
                os._exit(0)
            assert os.waitpid(worker, 0)[1] == 0, 'non-root worker fixture failed'
            # Даже root caller не входит в enrollment обычного worker uid.
            ticket = SimpleNamespace(id=ids[4], claim=BuildClaim(ids[0], ids[1], ids[2], ids[3]))
            denied = UnixPrivilegedClient(path=path, executor_uid=65534, timeout=10).apply(ticket)
            assert denied.outcome == 'unknown' and 'PermissionError' in denied.reason, 'unenrolled OS peer reached the executor'
            assert parent.poll(15), 'executor did not finish all framed requests'
            assert parent.recv() == {'apply':1,'inspect':1,'record':2}
            process.join(timeout=5)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            parent.close()
            child.close()
    print('privileged build executor: separate UID, own Unix socket, scoped claim, no replay, bounded subprocess verified')


if __name__ == '__main__':
    main()
