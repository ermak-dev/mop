"""Actual owned root/node/body acceptance трёх planned phases (#752).

HYPOTHESIS: metadata-only фазы не исполняют checkout/bootstrap/identity.
SOLUTION: actual MachineClient HTTPS checkpoints и root-owned systemd broker.
RESULT: actual Git HTTPS/SSH, retained Ansible, identity, recovery и negatives;
никаких native/ready или production RBAC claims. Запускается только probe.
STATUS: FIXED — see #752
"""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import threading
from uuid import uuid4

sys.path.insert(0, '/opt/fixture')
sys.path.insert(0, '/opt/runtime/python')
NODE, BODY = 21071, 21072
ROOT = Path('/opt/fixture')
SOCKET = Path('/run/mop-host/backend.sock')
INSTALLATION = 'owned-host-planned-fixture'
TOKEN = 'fixture-node-bearer-'+secrets.token_hex(24)
SOURCE_PASSWORD = 'fixture-source-password-'+secrets.token_hex(20)


def run(*args, **kw):
    return subprocess.run(args, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=dict(PATH='/usr/bin:/bin', HOME='/root', LANG='C.UTF-8'), **kw).stdout


def content_manifest(image, policy):
    from mop.node.host_root import ROOT_SOURCES
    from mop.node.host_body import BODY_SOURCES
    artifacts = {}
    def add(p, executable=False):
        p = Path(p)
        # Fixture копирует symlink targets в immutable runtime closure.
        if p.is_symlink():
            data, mode = p.read_bytes(), p.stat().st_mode & 0o777
            p.unlink()
            p.write_bytes(data)
            p.chmod(mode)
        artifacts[str(p)] = dict(path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest(), executable=executable)
    tools = dict(python=str(Path('/usr/bin/python3').resolve()), git='/usr/bin/git', ssh='/usr/bin/ssh',
        ansible='/opt/runtime/python/bin/ansible-playbook', runner=str(ROOT/'mop/node/host_body.py'),
        ca='/opt/runtime/ca.pem', ansible_config='/opt/runtime/ansible.cfg')
    directories = dict(empty='/opt/runtime/empty', git_exec='/usr/lib/git-core',
        python_root='/opt/runtime/python', ansible_plugins='/opt/runtime/plugins')
    for name in set(ROOT_SOURCES) | set(BODY_SOURCES): add(ROOT/name, name in ('bin/mop', 'mop/node/host_body.py'))
    for filename in tools.values(): add(filename, filename not in (tools['ca'], tools['ansible_config']))
    for directory in directories.values():
        for parent, dirs, files in os.walk(directory, followlinks=False):
            for name in files: add(Path(parent)/name, bool((Path(parent)/name).stat().st_mode & 0o111))
    return dict(schema_version=1,
        capacity=dict(cpu_mhz_per_core=2000, cpu_weight=75, tasks_max=64,
                      memory_mb_max=4096, cpu_mhz_max=8000, disk_mode='shared-reservation'),
        images=[dict(image_revision=image, logical_pin=hashlib.sha256(('image-revision:'+image).encode()).hexdigest(), policy_id=policy)],
        tools=tools, directories=directories, artifacts=list(artifacts.values()))


