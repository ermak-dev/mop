"""Вертикальная actual native fixture; PG authority double, не production acceptance.

HYPOTHESIS: phase DTO не запускает/не удерживает actual native/root/body lifetime.
SOLUTION: прежние actual planned phases плюс fixed native runner и NodeUID proxy.
RESULT: измеряется probe, readiness/RBAC здесь никогда не подменяются.
STATUS: IN PROGRESS — see #760
"""
import asyncio
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import ssl
import sys
from uuid import uuid4

spec = importlib.util.spec_from_file_location('planned_fixture', '/opt/hostphase-acceptance.py')
planned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planned)

PROFILE = dict(identifier='owned-fixture', origin='https://localhost:8443',
    ca='/opt/runtime/ca.pem', auth='api-key-v1')
BOOTSTRAP, DESIRED = 'claude-sonnet-4-6', 'claude-opus-4-6'


class ProviderHandler(planned.Handler):
    def handle_request(self):
        if self.path.startswith('/v1/'):
            # Никаких provider turns: startup metadata допускается, messages — отказ.
            if self.command == 'GET' and self.path.startswith('/v1/models'):
                value = dict(data=[dict(id=m, type='model', display_name=m,
                    created_at='2026-01-01T00:00:00Z') for m in (BOOTSTRAP, DESIRED)], has_more=False)
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.server.authority.provider_turns.append(self.path)
            self.send_error(403, 'owned fixture forbids provider turns')
            return
        return super().handle_request()


class Fixture:
    def prepare(self, base, cases, values):
        from mop.node.host_inputs import canonical_json
        self.cases = cases
        for value in values:
            value['image_policy'].update(cpu=4000, memory_mb=1024)
        base['image_policy'].update(cpu=4000, memory_mb=1024)
        value = copy.deepcopy(cases[0][-1])
        value['phase'] = value['work']['action'] = 'native_exec'
        value['work']['step_id'] = str(uuid4())
        value['bootstrap'] = None
        config_version, credential_version = str(uuid4()), str(uuid4())
        raw = canonical_json(dict(schema_version=1, profile_version=1,
            provider_profile=PROFILE['identifier'], credential_version=credential_version,
            proof_strategy='bootstrap-to-desired', bootstrap_model=BOOTSTRAP, desired_model=DESIRED))
        value['selection']['desired_model'] = DESIRED
        value['selection']['model_config_digest'] = hashlib.sha256(raw).hexdigest()
        refs = [dict(purpose='model_config', version_id=config_version),
            dict(purpose='model_credential', version_id=credential_version)]
        value['selection']['consumers'] = refs
        credential = canonical_json(dict(schema_version=1, provider_profile=PROFILE['identifier'],
            token='owned-disposable-provider-key-not-a-live-credential'))
        value['consumers'] = [dict(**ref, name='not-a-path', kind='credential',
            value=base64.b64encode(data).decode()) for ref, data in zip(refs, (raw, credential))]
        values.append(value)
        self.delivery = value
        (planned.ROOT/'host-native.json').write_text(json.dumps(dict(schema_version=1, provider=PROFILE)))

    def manifest(self, value):
        value['schema_version'] = 2
        value['capacity']['tasks_max'] = 128
        value['tools'].update(native='/opt/runtime/native', tmux='/usr/bin/tmux',
            hook=str(planned.ROOT/'mop/session.py'), mcp=str(planned.ROOT/'bin/mop'))
        artifacts = {v['path']: v for v in value['artifacts']}
        # Complete immutable CLI discovery и standby import closure.
        for filename in (planned.ROOT/'mop').rglob('*.py'):
            artifacts[str(filename)] = dict(path=str(filename),
                sha256=hashlib.sha256(filename.read_bytes()).hexdigest(),
                executable=bool(filename.stat().st_mode & 0o111))
        for name in ('native', 'tmux'):
            filename = Path(value['tools'][name])
            artifacts[str(filename)] = dict(path=str(filename),
                sha256=hashlib.sha256(filename.read_bytes()).hexdigest(), executable=True)
            if name == 'native':
                artifacts[str(filename)].update(kind='native', size=filename.stat().st_size)
        value['artifacts'] = list(artifacts.values())

    def authority(self, authority):
        from mop.common.producer import ProducerReport, ProducerAck
        from mop.common.telemetry import Observation, Ack
        authority.provider_turns = []
        authority.producer = None
        authority.observations = []
        with authority.db.cursor() as cursor:
            cursor.execute('CREATE TABLE native_producer (id integer PRIMARY KEY, report jsonb NOT NULL)')
            cursor.execute('CREATE TABLE native_telemetry (sequence bigint PRIMARY KEY, record jsonb NOT NULL)')
        authority.db.commit()
        original = authority.call
        def call(route, payload):
            if route not in ('session-producers', 'telemetry'):
                return original(route, payload)
            with authority.lock:
                if route == 'session-producers':
                    report = ProducerReport.parse({k: payload[k] for k in ('scope', 'launch', 'physical')})
                    assert payload['expected_prior'] is None and payload['expected_revision'] == '0'
                    assert authority.producer is None or authority.producer == report
                    with authority.db.cursor() as cursor:
                        cursor.execute('INSERT INTO native_producer VALUES (1,%s) ON CONFLICT DO NOTHING',
                            (json.dumps(report.wire()),))
                        cursor.execute('SELECT report FROM native_producer WHERE id=1')
                        assert cursor.fetchone()[0] == report.wire()
                    authority.db.commit()
                    authority.producer = report
                    return ProducerAck(report.scope, 1).wire()
                row = Observation.parse(payload)
                assert authority.producer is not None and row.scope == authority.producer.scope
                with authority.db.cursor() as cursor:
                    cursor.execute('SELECT record FROM native_telemetry WHERE sequence=%s', (row.sequence,))
                    previous = cursor.fetchone()
                    if previous is None:
                        assert row.sequence == len(authority.observations)+1
                        cursor.execute('INSERT INTO native_telemetry VALUES (%s,%s)',
                            (row.sequence, json.dumps(row.wire())))
                        authority.observations.append(row)
                    else:
                        assert previous[0] == row.wire()
                authority.db.commit()
                return Ack(row.scope.producer_epoch, row.sequence).wire()
        authority.call = call

    async def run(self, authority, cases, values):
        from mop.node.host_scope import Scope
        work = self.delivery['work']
        scope = Scope(authority.epoch, authority.node, authority.executor,
            work['target_id'], work['incarnation_id'], work['operation_id'])
        with authority.db.cursor() as cursor:
            cursor.execute('SELECT attempt FROM admissions WHERE step=%s', (cases[0][-1]['work']['step_id'],))
            prior = cursor.fetchone()[0]
        authority.db.commit()
        async def launch(document):
            proc = await asyncio.create_subprocess_exec('/usr/local/bin/python3.12', '-IB', __file__, 'frontend',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                user=planned.NODE, group=planned.NODE, extra_groups=(), env=dict(PATH='/usr/bin:/bin'))
            screen = b''
            async def capture_owned_startup():
                nonlocal screen
                socket = Path('/var/lib/mop-bodies')/scope.launch_id/'r/tmux'
                while proc.returncode is None:
                    if socket.exists():
                        observer = await asyncio.create_subprocess_exec('/usr/bin/tmux', '-S', str(socket),
                            'capture-pane', '-p', '-t', 'native:0.0', user=planned.BODY, group=planned.BODY,
                            extra_groups=(), env=dict(PATH='/usr/bin:/bin'), stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL)
                        try:
                            async with asyncio.timeout(1):
                                raw = await observer.stdout.read(65537)
                                await observer.wait()
                            if observer.returncode == 0 and len(raw) <= 65536:
                                screen = raw
                        finally:
                            if observer.returncode is None:
                                observer.kill()
                                await observer.wait()
                    await asyncio.sleep(.01)
            observer = asyncio.create_task(capture_owned_startup())
            try:
                out, err = await proc.communicate(json.dumps(document).encode())
            finally:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
            # Только disposable fixture pane; никакие live credentials/config
            # сюда не монтируются. Product protocol не отражает raw tool output.
            assert proc.returncode == 0, 'actual native frontend failed: '+err.decode()+'\nowned startup pane: '+screen[-4096:].decode(errors='replace')
            return json.loads(out)
        document = dict(origin=PROFILE['origin'], token=planned.TOKEN, node=authority.node,
            scope=scope.wire(), work=work, prior=prior)
        first = await launch(document)
        # Первый frontend уже process-exited. Root/body/native остаются теми же.
        second = await launch(dict(document, mode='observe', admission=first['admission']))
        assert first['native']['attachment'] == second['native']['attachment']
        assert first['native']['producer'] == second['native']['producer']
        assert not authority.provider_turns, 'unexpected provider request: '+repr(authority.provider_turns)
        assert any(row.event == 'PostModelSwitch' and row.model == DESIRED for row in authority.observations)
        assert authority.results[-1]['evidence'] == 'native-started'
        print(json.dumps(dict(status='accepted', python='3.12', postgres=17,
            root_uid=0, node_uid=planned.NODE, body_uid=planned.BODY,
            native_version='2.1.302', native_execution=True, post_ack_model_commit=True,
            original_lifetime_after_frontend_exit=True, native=second['native'],
            ready_confirmation=False, production_rbac=False)), flush=True)