class Authority:
    """Fixture authority использует реальный PostgreSQL, не production RBAC gate."""
    def __init__(self, db, values, epoch, node, previous):
        self.db, self.values = db, values
        self.epoch, self.node, self.executor = epoch, node, previous
        self.results, self.checks = [], []
        self.block, self.revoke = None, False
        self.lock = threading.Lock()
        with db.cursor() as cursor:
            cursor.execute('CREATE TABLE admissions (step text PRIMARY KEY, request text, attempt text UNIQUE, revision bigint, lease text, revoked boolean NOT NULL DEFAULT false)')
            cursor.execute('CREATE TABLE result_receipts (receipt text PRIMARY KEY, attempt text UNIQUE, expected_revision text, outcome text, evidence text)')
        self.drop_result_once = False
        self.lease_limits = {}
        db.commit()

    def call(self, route, payload):
        with self.lock:
            if route == 'executor/host':
                return dict(schema_version=1, installation_id=INSTALLATION, epoch=self.epoch,
                    node_id=self.node, executor_epoch=self.executor, empty=not bool(self.results))
            if route == 'executor/host/bind':
                assert payload['previous_epoch'] == self.executor
                self.executor = payload['new_epoch']
                for value in self.values:
                    value['work']['executor_epoch'] = self.executor
                return dict(schema_version=1, executor_epoch=self.executor, replay_allowed=False)
            if route == 'work/initial-launch':
                return next(value for value in self.values if value['work'] == payload)
            if route == 'work/claim':
                work = payload['work']
                if self.revoke: raise PermissionError('fixture admission revoked')
                with self.db.cursor() as cursor:
                    cursor.execute('SELECT request,attempt,revision,lease,revoked FROM admissions WHERE step=%s', (work['step_id'],))
                    row = cursor.fetchone()
                    if row is None:
                        lease = min(datetime.fromisoformat(work['not_after']), datetime.now(timezone.utc)+timedelta(seconds=self.lease_limits.get(work['step_id'], 320)))
                        row = (payload['request_id'], str(uuid4()), 1, lease.isoformat(), False)
                        cursor.execute('INSERT INTO admissions(step,request,attempt,revision,lease) VALUES(%s,%s,%s,%s,%s)', (work['step_id'], *row[:4]))
                        self.db.commit()
                    if row[0] != payload['request_id'] or row[4] or datetime.fromisoformat(row[3]) <= datetime.now(timezone.utc):
                        raise PermissionError('fixture claim conflict or expiry')
                self.checks.append(work['action'])
                return dict(schema_version=1, attempt_id=row[1], revision=str(row[2]), state='claimed', lease_until=row[3], work=work)
            if route == 'work/result':
                with self.db.cursor() as cursor:
                    cursor.execute('SELECT receipt,attempt,expected_revision,outcome,evidence FROM result_receipts WHERE receipt=%s', (payload['receipt_id'],))
                    previous = cursor.fetchone()
                    row = tuple(payload[k] for k in ('receipt_id', 'attempt_id', 'expected_revision', 'outcome', 'evidence'))
                    if previous is None:
                        cursor.execute('SELECT revision FROM admissions WHERE attempt=%s', (payload['attempt_id'],))
                        assert cursor.fetchone() == (int(payload['expected_revision']),)
                        cursor.execute('INSERT INTO result_receipts VALUES (%s,%s,%s,%s,%s)', row)
                        cursor.execute('UPDATE admissions SET revision=revision+1 WHERE attempt=%s', (payload['attempt_id'],))
                        self.db.commit()
                        self.results.append(dict(payload))
                    else:
                        assert previous == row, 'result receipt replay changed committed evidence'
                        self.db.commit()
                return dict(schema_version=1, **{key: payload[key] for key in ('receipt_id', 'attempt_id', 'outcome', 'evidence')}, revision='2')
            raise AssertionError('unexpected fixture route '+route)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()
    def handle_request(self):
        from urllib.parse import urlsplit
        path = urlsplit(self.path).path
        if path.startswith('/api/v1/machine/'):
            if self.headers.get('Authorization') != 'Token '+TOKEN:
                self.send_error(403)
                return
            size = int(self.headers.get('Content-Length', '0'))
            payload = json.loads(self.rfile.read(size)) if size else None
            try:
                value = self.server.authority.call(path.removeprefix('/api/v1/machine/'), payload)
                raw = json.dumps(value).encode()
            except PermissionError:
                self.send_response(409)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error":"execution_authority_changed"}')
                return
            if path.endswith('/work/result') and self.server.authority.drop_result_once:
                # COMMIT уже завершён; теряется только реальный HTTP response.
                self.server.authority.drop_result_once = False
                self.close_connection = True
                self.connection.shutdown(2)
                self.connection.close()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        expected = 'Basic '+base64.b64encode(('fixture:'+SOURCE_PASSWORD).encode()).decode()
        if self.headers.get('Authorization') != expected:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="owned-fixture"')
            self.end_headers()
            return
        size = int(self.headers.get('Content-Length', '0'))
        from urllib.parse import urlsplit
        u = urlsplit(self.path)
        p = subprocess.run(('/usr/lib/git-core/git-http-backend',), input=self.rfile.read(size),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, user=21073, group=21073, extra_groups=(),
            env=dict(PATH='/usr/bin:/bin', GIT_PROJECT_ROOT='/srv/git', GIT_HTTP_EXPORT_ALL='1',
                PATH_INFO=u.path, QUERY_STRING=u.query, REQUEST_METHOD=self.command,
                CONTENT_TYPE=self.headers.get('Content-Type', ''), CONTENT_LENGTH=str(size),
                REMOTE_USER='fixture', REMOTE_ADDR='127.0.0.1', GIT_CONFIG_NOSYSTEM='1'))
        headers, separator, data = p.stdout.partition(b'\r\n\r\n')
        if not separator: raise AssertionError('invalid controlled Git CGI response')
        status = 200
        pairs = []
        for line in headers.split(b'\r\n'):
            key, value = line.decode().split(': ', 1)
            if key == 'Status': status = int(value.split()[0])
            else: pairs.append((key, value))
        self.send_response(status)
        for key, value in pairs: self.send_header(key, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def frontend(document):
    assert os.geteuid() == NODE
    from mop.node.host_backend import Identity
    from mop.node.host_backend_client import BackendClient
    from mop.node.host_input_files import NodeWriter
    from mop.node.host_phase_client import PlannedExecutor
    from mop.node.http_execution import HttpsTransport, MachineClient, Work
    from mop.node.native_startup import start
    client = MachineClient(HttpsTransport(document['origin'], document['token'],
        timeout=5., tls=ssl.create_default_context(cafile='/opt/runtime/ca.pem')))
    backend = BackendClient(SOCKET, node_uid=NODE, identity=Identity(INSTALLATION, document['node']))
    writer = NodeWriter(Path('/var/lib/mop-node/inputs'), node_uid=NODE)
    executor = PlannedExecutor(client, backend, writer)
    runtime = await start(client, executor, installation_id=INSTALLATION, node_id=document['node'], backend=backend)
    try:
        from mop.node.host_scope import Scope, decode, encode as frame_encode
        from mop.node.host_scope_supervisor import read_frame
        from mop.node.host_inputs import admission_wire
        from mop.node.host_phases import Projection, Submission, parse_admission
        from mop.node.http_execution import EffectGuard, Refused, Unavailable
        mode = document.get('mode', 'run')
        def phase_guard(admission):
            def check():
                if datetime.now(timezone.utc) >= admission.lease_until or admission.work.executor_epoch != runtime.executor_epoch:
                    raise Refused('original fixture admission expired or backend changed')
            async def authorize(): await client.recheck(admission)
            return EffectGuard(authorize, check, admission)
        if mode == 'ingress_eligibility':
            calls, evidence = [], []
            original = client.read_initial_launch
            async def delivery(work):
                calls.append(work['action'])
                return await original(work)
            client.read_initial_launch = delivery
            for value in document['works']:
                work = Work.parse({**value, 'executor_epoch': runtime.executor_epoch})
                admission = await client.claim(work, str(uuid4()))
                try: await executor.execute(work, phase_guard(admission))
                except (Refused, Unavailable): pass
                else: raise AssertionError('ineligible actual planned action executed')
                evidence.append(dict(action=work.action, slots=len(writer.retained),
                    files=sum((writer.directory.path/s.descriptor.filename).exists() for s in writer.retained)))
            assert not calls and not writer.retained, '#759 actual eligibility leaked private input: '+repr(evidence)
            print(json.dumps(dict(status='refused-before-delivery')), flush=True)
            return
        if mode == 'partial_staging':
            from contextlib import asynccontextmanager
            from unittest.mock import patch
            from mop.node import host_input_files as files
            for observation in document.get('observations', []):
                await executor.observe(Scope.parse(observation['scope']), observation['attempt_id'])
            work = Work.parse({**document['works'][0], 'executor_epoch': runtime.executor_epoch})
            admission = await client.claim(work, str(uuid4()))
            original, hit, connections, paths = os.fsync, [], [], []
            @asynccontextmanager
            async def unexpected_connect():
                connections.append(True)
                raise AssertionError('partial staging reached backend connect')
                yield
            backend.connect = unexpected_connect
            def fail_once(fd):
                if fd == writer.directory.fd and not hit:
                    hit.append(True)
                    source = writer.retained[0]
                    path = writer.directory.path/source.descriptor.filename
                    paths.append(path)
                    assert path.stat().st_uid == NODE and path.stat().st_size > 0
                    raise OSError('controlled actual directory fsync before staging token')
                return original(fd)
            with patch.object(files.os, 'fsync', fail_once):
                try: await executor.execute(work, phase_guard(admission))
                except Refused: pass
                else: raise AssertionError('actual partial stage did not fail')
            await executor.aclose()
            assert hit == [True] and not connections and not executor._owned
            assert not writer.retained and paths and all(not path.exists() for path in paths)
            print(json.dumps(dict(status='compensated-partial-stage', action=work.action,
                backend_calls=0, executor_owners=0, writer_slots=0, private_bytes=0)), flush=True)
            return
        if mode == 'ingress_failure':
            from contextlib import asynccontextmanager
            from types import SimpleNamespace
            from mop.node.host_scope import Unknown
            work = Work.parse({**document['works'][0], 'executor_epoch': runtime.executor_epoch})
            admission = await client.claim(work, str(uuid4()))
            guard = phase_guard(admission)
            original, staged_paths = backend.connect, []
            @asynccontextmanager
            async def boundary():
                assert len(writer.retained) == 1, 'fault missed actual poststage boundary'
                staged_paths.append(writer.directory.path/writer.retained[0].descriptor.filename)
                assert staged_paths[-1].exists()
                async with original() as session:
                    if document['failure'] == 'definite':
                        raise Unavailable('controlled actual pre-submit failure')
                    async def dispatch(request, **kw):
                        await session.planned(request, **kw)
                        raise Unknown('controlled actual lost phase outcome')
                    yield SimpleNamespace(planned=dispatch)
            backend.connect = boundary
            try:
                try: await executor.execute(work, guard)
                except (Unavailable, Unknown): pass
                else: raise AssertionError('controlled actual boundary did not fail')
            finally:
                backend.connect = original
            await executor.aclose()
            if document['failure'] == 'definite':
                assert not writer.retained, '#759 actual definite pre-submit failure leaked slot'
                assert admission.attempt_id not in executor._owned
                assert staged_paths and all(not path.exists() for path in staged_paths)
            else:
                owned = executor._owned[admission.attempt_id]
                assert len(writer.retained) == 1 and owned.possible_dispatch
                path = writer.directory.path/owned.descriptor.filename
                inode = path.stat().st_ino
                try: await executor.execute(work, guard)
                except Refused as error: assert 'replay' in str(error)
                else: raise AssertionError('unknown actual dispatch permitted replay')
                assert path.stat().st_ino == inode, 'unknown dispatch deleted/replaced owned input'
                await executor.exchange(executor.address(owned, 'cleanup_owned'))
                async with asyncio.timeout(10.):
                    while path.exists(): await asyncio.sleep(.02)
                writer.forget(owned.descriptor)
            print(json.dumps(dict(status='accepted-boundary', failure=document['failure'])), flush=True)
            return
        if mode == 'deny_token':
            bad = MachineClient(HttpsTransport(document['origin'], 'wrong-owned-fixture-token',
                timeout=5., tls=ssl.create_default_context(cafile='/opt/runtime/ca.pem')))
            try:
                await bad.host_executor_state(document['node'])
            except Refused:
                pass
            else:
                raise AssertionError('wrong actual machine token accepted')
            print(json.dumps(dict(status='refused', negative='node-token')), flush=True)
            return
        if mode in ('deny_input', 'expired_guard'):
            from mop.node.host_inputs import Descriptor
            from mop.node.host_scope import Reference
            work = Work.parse({**document['works'][0], 'executor_epoch': runtime.executor_epoch})
            admission = await client.claim(work, str(uuid4()))
            if mode == 'expired_guard':
                await asyncio.sleep(2.2)
                try:
                    await phase_guard(admission)()
                except Refused:
                    pass
                else:
                    raise AssertionError('expired real SQL admission authorized a new effect')
            else:
                scope = executor.scope(work)
                delivery = await client.read_initial_launch(work.wire())
                descriptor = writer.write(admission, scope, delivery)
                submitted = descriptor
                reference = None
                if document['negative'] == 'snapshot':
                    submitted = Descriptor(scope, admission.attempt_id, '0'*64)
                elif document['negative'] == 'phase':
                    reference = Reference(scope, uuid4().hex, str(uuid4()))
                request = Submission(admission, submitted, Projection.from_delivery(delivery), reference)
                try:
                    await executor.exchange(request)
                except Refused:
                    pass
                else:
                    raise AssertionError('invalid actual phase input accepted: '+document['negative'])
                if (Path('/var/lib/mop-node/inputs')/descriptor.filename).exists():
                    # До capture у Root нет receipt и cleanup authority. Этот
                    # rejected node-only inode уберёт только owned container teardown.
                    assert document['negative'] in ('snapshot', 'phase')
                else:
                    writer.forget(descriptor)
            print(json.dumps(dict(status='refused', negative=document.get('negative', mode))), flush=True)
            return
        if mode == 'stage':
            work = Work.parse({**document['works'][0], 'executor_epoch': runtime.executor_epoch})
            admission = await client.claim(work, str(uuid4()))
            scope = executor.scope(work)
            delivery = await client.read_initial_launch(work.wire())
            descriptor = writer.write(admission, scope, delivery)
            submission = Submission(admission, descriptor, Projection.from_delivery(delivery))
            phase = await executor.exchange(submission)
            async with asyncio.timeout(60.):
                while True:
                    assert phase['status'] == 'accepted', 'staged phase did not retain its original task'
                    checkpoint = phase['checkpoint']
                    if checkpoint is not None and checkpoint['effect'] == document['stop_before']:
                        break
                    request = dict(schema_version=1, scope=scope.wire(), attempt_id=admission.attempt_id,
                        action='phase_checkpoint' if checkpoint else 'phase_status')
                    phase = await executor.exchange(request, guard=phase_guard(admission) if checkpoint else None)
                    if not checkpoint: await asyncio.sleep(.02)
            print(json.dumps(dict(status='retained', admission=admission_wire(admission), scope=scope.wire(), reference=phase['reference'])), flush=True)
            return
        if mode == 'approve_start':
            original = parse_admission(document['admission'])
            await executor.exchange(dict(schema_version=1, action='phase_checkpoint',
                scope=executor.scope(original.work).wire(), attempt_id=original.attempt_id), guard=phase_guard(original))
            print(json.dumps(dict(status='accepted-start')), flush=True)
            return
        if mode == 'cleanup_observed':
            observation = document['observations'][0]
            phase = await executor.observe(Scope.parse(observation['scope']), observation['attempt_id'])
            document['admission'], mode = phase['admission'], 'cleanup'
        if mode in ('checkpoint_denied', 'cleanup'):
            original = parse_admission(document['admission'])
            scope = executor.scope(original.work)
            if mode == 'checkpoint_denied':
                try:
                    await executor.resume(original.work, phase_guard(original))
                except Refused:
                    pass
                else:
                    raise AssertionError('revoked actual SQL checkpoint authorized another effect')
            else:
                for _ in range(3):
                    await executor.exchange(dict(schema_version=1, action='cleanup_owned', scope=scope.wire(), attempt_id=original.attempt_id))
            print(json.dumps(dict(status='refused' if mode=='checkpoint_denied' else 'retained-cleanup')), flush=True)
            return
        if mode == 'expect_failure':
            for observation in document.get('observations', []):
                await executor.observe(Scope.parse(observation['scope']), observation['attempt_id'])
            work = Work.parse({**document['works'][0], 'executor_epoch': runtime.executor_epoch})
            admission = await client.claim(work, str(uuid4()))
            try:
                await executor.execute(work, phase_guard(admission))
            except (Refused, Unavailable) as error:
                assert document['error_contains'] in str(error), 'failure was not the requested actual boundary: '+str(error)
            else:
                raise AssertionError('partial or mismatched physical phase returned success')
            print(json.dumps(dict(status='unknown', admission=admission_wire(admission))), flush=True)
            return
        if mode == 'lost_submission':
            work = Work.parse({**document['works'][0], 'executor_epoch': runtime.executor_epoch})
            admission = await client.claim(work, str(uuid4()))
            await client.recheck(admission)
            scope = executor.scope(work)
            delivery = await client.read_initial_launch(work.wire())
            descriptor = writer.write(admission, scope, delivery)
            submission = Submission(admission, descriptor, Projection.from_delivery(delivery))
            async with backend.connect() as session:
                session.writer.write(submission.encode()+b'\n')
                await session.writer.drain()
                response = decode(await read_frame(session.reader))
                assert response['status'] == 'ok'
                # Реальный disconnect до finite response ACK, owner остаётся Root.
            async with backend.connect() as session:
                session.writer.write(frame_encode(dict(schema_version=1, action='phase_status', scope=scope.wire(), attempt_id=admission.attempt_id))+b'\n')
                await session.writer.drain()
                observed = decode(await read_frame(session.reader))['phase']
                assert observed['status'] == 'accepted' and observed['checkpoint']['effect'] == 'workspace'
                # Отдельно потерян status ACK: это read-only, не новый эффект.
            try:
                await executor.exchange(submission)
            except Refused:
                pass
            else:
                raise AssertionError('lost submission ACK permitted execution replay')
            print(json.dumps(dict(status='retained', admission=admission_wire(admission), scope=scope.wire())), flush=True)
            return
        if mode in ('resume_lost_result', 'recover_result'):
            original = parse_admission(document['admission'])
            if mode == 'resume_lost_result':
                admission = await client.claim(original.work, original.request_id)
                assert admission == original
                def check():
                    if datetime.now(timezone.utc) >= admission.lease_until:
                        raise Refused('original fixture admission expired')
                async def authorize(): await client.recheck(admission)
                outcome, evidence = await executor.resume(admission.work, EffectGuard(authorize, check, admission))
                try:
                    await client.result(admission, outcome, evidence, receipt_id=executor.result_identity(admission))
                except Unavailable:
                    pass
                else:
                    raise AssertionError('controlled committed result ACK was not lost')
                phase = await executor.observe(executor.scope(admission.work), admission.attempt_id)
                assert phase['status'] == 'succeeded' and phase['acknowledged'] is False
            else:
                await executor.recover_result(original)
                phase = await executor.observe(executor.scope(original.work), original.attempt_id)
                assert phase['acknowledged'] is True
            print(json.dumps(dict(status='recovered', admission=admission_wire(original))), flush=True)
            return
        assert mode == 'run'
        for observation in document.get('observations', []):
            phase = await executor.observe(Scope.parse(observation['scope']), observation['attempt_id'])
            assert phase['status'] == 'succeeded' and phase['acknowledged'] is True
        for value in document['works']:
            work = Work.parse({**value, 'executor_epoch': runtime.executor_epoch})
            await runtime._execute(work)
            if runtime.last_error:
                raise AssertionError('planned Runtime '+work.action+' failed: '+runtime.last_error)
        print(json.dumps(dict(status='accepted', frontend_uid=os.geteuid(), phases=[w['action'] for w in document['works']])), flush=True)
    finally:
        await runtime.aclose()
        writer.close()


async def early_start_cleanup(config, authority, template, values, launch):
    # BUG #758 / HYPOTHESIS: early cancel ломает inherited cleanup и не ждёт
    # исходный physical start. Barrier внутри настоящего systemd start, до Facts.
    # SOLUTION: join original start + retirement, затем exact phase artifacts.
    # RESULT: actual cancel/timeout + repeated cancel GREEN, один start/retire.
    # STATUS: FIXED — see #758
    import copy
    from mop.node.host_backend import Identity, Process
    from mop.node.host_phase_supervisor import PhaseConfig, PhaseOwner
    from mop.node.host_scope import Scope, Refused, Unknown, Request
    from mop.node.host_scope_supervisor import SocketServer
    from mop.node.host_scope_systemd import SystemdCgroupAdapter
    # BUG #764: successful original start теряется при concurrent inspect.
    # HYPOTHESIS: transient active slot ошибочно используется как start owner.
    # SOLUTION: original action retained при создании в physical supervisor.
    # STATUS: FIXED — see #764
    for mode in ('cancel', 'timeout', 'inspect'):
        case = copy.deepcopy(template)
        target, incarnation, operation = (str(uuid4()) for _ in range(3))
        for value in case:
            value['work'].update(target_id=target, incarnation_id=incarnation, operation_id=operation,
                step_id=str(uuid4()), executor_epoch=authority.executor)
        values.extend(case)
        installed = PhaseConfig.load(ROOT/'host-phase.json').installed
        adapter = SystemdCgroupAdapter(installed)
        print('early-start '+mode+': install owner', file=sys.stderr, flush=True)
        owner = PhaseOwner(installed, adapter, identity=Identity(INSTALLATION, authority.node), phase_config=ROOT/'host-phase.json')
        endpoint = SocketServer(owner, SOCKET)
        entered, release = asyncio.Event(), asyncio.Event()
        command = adapter.command.run
        starts, kills = [], []
        async def barrier(*args):
            result = await command(*args)
            if args[0] == '/usr/bin/systemd-run' and '--scope' in args:
                starts.append(args)
                if mode != 'inspect':
                    entered.set()
                    await release.wait()
            return result
        adapter.command.run = barrier
        retire = adapter.retire
        async def retirement(reference):
            result = await retire(reference)
            kills.append(reference)
            return result
        adapter.retire = retirement
        await endpoint.start()
        observation = None
        print('early-start '+mode+': stage', file=sys.stderr, flush=True)
        try:
            staged = await launch([case[0]['work']], mode='stage',
                stop_before='git-fetch' if mode == 'inspect' else 'start-broker')
            scope = Scope.parse(staged['scope'])
            item = owner._attempt(scope, staged['admission']['attempt_id'])
            body = owner.bodies[scope]
            if mode == 'inspect':
                reference = body.reference
                assert reference is not None and scope not in owner.active
                inspect = adapter.inspect
                async def delayed(address):
                    entered.set()
                    await release.wait()
                    return await inspect(address)
                adapter.inspect = delayed
                observation = asyncio.create_task(owner.execute(NODE,
                    Request('inspect', scope, str(uuid4()), reference)))
            else:
                print('early-start '+mode+': approve physical start', file=sys.stderr, flush=True)
                if mode == 'timeout': owner.timeout = .5
                await launch([], mode='approve_start', admission=staged['admission'])
            async with asyncio.timeout(10.): await entered.wait()
            print('early-start '+mode+': original physical start/inspect barrier', file=sys.stderr, flush=True)
            process = Process.read(int(run('/usr/bin/systemctl', 'show', '--value', '--property=MainPID', '--', scope.unit)))
            assert Path('/proc', str(process.pid), 'status').read_text().split('Uid:')[1].splitlines()[0].split() == [str(BODY)]*4
            assert scope.cgroup in Path('/proc', str(process.pid), 'cgroup').read_text()
            if mode == 'inspect':
                assert owner.active[scope].request.action == 'inspect', 'barrier missed supported concurrent inspect'
                assert body.reference == reference == owner._owned[scope]
            else:
                assert body.reference is None and scope not in owner._owned, 'barrier missed original physical start'
            if mode != 'timeout': item.task.cancel()
            async with asyncio.timeout(10.):
                while item.status == 'accepted': await asyncio.sleep(.01)
            # Повторная отмена caller не вправе бросить retained cleanup task.
            # Новый startup/bind здесь намеренно не нужен и не является cleanup.
            item.task.cancel()
            await asyncio.sleep(0)
            item.task.cancel()
            await asyncio.sleep(.02)
            assert body.cleanup is not None and not body.cleanup.done(), (
                '#758 phase cleanup failed before original start outcome: '+str(item.error))
            assert body.workspace is not None and body.socket_identity is not None
            assert item.receipt is not None and not kills, 'artifacts removed before exact retirement'
            release.set()
            if observation is not None:
                observed = await observation
                assert observed.reference == reference and observed.populated
            async with asyncio.timeout(25.): await asyncio.shield(body.cleanup)
            await asyncio.gather(item.task, return_exceptions=True)
            for _ in range(3): await owner._cleanup_phase(body)
            assert body.reference == owner._owned[scope], 'cleanup reminted instead of retaining the original reference'
            assert len(starts) == 1 and kills == [body.reference], 'cleanup replayed start/retirement or changed incarnation'
            assert body.workspace is None and body.socket_identity is None and item.receipt is None
            assert not (installed.body_root/scope.launch_id).exists()
            assert not (Path(config['control_root'])/(scope.launch_id+'.sock')).exists()
            assert not list(Path(config['ingress']).glob('*'+item.submission.admission.attempt_id+'*'))
            assert not list(Path(config['snapshots']).glob('*.json'))
            try: current = Process.read(process.pid)
            except (Refused, Unknown, OSError): current = None
            assert current != process, 'original actual body process survived early-start cleanup'
            # Удаляем только собственные точные fixture units перед cold-bind
            # следующего daemon; production inventory/adoption не подменяется.
            run('/usr/bin/systemctl', 'stop', '--', scope.unit, scope.unit.replace('mop-host-', 'mop-clean-', 1))
            run('/usr/bin/systemctl', 'reset-failed', '--', scope.unit)
        except BaseException:
            import traceback
            traceback.print_exc()
            raise
        finally:
            release.set()
            if observation is not None: await asyncio.gather(observation, return_exceptions=True)
            try:
                await endpoint.aclose()
                # Manager GC асинхронен: cold-bind не усыновляет даже empty unit.
                async with asyncio.timeout(5.):
                    while (await adapter.startup_inventory()):
                        await asyncio.sleep(.05)
            finally: await adapter.aclose()


async def accept():
    assert os.geteuid() == 0 and Path('/proc/1/comm').read_text().strip() == 'systemd'
    assert sys.version_info[:2] == (3, 12)
    assert (ROOT/'mop/node/host_body.py').exists(), 'actual installed body-UID lifecycle broker is missing (#752)'
    from mop.common.bootstrap_envelope import bounded_content, encode
    from mop.common.soul import from_manifest
    from mop.node.host_phase_supervisor import PhaseOwner
    import psycopg
    # PostgreSQL принадлежит только этому private container; нет host/server inputs.
    run('/usr/bin/pg_ctlcluster', '17', 'main', 'start')
    run('/usr/sbin/runuser', '-u', 'postgres', '--', '/usr/bin/psql', '-c', "CREATE ROLE fixture LOGIN PASSWORD 'owned-fixture-only';")
    run('/usr/sbin/runuser', '-u', 'postgres', '--', '/usr/bin/createdb', '-O', 'fixture', 'fixture')
    db = psycopg.connect('host=127.0.0.1 dbname=fixture user=fixture password=owned-fixture-only')
    with db.cursor() as cursor:
        cursor.execute('SHOW server_version_num')
        assert 170000 <= int(cursor.fetchone()[0]) < 180000
    db.commit()
    run('/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
        '-keyout', '/opt/runtime/server.key', '-out', '/opt/runtime/ca.pem')
    Path('/opt/runtime/server.key').chmod(0o600)
    Path('/opt/runtime/ca.pem').chmod(0o644)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 8443), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain('/opt/runtime/ca.pem', '/opt/runtime/server.key')
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    source = Path('/opt/source-fixture')
    source.mkdir()
    run('/usr/bin/git', '-C', str(source), 'init')
    run('/usr/bin/git', '-C', str(source), 'config', 'user.name', 'Fixture Source')
    run('/usr/bin/git', '-C', str(source), 'config', 'user.email', 'fixture@example.invalid')
    (source/'pinned.txt').write_text('selected immutable commit\n')
    (source/'.mop').mkdir()
    (source/'.mop/bootstrap.yaml').write_text('- ansible.builtin.fail: {msg: checkout bootstrap must never run}\n')
    run('/usr/bin/git', '-C', str(source), 'add', '.')
    run('/usr/bin/git', '-C', str(source), 'commit', '-m', 'selected fixture commit')
    commit = run('/usr/bin/git', '-C', str(source), 'rev-parse', 'HEAD').decode().strip()
    (source/'pinned.txt').write_text('moved branch must not be checked out\n')
    run('/usr/bin/git', '-C', str(source), 'commit', '-am', 'move fixture ref')
    Path('/srv/git').mkdir()
    run('/usr/bin/git', 'clone', '--bare', str(source), '/srv/git/repo.git')
    run('/usr/bin/git', '--git-dir=/srv/git/repo.git', 'config', 'uploadpack.allowAnySHA1InWant', 'true')
    # SSH upload-pack принадлежит реальному fixture-git, не root safe.directory bypass.
    for parent, dirs, files in os.walk('/srv/git/repo.git'):
        os.chown(parent, 21073, 21073)
        for name in files: os.chown(Path(parent)/name, 21073, 21073)
    sshdir = Path('/opt/ssh-fixture')
    sshdir.mkdir(mode=0o755)
    run('/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(sshdir/'host'))
    run('/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(sshdir/'source'))
    (sshdir/'authorized_keys').write_bytes((sshdir/'source.pub').read_bytes())
    (sshdir/'authorized_keys').chmod(0o644)
    (sshdir/'sshd_config').write_text('Port 2222\nListenAddress 127.0.0.1\nHostKey /opt/ssh-fixture/host\n'
        'PidFile /run/fixture-sshd.pid\nAuthorizedKeysFile /opt/ssh-fixture/authorized_keys\n'
        'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\n'
        'AuthenticationMethods publickey\nPermitRootLogin no\nAllowUsers fixture-git\nUsePAM no\n'
        'StrictModes yes\nPermitUserEnvironment no\nAllowAgentForwarding no\nAllowTcpForwarding no\n'
        'ForceCommand /usr/bin/git-upload-pack /srv/git/repo.git\nLogLevel QUIET\n')
    sshd = await asyncio.create_subprocess_exec('/usr/sbin/sshd', '-D', '-f', str(sshdir/'sshd_config'),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    image, policy, epoch, node, previous, grant = (str(uuid4()) for _ in range(6))
    prompt = b'Approved fixture role, never a native session.\n'
    role = from_manifest(dict(soul='builder', version=1, runtime=dict(driver='any', receives_dispatch=False),
        capabilities=dict(node_verbs=[], cluster_verbs=[], dispatch=False, spawn=[]), prompt='prompt.md'), prompt)
    tasks = b'- name: actual approved task\n  ansible.builtin.copy:\n    dest: "../bootstrap-proof.txt"\n    content: "approved retained envelope executed\\n"\n'
    envelope = encode(bounded_content(tasks, b'{}\n', 'https://localhost/approved-bootstrap.git', commit, ()))
    base = dict(schema_version=1, bootstrap_revision=str(uuid4()), bootstrap_digest=hashlib.sha256(envelope).hexdigest(),
        image_revision=image, image_digest=hashlib.sha256(('image-revision:'+image).encode()).hexdigest(),
        display_name='pu-fixture-1', image_policy=dict(policy_id=policy, cpu=500, memory_mb=256, disk_mb=64),
        selection=dict(policy_id=str(uuid4()), digest='c'*64, source=dict(origin='https://localhost:8443/repo.git', commit=commit, ref='refs/heads/master'),
            desired_model='fixture-only', model_config_digest=hashlib.sha256(b'{}').hexdigest(),
            consumers=[dict(purpose='source_credential', version_id=str(uuid4())), dict(purpose='model_config', version_id=str(uuid4()))]),
        git_name='--approved-fixture-owner', git_email='fixture-owner@example.invalid')
    import copy
    source_ref = base['selection']['consumers'][0]['version_id']
    credentials = [dict(format='git-https-basic-v1', origin=base['selection']['source']['origin'], username='fixture', password=SOURCE_PASSWORD),
        dict(format='git-ssh-key-v1', origin='ssh://fixture-git@localhost:2222/srv/git/repo.git',
            private_key=(sshdir/'source').read_text(),
            known_hosts='[localhost]:2222 '+' '.join((sshdir/'host.pub').read_text().split()[:2])+'\n')]
    values, cases = [], []
    for credential in credentials:
        target, inc, operation = (str(uuid4()) for _ in range(3))
        selected = copy.deepcopy(base)
        selected['selection']['source']['origin'] = credential['origin']
        case = []
        for action in ('prepare_body', 'bootstrap_body', 'initial_identity'):
            work = dict(schema_version=1, epoch=epoch, node_id=node, executor_epoch=previous,
                target_id=target, incarnation_id=inc, operation_id=operation, step_id=str(uuid4()), grant_id=grant,
                owner_id='1', generation='1', revision='1', role='builder', bundle_digest=role.digest,
                action=action, not_after=(datetime.now(timezone.utc)+timedelta(seconds=340)).isoformat())
            value = {**selected, 'work': work, 'phase': action, 'bootstrap': dict(envelope=base64.b64encode(envelope).decode()) if action == 'bootstrap_body' else None,
                'role': dict(manifest=role.manifest.to_dict(), prompt=base64.b64encode(prompt).decode()) if action == 'initial_identity' else None,
                'consumers': [dict(purpose='source_credential', version_id=source_ref, name='not-a-path', kind='credential',
                    value=base64.b64encode(json.dumps(credential).encode()).decode())] if action == 'prepare_body' else []}
            values.append(value)
            case.append(value)
        cases.append(case)
    server.authority = Authority(db, values, epoch, node, previous)
    installed = dict(node_uid=NODE, body_uid=BODY, body_gid=BODY, runner=str(ROOT/'mop/node/host_body.py'),
                     body_root='/var/lib/mop-bodies', node_private='/var/lib/mop-node')
    for p, mode, uid, gid in (('/var/lib/mop-bodies', 0o755, 0, 0), ('/var/lib/mop-node', 0o700, NODE, NODE),
        ('/var/lib/mop-node/inputs', 0o700, NODE, NODE), ('/var/lib/mop-snapshots', 0o750, 0, BODY),
        ('/run/mop-host', 0o755, 0, 0), ('/run/mop-phase', 0o755, 0, 0)):
        Path(p).mkdir(parents=True, exist_ok=True)
        os.chown(p, uid, gid)
        Path(p).chmod(mode)
    # Отдельный manager только owned fixture; не меняем environment хоста.
    manager_keys = [line.partition('=')[0] for line in run('/usr/bin/systemctl', 'show-environment').decode().splitlines()]
    if manager_keys: run('/usr/bin/systemctl', 'unset-environment', *manager_keys)
    run('/usr/bin/systemctl', 'set-environment', 'PATH=/usr/bin:/bin', 'LANG=C.UTF-8')
    resource_probe = subprocess.run(('/usr/bin/systemd-run', '--quiet', '--wait', '--unit=fixture-resource-properties',
        '--property=User='+str(BODY), '--property=Group='+str(BODY), '--property=CPUQuota=25%',
        '--property=CPUQuotaPeriodSec=100ms', '--property=MemoryMax=268435456', '--property=TasksMax=64',
        '--property=CPUWeight=75', '/usr/bin/true'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert resource_probe.returncode == 0, 'owned fixed resource property probe: '+resource_probe.stderr.decode()
    manifest = content_manifest(image, policy)
    (ROOT/'host-runtime.json').write_text(json.dumps(manifest))
    config = dict(schema_version=1, installed=installed, runtime_manifest=str(ROOT/'host-runtime.json'),
        ingress='/var/lib/mop-node/inputs', snapshots='/var/lib/mop-snapshots', control_root='/run/mop-phase')
    (ROOT/'host-phase.json').write_text(json.dumps(config))
    physical = dict(schema_version=1, installation_id=INSTALLATION, node_id=node, **installed,
                    socket=str(SOCKET), phase_config=str(ROOT/'host-phase.json'))
    Path('/etc/mop-physical.json').write_text(json.dumps(physical))
    Path('/etc/mop-physical.json').chmod(0o600)
    preflight = subprocess.run(('/usr/bin/python3', '-ISB', '-c',
        'import sys;sys.path.insert(0,"/opt/fixture");from mop.node.host_body import installed_entry,load_installation;load_installation(installed_entry())'),
        user=BODY, group=BODY, extra_groups=(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(PATH='/usr/bin:/bin'))
    assert preflight.returncode == 0 and not preflight.stdout, 'actual installed body preflight: '+preflight.stderr.decode()
    from mop.node.host_scope import Scope
    async def launch(works, observations=(), **options):
        document = dict(origin='https://localhost:8443', token=TOKEN, node=node,
            works=works, observations=list(observations), **options)
        proc = await asyncio.create_subprocess_exec('/usr/local/bin/python3.12', '-IB', __file__, 'frontend',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            user=NODE, group=NODE, extra_groups=(), env=dict(PATH='/usr/bin:/bin',
                HTTPS_PROXY='http://127.0.0.1:1', GIT_CONFIG_PARAMETERS='poison', SSH_AUTH_SOCK='/tmp/hostile-agent',
                GIT_AUTHOR_NAME='wrong-ambient-owner', NODE_BEARER=TOKEN))
        out, err = await proc.communicate(json.dumps(document).encode())
        if proc.returncode != 0:
            statuses = []
            for work in works:
                scope = Scope(epoch, node, server.authority.executor, work['target_id'], work['incarnation_id'], work['operation_id'])
                p = subprocess.run(('/usr/bin/systemctl', 'show', scope.unit, '--property=ActiveState,SubState,ExecMainCode,ExecMainStatus,Result,ControlGroup'),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                origin = next(v['selection']['source']['origin'] for v in values if v['work']['step_id'] == work['step_id'])
                statuses.append(origin+' '+p.stdout.decode())
            raise AssertionError('actual planned frontend failed: '+err.decode()+' owned units: '+repr(statuses))
        return json.loads(out)
    async with asyncio.timeout(90.):
        await early_start_cleanup(config, server.authority, cases[0], values, launch)
    root = await asyncio.create_subprocess_exec(str(ROOT/'bin/mop'), 'host-root', '--physical', '/etc/mop-physical.json',
        env=dict(PATH='/usr/bin:/bin', NODE_BEARER='root-ambient-not-for-body'),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(15.):
            while not SOCKET.exists():
                if root.returncode is not None:
                    out, err = await root.communicate()
                    raise AssertionError('installed root failed: '+err.decode())
                await asyncio.sleep(.02)
        for index, case in enumerate(cases):
            if index == 0:
                result = await launch([case[0]['work']], mode='lost_submission')
                assert not (Path('/var/lib/mop-bodies')/case[0]['work']['operation_id']).exists(), 'read-only status dispatched an effect'
                original_admission = result['admission']
                server.authority.drop_result_once = True
                await launch([case[0]['work']], mode='resume_lost_result', admission=original_admission)
                await launch([], mode='recover_result', admission=original_admission)
            else:
                result = await launch([case[0]['work']])
                assert result['phases'] == ['prepare_body']
            work = case[0]['work']
            with db.cursor() as cursor:
                cursor.execute('SELECT attempt FROM admissions WHERE step=%s', (work['step_id'],))
                original_attempt = cursor.fetchone()[0]
            db.commit()
            scope = Scope(epoch, node, server.authority.executor, work['target_id'], work['incarnation_id'], work['operation_id'])
            result = await launch([v['work'] for v in case[1:]], [dict(scope=scope.wire(), attempt_id=original_attempt)])
            assert result['phases'] == ['bootstrap_body', 'initial_identity']
            body = Path('/var/lib/mop-bodies')/work['operation_id']
            assert (body/'repo/pinned.txt').read_text() == 'selected immutable commit\n'
            assert (body/'bootstrap-proof.txt').read_text() == 'approved retained envelope executed\n'
            assert run('/usr/bin/git', '-C', str(body/'repo'), '-c', 'safe.directory=*', 'rev-parse', 'HEAD').decode().strip() == commit
            assert run('/usr/bin/git', '-C', str(body/'repo'), '-c', 'safe.directory=*', 'config', '--local', 'user.name').decode().strip() == base['git_name']
            assert (body/'role/prompt.md').read_bytes() == prompt
            broker_pid = int(run('/usr/bin/systemctl', 'show', '--value', '--property=MainPID', '--', scope.unit))
            for name in ('environ', 'cmdline'):
                process_bytes = Path('/proc', str(broker_pid), name).read_bytes()
                assert TOKEN.encode() not in process_bytes and SOURCE_PASSWORD.encode() not in process_bytes
                assert b'NODE_BEARER=' not in process_bytes and b'root-ambient-not-for-body' not in process_bytes
                assert b'SSH_AUTH_SOCK=' not in process_bytes and b'HTTPS_PROXY=' not in process_bytes
            for parent, dirs, files in os.walk(body):
                for filename in files:
                    p = Path(parent)/filename
                    if p.is_file() and p.stat().st_size < 1048576:
                        data = p.read_bytes()
                        assert TOKEN.encode() not in data and SOURCE_PASSWORD.encode() not in data
                        assert credentials[1]['private_key'].encode() not in data
        assert [r['evidence'] for r in server.authority.results] == ['body-prepared', 'body-bootstrapped', 'initial-identity-applied']*2
        assert all(r['outcome'] == 'succeeded' for r in server.authority.results)
        assert len(server.authority.checks) >= 32, 'no actual finite recheck before compiled effects'
        negatives = []
        def extra_case():
            case = copy.deepcopy(cases[0])
            target, inc, operation = (str(uuid4()) for _ in range(3))
            for value in case:
                value['work'].update(target_id=target, incarnation_id=inc, operation_id=operation,
                    step_id=str(uuid4()), executor_epoch=server.authority.executor,
                    not_after=(datetime.now(timezone.utc)+timedelta(seconds=340)).isoformat())
            cases.append(case)
            values.extend(case)
            return case
        def observation(value):
            work = value['work']
            with server.authority.lock:
                with db.cursor() as cursor:
                    cursor.execute('SELECT attempt FROM admissions WHERE step=%s', (work['step_id'],))
                    attempt = cursor.fetchone()[0]
                db.commit()
            scope = Scope(epoch, node, server.authority.executor, work['target_id'], work['incarnation_id'], work['operation_id'])
            return dict(scope=scope.wire(), attempt_id=attempt)
        async def removed(case):
            body = Path('/var/lib/mop-bodies')/case[0]['work']['operation_id']
            async with asyncio.timeout(20.):
                while body.exists(): await asyncio.sleep(.02)
        def retained_process(value):
            from mop.node.host_backend import Process
            scope = Scope.parse(observation(value)['scope'])
            pid = int(run('/usr/bin/systemctl', 'show', '--value', '--property=MainPID', '--', scope.unit))
            return Process.read(pid)
        def process_gone(process):
            from mop.node.host_backend import Process
            from mop.node.host_scope import Refused, Unknown
            try: current = Process.read(process.pid)
            except (Refused, Unknown, OSError): return True
            return current != process
        def replace_bootstrap(case, task_list):
            raw = encode(bounded_content(json.dumps(task_list).encode(), b'{}\n', 'https://localhost/approved-bootstrap.git', commit, ()))
            revision = str(uuid4())
            for value in case:
                value.update(bootstrap_revision=revision, bootstrap_digest=hashlib.sha256(raw).hexdigest())
                if value['phase'] == 'bootstrap_body': value['bootstrap'] = dict(envelope=base64.b64encode(raw).decode())

        await launch([], mode='deny_token')
        negatives.append('wrong-node-token')
        # BUG #765: настоящий node UID пишет private input, fsync каталога падает
        # до выдачи token; backend ещё не вызван, stage сам компенсирует exact inode.
        # STATUS: FIXED — see #765
        case = extra_case()
        result = await launch([case[0]['work']], mode='partial_staging')
        assert result['private_bytes'] == result['writer_slots'] == result['backend_calls'] == 0
        case = extra_case()
        await launch([case[0]['work']])
        observed = observation(case[0])
        result = await launch([case[1]['work']], [observed], mode='partial_staging')
        assert result['private_bytes'] == result['writer_slots'] == result['backend_calls'] == 0
        await launch([], [observed], mode='cleanup_observed')
        await removed(case)
        negatives.extend(('prepare-partial-staging-compensated', 'bootstrap-partial-staging-compensated'))
        # BUG #759 / HYPOTHESIS: valid delivery уходил в ingress до eligibility.
        # SOLUTION: проверка action/reference до delivery; exact staging owner
        # убирает только definite pre-submit failure, unknown dispatch не replay.
        # RESULT: actual Python 3.12/PG17 fixture — 147 recheck, все пути ниже прошли.
        # STATUS: FIXED — see #759.
        identity_case, native_case, bootstrap_case = extra_case(), extra_case(), extra_case()
        native = native_case[2]
        native['phase'] = native['work']['action'] = 'native_exec'
        model = next(p for p in native['selection']['consumers'] if p['purpose'] == 'model_config')
        native['consumers'] = [dict(**model, name='model', kind='file', value=base64.b64encode(b'{}').decode())]
        await launch([bootstrap_case[1]['work'], identity_case[2]['work'], native['work']], mode='ingress_eligibility')
        negatives.append('ineligible-before-private-delivery')
        for failure in ('definite', 'unknown'):
            case = extra_case()
            await launch([case[0]['work']], mode='ingress_failure', failure=failure)
            negatives.append('poststage-'+failure+'-ownership')
        for negative in ('snapshot', 'phase', 'source-format', 'source-origin'):
            case = extra_case()
            value = case[1] if negative == 'phase' else case[0]
            if negative.startswith('source-'):
                credential = copy.deepcopy(credentials[0])
                if negative == 'source-format': credential['format'] = 'unknown-not-executable'
                else: credential['origin'] = 'https://localhost:8443/foreign.git'
                value['consumers'][0]['value'] = base64.b64encode(json.dumps(credential).encode()).decode()
            await launch([value['work']], mode='deny_input', negative=negative)
            assert not (Path('/var/lib/mop-bodies')/value['work']['operation_id']).exists(), 'invalid input caused a workspace effect'
            negatives.append('wrong-'+negative)
        case = extra_case()
        # Role codec проверяется на eligible identity с настоящим prior ACK, а не
        # теряется за новым более ранним missing-reference отказом (#759).
        await launch([case[0]['work']])
        await launch([case[1]['work']], [observation(case[0])])
        case[2]['role']['prompt'] = base64.b64encode(prompt+b'unapproved changed role prompt\n').decode()
        await launch([case[2]['work']], [observation(case[1])], mode='expect_failure', error_contains='approved initial launch role pin changed')
        assert not (Path('/var/lib/mop-bodies')/case[2]['work']['operation_id']/'role').exists(), 'wrong approved role caused an identity effect'
        await launch([], [observation(case[1])], mode='cleanup_observed')
        await removed(case)
        negatives.append('wrong-approved-role-prompt')

        case = extra_case()
        server.authority.lease_limits[case[0]['work']['step_id']] = 2
        await launch([case[0]['work']], mode='expired_guard')
        assert not (Path('/var/lib/mop-bodies')/case[0]['work']['operation_id']).exists()
        negatives.append('expired-original-admission')

        case = extra_case()
        staged = await launch([case[0]['work']], mode='stage', stop_before='git-fetch')
        body = Path('/var/lib/mop-bodies')/case[0]['work']['operation_id']
        process = retained_process(case[0])
        hello = json.dumps(dict(schema_version=1, action='body_hello', scope=staged['scope']))+'\n'
        # Тот же body UID вне exact cgroup/MainPID не получает control authority.
        counterfeit = subprocess.run(('/usr/bin/python3', '-ISB', '-c',
            'import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(sys.argv[1]); s.sendall(sys.argv[2].encode()); assert s.recv(1)==b""',
            '/run/mop-phase/'+case[0]['work']['operation_id']+'.sock', hello), user=BODY, group=BODY,
            extra_groups=(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(PATH='/usr/bin:/bin'))
        assert counterfeit.returncode == 0, 'same-UID foreign cgroup was not rejected'
        negatives.append('same-uid-foreign-mainpid-cgroup')
        server.authority.revoke = True
        try:
            await launch([], mode='checkpoint_denied', admission=staged['admission'])
            assert not (body/'repo/pinned.txt').exists(), 'revoked git-fetch checkpoint ran'
            await launch([], mode='cleanup', admission=staged['admission'])
            await removed(case)
        finally:
            server.authority.revoke = False
        assert process_gone(process), 'repeated cancellation lost exact cgroup cleanup'
        negatives.extend(('revoke-at-checkpoint', 'repeated-cancel-retained-cleanup'))

        case = extra_case()
        await launch([case[0]['work']])
        observed = observation(case[0])
        process = retained_process(case[0])
        scope = Scope.parse(observed['scope'])
        run('/usr/bin/systemctl', 'set-property', '--runtime', scope.unit, 'MemoryMax=134217728')
        await launch([case[1]['work']], [observed], mode='expect_failure', error_contains='resource limits differ')
        await removed(case)
        assert process_gone(process), 'resource mismatch cleanup left its body alive'
        negatives.append('actual-resource-readback-mismatch')

        case = extra_case()
        program = ('import subprocess,time,sys; '
            'p=subprocess.Popen(["/usr/bin/python3","-c","import time; time.sleep(120)"],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
            'open("../escaped-helper.pid","w").write(str(p.pid)); time.sleep(1.5); sys.exit(23)')
        replace_bootstrap(case, [{'name': 'owned escaped helper then actual failure',
            'ansible.builtin.command': {'argv': ['/usr/bin/python3', '-I', '-c', program]}}])
        await launch([case[0]['work']])
        observed = observation(case[0])
        failed = asyncio.create_task(launch([case[1]['work']], [observed], mode='expect_failure', error_contains='ansible installed tool failed'))
        pidfile = Path('/var/lib/mop-bodies')/case[0]['work']['operation_id']/'escaped-helper.pid'
        async with asyncio.timeout(60.):
            while not pidfile.exists():
                if failed.done():
                    await failed
                    raise AssertionError('partial failure never ran its controlled escaped helper')
                await asyncio.sleep(.02)
        from mop.node.host_backend import Process
        escaped = Process.read(int(pidfile.read_text()))
        assert int(Path('/proc', str(escaped.pid), 'stat').read_text().rsplit(')', 1)[1].split()[3]) == escaped.pid, 'helper did not actually setsid'
        assert Scope.parse(observed['scope']).cgroup in Path('/proc', str(escaped.pid), 'cgroup').read_text()
        await failed
        await removed(case)
        assert process_gone(escaped), 'setsid helper escaped exact cgroup retirement'
        negatives.extend(('actual-ansible-partial-failure', 'setsid-helper-retired'))

        case = extra_case()
        override = '[user]\nname = wrong-local-include\nemail = wrong@example.invalid\n'
        config_text = '[core]\nrepositoryformatversion = 0\nbare = false\n[user]\nname = old\nemail = old@example.invalid\nuseConfigOnly = true\n[include]\npath = identity-override\n'
        replace_bootstrap(case, [
            {'ansible.builtin.copy': {'dest': '../repo/.git/identity-override', 'content': override}},
            {'ansible.builtin.copy': {'dest': '../repo/.git/config', 'content': config_text}}])
        await launch([case[0]['work']])
        await launch([case[1]['work']], [observation(case[0])])
        await launch([case[2]['work']], [observation(case[1])], mode='expect_failure', error_contains='actual Git author/committer differs')
        await removed(case)
        negatives.append('actual-git-identity-override')

        case = extra_case()
        isolated = from_manifest(dict(soul='builder', version=1, runtime=dict(driver='isolating', receives_dispatch=False),
            capabilities=dict(node_verbs=[], cluster_verbs=[], dispatch=False, spawn=[]), prompt='prompt.md'), prompt)
        for value in case:
            value['work']['bundle_digest'] = isolated.digest
            if value['phase'] == 'initial_identity': value['role'] = dict(manifest=isolated.manifest.to_dict(), prompt=base64.b64encode(prompt).decode())
        await launch([case[0]['work']])
        await launch([case[1]['work']], [observation(case[0])])
        await launch([case[2]['work']], [observation(case[1])], mode='expect_failure', error_contains='isolating role is unavailable')
        await removed(case)
        negatives.append('approved-isolating-role-unavailable')
        assert not list(Path('/var/lib/mop-snapshots').glob('*.json'))
        print(json.dumps(dict(status='accepted', python='3.12', postgres=17, root_uid=0, node_uid=NODE, body_uid=BODY,
            actual_negatives=negatives, early_start_cleanup=['cancel', 'timeout'],
            concurrent_inspect_cleanup=True,
            actual_phases=['prepare_body', 'bootstrap_body', 'initial_identity'],
            source_transports=['https', 'ssh'], frontend_replacement=True,
            lost_submission_ack=True, lost_status_ack=True, lost_result_ack=True, original_attempt_resume=True,
            admission_checks=len(server.authority.checks), native_execution=False,
            ready_confirmation=False, disk_mode='shared-reservation', production_rbac=False)), flush=True)
    finally:
        if root.returncode is None: root.terminate()
        async with asyncio.timeout(30.):
            out, err = await root.communicate()
        server.shutdown()
        sshd.terminate()
        await sshd.wait()
        db.close()
        assert root.returncode == 0 and not out and not err, ('root cleanup failed: returncode='+str(root.returncode)
            +' stdout_bytes='+str(len(out))+' stderr_bytes='+str(len(err))+' reason='+repr(err.decode()))
        for case in cases:
            assert not (Path('/var/lib/mop-bodies')/case[0]['work']['operation_id']).exists(), 'body UID cleanup did not remove the derived workspace'


if __name__ == '__main__':
    asyncio.run(frontend(json.load(sys.stdin)) if len(sys.argv) > 1 and sys.argv[1] == 'frontend' else accept())