async def frontend(document):
    assert os.geteuid() == planned.NODE
    from mop.node.host_backend import Identity
    from mop.node.host_backend_client import BackendClient
    from mop.node.host_input_files import NodeWriter
    from mop.node.host_inputs import admission_wire
    from mop.node.host_native import ProviderProfile
    from mop.node.host_native_client import InitialHostExecutor
    from mop.node.host_phases import parse_admission
    from mop.node.host_scope import Scope
    from mop.node.http_execution import EffectGuard, HttpsTransport, MachineClient, Work
    from mop.node.native_startup import start
    client = MachineClient(HttpsTransport(document['origin'], document['token'],
        timeout=5., tls=ssl.create_default_context(cafile=PROFILE['ca'])))
    backend = BackendClient(planned.SOCKET, node_uid=planned.NODE,
        identity=Identity(planned.INSTALLATION, document['node']))
    writer = NodeWriter(Path('/var/lib/mop-node/inputs'), node_uid=planned.NODE)
    profile = ProviderProfile(PROFILE['identifier'], PROFILE['origin'], Path(PROFILE['ca']), PROFILE['auth'])
    executor = InitialHostExecutor(client, backend, writer, profile=profile)
    runtime = await start(client, executor, installation_id=planned.INSTALLATION,
        node_id=document['node'], backend=backend)
    scope = Scope.parse(document['scope'])
    retained = None
    try:
        if document.get('mode') == 'observe':
            admission = parse_admission(document['admission'])
            await executor.recover_native_result(admission)
            await asyncio.sleep(2)
            owned = executor._owned[admission.attempt_id]
            phase = await executor.exchange(executor.address(owned))
        else:
            await executor.observe(scope, document['prior'])
            work = Work.parse({**document['work'], 'executor_epoch': runtime.executor_epoch})
            admission = await client.claim(work, str(uuid4()))
            def check():
                if datetime.now(timezone.utc) >= admission.lease_until:
                    raise AssertionError('actual original native admission expired')
            async def authorize(): await client.recheck(admission)
            guard = EffectGuard(authorize, check, admission)
            task = asyncio.create_task(executor.execute(work, guard))
            try:
                while not task.done():
                    owned = executor._owned.get(admission.attempt_id)
                    if owned is not None and owned.possible_dispatch:
                        try:
                            phase = await executor.exchange(executor.address(owned))
                            if phase.get('native', {}).get('attachment') is not None:
                                retained = phase['native']
                        except Exception:
                            pass
                    await asyncio.sleep(.1)
                outcome, evidence = await task
                assert outcome == 'succeeded'
                owned = executor._owned[admission.attempt_id]
                committed = await client.result(admission, 'succeeded', evidence, receipt_id=owned.phase_receipt)
                await executor.acknowledge(committed)
                phase = await executor.exchange(executor.address(owned))
            except BaseException as error:
                worker = executor.workers.get((scope, admission.attempt_id))
                print(json.dumps(dict(status='failed', actual_retained_native=retained,
                    reason=str(error), worker_error=worker.last_error if worker else None)), file=sys.stderr, flush=True)
                raise
        assert phase['status'] == 'succeeded' and phase['native']['committed'] is not None
        print(json.dumps(dict(admission=admission_wire(admission), native=phase['native'])), flush=True)
    finally:
        await runtime.aclose()
        await executor.aclose()
        writer.close()
        await client.aclose()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'frontend':
        asyncio.run(frontend(json.load(sys.stdin)))
    else:
        planned.Handler = ProviderHandler
        asyncio.run(planned.accept(native_fixture=Fixture()))
