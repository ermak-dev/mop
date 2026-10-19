#!/usr/bin/env python3
"""HYPOTHESIS: HOST activation cannot address the prepared repo or large native.
SPEC: closed repo/r profile, immutable current-view paths and native-only size cap.
SOLUTION: отдельный fixed profile, не произвольные пути и не ослабление matches.
RESULT: layout (3), artifact (2), consumer (2), result (2), submission (2)
проверки наблюдались RED до реализации, теперь GREEN. Native graph закрыт;
combined root/node/body/systemd/PostgreSQL/native acceptance не установлена.
STATUS: FIXED — see #760 (finite values only; runtime task remains incomplete)
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _host_inputs_data import selected
from mop.driver.launch import Registration, RegisteredLayout


def registration(action='native_exec'):
    admission, scope, delivery = selected(action)
    return Registration(admission.work, delivery['bootstrap_revision'], delivery['bootstrap_digest'],
                        delivery['image_digest'], delivery['display_name'])


def host_layout(home, pin):
    try:
        return RegisteredLayout(home, pin, profile='host-native-v1')
    except TypeError as error:
        raise AssertionError('closed HOST native profile must address the existing repo/r workspace') from error


class LayoutTests(unittest.TestCase):
    def test_host_profile_addresses_prepared_repo_without_changing_default(self):
        pin = registration()
        home = Path('/tmp/body')
        old = RegisteredLayout(home, pin)
        self.assertEqual(old.clone, home/'puppets'/pin.display_name)
        self.assertEqual(old.runtime, home/'runtime'/pin.work.incarnation_id)
        layout = host_layout(home, pin)
        self.assertEqual(layout.clone, home/'repo')
        self.assertEqual(layout.runtime, home/'r')
        self.assertEqual(layout.tmux_socket, home/'r/tmux')
        self.assertEqual(layout.launcher, home/'r/launch')
        self.assertEqual(layout.hook_socket, home/'r/h')
        self.assertEqual(layout.management, home/'r/m')
        self.assertTrue(pin.matches(pin.work))
        self.assertFalse(pin.matches(replace(pin.work, action='confirm_ready')))
        with self.assertRaises(ValueError):
            RegisteredLayout(home, pin, profile='/tmp/arbitrary')

    def test_host_profile_checks_hook_and_native_socket_byte_limits_before_spawn(self):
        pin = registration()
        # /r/session.sock = 15 bytes; /r/h = 4 bytes. Оба actual byte limits
        # проверяются до materialization, без усечения или fallback discovery.
        accepted = Path('/'+'x'*91)
        self.assertEqual(len(bytes(accepted/'r/session.sock')), 107)
        layout = host_layout(accepted, pin)
        self.assertLessEqual(len(bytes(layout.hook_socket)), 100)
        with self.assertRaisesRegex(ValueError, 'hook|socket'):
            host_layout(Path('/'+'x'*92), pin)
        with self.assertRaises(ValueError):
            host_layout(Path('/'+'é'*50), pin)

    def test_current_phase_view_preserves_anchor_profile_and_exact_registration(self):
        from mop.node.producer_views import _CurrentProducerView
        pin = registration()
        layout = host_layout(Path('/tmp/body'), pin)
        current = replace(pin, work=replace(pin.work, action='confirm_ready', revision=pin.work.revision+1))
        runtime = SimpleNamespace(_layout=layout, _proof=object(), _identity=object())
        view = _CurrentProducerView(runtime, SimpleNamespace(registration=current))
        self.assertEqual(view.layout.profile, layout.profile)
        self.assertEqual(view.layout.clone, layout.clone)
        self.assertEqual(view.layout.runtime, layout.runtime)
        self.assertTrue(view.layout.registration.matches(current.work))
        self.assertFalse(view.layout.registration.matches(pin.work))


def native_api():
    import importlib
    import importlib.util
    assert importlib.util.find_spec('mop.node.host_native') is not None, \
        'strict HOST native consumer codec is missing (#760)'
    return importlib.import_module('mop.node.host_native')


def model_delivery():
    import base64
    import hashlib
    from uuid import uuid4
    from _body_fixture import bundle
    from mop.node.host_inputs import canonical_json
    a, scope, value = selected('native_exec')
    role = bundle()
    a = replace(a, work=replace(a.work, role=role.role, bundle_digest=role.digest))
    value['work'] = a.work.wire()
    value['role'] = dict(manifest=role.manifest.to_dict(), prompt=base64.b64encode(role.prompt).decode())
    config_id, credential_id = str(uuid4()), str(uuid4())
    config = dict(schema_version=1, profile_version=1, provider_profile='owned-fixture-v1',
        credential_version=credential_id, proof_strategy='bootstrap-to-desired',
        bootstrap_model='fixture-bootstrap', desired_model='fixture-desired')
    raw = canonical_json(config)
    value['selection'].update(desired_model=config['desired_model'], model_config_digest=hashlib.sha256(raw).hexdigest(),
        consumers=[dict(purpose='model_config', version_id=config_id),
                   dict(purpose='model_credential', version_id=credential_id)])
    credential = dict(schema_version=1, provider_profile='owned-fixture-v1', token='fixture-secret-not-for-repr')
    value['consumers'] = [dict(purpose='model_config', version_id=config_id, name='NODE_OPTIONS',
        kind='file', value=base64.b64encode(raw).decode()),
        dict(purpose='model_credential', version_id=credential_id, name='../../arbitrary',
             kind='credential', value=base64.b64encode(canonical_json(credential)).decode())]
    return a, scope, value


def ready_fixture():
    from uuid import UUID, uuid4
    from mop.common.readiness import ReadyRequest, ReadyEvidence, work_document
    from mop.common.telemetry import SessionScope
    a, _, value = selected()
    a = replace(a, work=replace(a.work, action='confirm_ready'))
    w = a.work
    scope = SessionScope(*(UUID(getattr(w, key)) for key in
        ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')), 'fixture-session', uuid4())
    request = ReadyRequest(work_document(w.wire()), UUID(a.attempt_id), UUID(a.request_id),
        a.revision, a.lease_until, scope, 123, str(uuid4())+':456')
    evidence = ReadyEvidence(request, UUID(value['bootstrap_revision']), value['bootstrap_digest'],
        value['image_digest'], value['display_name'], UUID(value['image_revision']),
        tuple(sorted(value['image_policy'].items())), uuid4(), uuid4(), 1, 2**40, 1, 1, 1, 'fixture-model')
    return a, evidence


class ResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_carries_original_full_ready_evidence_with_stable_receipt(self):
        from uuid import uuid4
        from mop.node.http_execution import Runtime, CommittedResult
        a, evidence = ready_fixture()
        identity = str(uuid4())
        observed = []
        class Client:
            async def claim(self, work, request): return a
            async def recheck(self, admission): pass
            async def result(self, admission, outcome, kind, *, receipt_id=None, ready_evidence=None):
                observed.append((admission, outcome, kind, receipt_id, ready_evidence))
                return CommittedResult(admission, receipt_id, a.revision+1, outcome, kind)
        class Executor:
            async def execute(self, work, guard): return 'succeeded', 'ready'
            def result_identity(self, admission):
                assert admission is a
                return identity
            def result_evidence(self, admission):
                assert admission is a
                return evidence
            async def acknowledge(self, committed): observed.append(committed)
        r = Runtime(Client(), Executor(), node_id=a.work.node_id, epoch=a.work.epoch,
                    executor_epoch=a.work.executor_epoch)
        r._ready = True
        await r._execute(a.work)
        self.assertIsNone(r.last_error)
        self.assertIs(observed[0][4], evidence, 'Runtime discarded the query snapshot before result COMMIT')
        self.assertEqual(observed[0][3], identity)
        self.assertEqual(observed[1].receipt_id, identity)

    async def test_runtime_does_not_send_success_for_missing_or_foreign_ready_evidence(self):
        from mop.node.http_execution import Runtime
        a, evidence = ready_fixture()
        for proposed in (None, evidence.wire(), replace(evidence,
                request=replace(evidence.request, revision=a.revision+1))):
            sent = []
            class Client:
                async def claim(self, work, request): return a
                async def recheck(self, admission): pass
                async def result(self, *args, **kwargs): sent.append((args, kwargs))
            class Executor:
                async def execute(self, work, guard): return 'succeeded', 'ready'
                def result_evidence(self, admission): return proposed
            r = Runtime(Client(), Executor(), node_id=a.work.node_id, epoch=a.work.epoch,
                        executor_epoch=a.work.executor_epoch)
            r._ready = True
            await r._execute(a.work)
            self.assertEqual(sent, [], 'no successful result may shed or replace its typed query evidence')
            self.assertIn('readiness', r.last_error)


class ModelTests(unittest.TestCase):
    def test_explicit_pinned_strategy_uses_only_installed_verified_profile(self):
        m = native_api()
        _, _, value = model_delivery()
        profile = m.ProviderProfile('owned-fixture-v1', 'https://provider.invalid:9443', Path('/opt/fixed/provider-ca'), 'api-key-v1')
        result = m.ModelInputs.parse(value, profile)
        self.assertEqual(result.bootstrap_model, 'fixture-bootstrap')
        self.assertEqual(result.desired_model, 'fixture-desired')
        self.assertEqual(result.config_digest, value['selection']['model_config_digest'])
        self.assertEqual(result.config_version, value['selection']['consumers'][0]['version_id'])
        self.assertEqual(result.credential_version, value['selection']['consumers'][1]['version_id'])
        self.assertEqual(result.environment(profile), dict(ANTHROPIC_BASE_URL='https://provider.invalid:9443',
            ANTHROPIC_API_KEY='fixture-secret-not-for-repr', NODE_EXTRA_CA_CERTS='/opt/fixed/provider-ca'))
        self.assertNotIn('fixture-secret-not-for-repr', repr(result))
        self.assertNotIn('NODE_OPTIONS', repr(result))
        self.assertNotIn('../../arbitrary', repr(result))
        self.assertNotIn('fixture-secret-not-for-repr', repr(result.pins()))
        self.assertEqual(result.wire(), result.pins().wire(), 'native metadata projection must never serialize the credential')
        self.assertNotIn('token', result.wire())

    def test_missing_or_conflicting_approval_cannot_choose_a_fallback_model(self):
        import base64
        import copy
        import hashlib
        from uuid import uuid4
        from mop.node.host_inputs import canonical_json, _json
        from mop.node.http_execution import Refused
        m = native_api()
        _, _, value = model_delivery()
        profile = m.ProviderProfile('owned-fixture-v1', 'https://provider.invalid', Path('/opt/fixed/ca'), 'api-key-v1')
        cases = [dict(proof_strategy='same-model'), dict(proof_strategy=None), dict(bootstrap_model='fixture-desired'),
                 dict(bootstrap_model='bad\n/model injected'), dict(desired_model='fixture-other'),
                 dict(desired_model='/model injected'), dict(desired_model='-flag'),
                 dict(provider_profile='unknown'), dict(credential_version=str(uuid4())),
                 dict(schema_version=True), dict(profile_version=2), dict(environment={'NODE_OPTIONS': 'bad'})]
        for change in cases:
            bad = copy.deepcopy(value)
            config = _json(base64.b64decode(bad['consumers'][0]['value']))
            config.update(change)
            raw = canonical_json(config)
            bad['consumers'][0]['value'] = base64.b64encode(raw).decode()
            bad['selection']['model_config_digest'] = hashlib.sha256(raw).hexdigest()
            with self.subTest(change=change), self.assertRaises(Refused):
                m.ModelInputs.parse(bad, profile)
        for change in (dict(token='x\ny'), dict(token=''), dict(token='x'*8193),
                       dict(provider_profile='other'), dict(schema_version=True), dict(url='https://other.invalid')):
            bad = copy.deepcopy(value)
            credential = _json(base64.b64decode(bad['consumers'][1]['value']))
            credential.update(change)
            bad['consumers'][1]['value'] = base64.b64encode(canonical_json(credential)).decode()
            with self.subTest(credential=tuple(change)), self.assertRaises(Refused) as caught:
                m.ModelInputs.parse(bad, profile)
            self.assertNotIn('fixture-secret-not-for-repr', str(caught.exception))
        for origin in ('http://provider.invalid', 'https://user:pass@provider.invalid',
                       'https://provider.invalid/path', 'https://provider.invalid?x=1'):
            with self.subTest(origin=origin), self.assertRaises(Refused):
                m.ProviderProfile('owned-fixture-v1', origin, Path('/opt/fixed/ca'), 'api-key-v1')
        with self.assertRaises(Refused):
            m.ModelInputs.parse(value, None)


class SubmissionTests(unittest.TestCase):
    def test_native_submission_is_bound_to_actual_admission_descriptor_and_retained_reference(self):
        import copy
        from uuid import uuid4
        from mop.node.host_inputs import pack
        from mop.node.host_phases import Projection, Submission
        from mop.node.host_scope import Reference, encode
        from mop.node.http_execution import Refused
        m = native_api()
        self.assertTrue(hasattr(m, 'NativeSubmission'), 'native phase needs its own closed admitted submission')
        a, scope, value = model_delivery()
        profile = m.ProviderProfile('owned-fixture-v1', 'https://provider.invalid', Path('/opt/fixed/ca'), 'api-key-v1')
        pins = m.ModelInputs.parse(value, profile).pins()
        descriptor = pack(a, scope, value).descriptor
        projection = Projection.from_delivery(value)
        reference = Reference(scope, '1'*32, str(uuid4()))
        request = m.NativeSubmission(a, descriptor, projection, reference, pins)
        self.assertEqual(m.NativeSubmission.decode(request.encode()), request)
        self.assertNotIn('fixture-secret-not-for-repr', request.encode().decode())
        with self.assertRaises(Refused):
            Submission(a, descriptor, projection, reference)
        for change in (dict(reference=None), dict(admission=replace(a, attempt_id=str(uuid4()))),
                       dict(admission=replace(a, work=replace(a.work, action='confirm_ready'))),
                       dict(projection=replace(projection, bundle_digest='e'*64)), dict(pins=pins.wire())):
            with self.subTest(change=tuple(change)), self.assertRaises(Refused):
                replace(request, **change)
        for key in ('argv', 'environment', 'callback', 'path', 'token'):
            bad = copy.deepcopy(request.wire())
            bad[key] = 'injected'
            with self.assertRaises(Refused):
                m.NativeSubmission.decode(encode(bad))

    def test_confirm_submission_has_no_consumers_and_borrows_only_original_native_attachment(self):
        from datetime import timedelta
        from uuid import uuid4
        from mop.node.host_phases import Projection
        from mop.node.host_scope import Reference, encode
        from mop.node.http_execution import Refused
        m = native_api()
        self.assertTrue(hasattr(m, 'ConfirmSubmission'), 'confirm must not be forced through a consumer snapshot schema')
        a, scope, value = model_delivery()
        a = replace(a, work=replace(a.work, action='confirm_ready', step_id=str(uuid4())))
        reference = Reference(scope, '1'*32, str(uuid4()))
        native_attempt = str(uuid4())
        request = m.ConfirmSubmission(a, reference, Projection.from_delivery(value), native_attempt)
        self.assertEqual(m.ConfirmSubmission.decode(request.encode()), request)
        self.assertNotIn('descriptor', request.wire())
        self.assertNotIn('consumers', request.wire())
        for changes in (dict(descriptor={}), dict(consumers=[]), dict(model='different')):
            with self.assertRaises(Refused):
                m.ConfirmSubmission.decode(encode({**request.wire(), **changes}))
        for change in (dict(native_attempt=a.attempt_id), dict(reference=None),
                       dict(admission=replace(a, work=replace(a.work, action='native_exec'))),
                       dict(admission=replace(a, lease_until=a.lease_until-timedelta(seconds=300)))):
            with self.subTest(change=tuple(change)), self.assertRaises(Refused):
                replace(request, **change)


class ArtifactTests(unittest.TestCase):
    def test_native_only_size_exception_streams_exact_bytes_and_keeps_default_cap(self):
        import hashlib
        import tempfile
        from mop.node.host_phase_manifest import verify_artifact
        from mop.node.http_execution import Refused
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'native'
            size = 67108865
            with filename.open('wb') as target:
                target.truncate(size)
            filename.chmod(0o700)
            h = hashlib.sha256()
            with filename.open('rb') as source:
                while chunk := source.read(65536):
                    h.update(chunk)
            expected = h.hexdigest()
            with self.assertRaises(Refused):
                verify_artifact(filename, expected, root_owned=False, executable=True)
            try:
                result = verify_artifact(filename, expected, root_owned=False,
                                         executable=True, native_size=size)
            except TypeError as error:
                self.fail('exact classified native artifact must support the pinned ELF above 64 MiB: '+str(error))
            self.assertEqual(result, expected)
            for recorded in (True, size-1, size+1, 536870913):
                with self.assertRaises(Refused):
                    verify_artifact(filename, expected, root_owned=False, executable=True, native_size=recorded)
            with self.assertRaises(Refused):
                verify_artifact(filename, expected, root_owned=False, native_size=size)
            with self.assertRaises(Refused):
                verify_artifact(filename, '0'*64, root_owned=False, executable=True, native_size=size)
            with filename.open('wb') as target:
                target.truncate(536870913)
            with self.assertRaises(Refused):
                verify_artifact(filename, expected, root_owned=False, executable=True, native_size=536870913)

    def test_manifest_size_exception_is_exactly_the_native_tool_not_arbitrary_data(self):
        import copy
        from mop.node.host_inputs import canonical_json
        from mop.node.host_phase_manifest import RuntimeManifest, TOOLS, DIRECTORIES
        from mop.node.http_execution import Refused
        image = selected()[2]
        tools = {name: '/opt/fixed/'+name for name in (*TOOLS, 'native', 'tmux', 'hook', 'mcp')}
        value = dict(schema_version=2,
            capacity=dict(cpu_mhz_per_core=2000, cpu_weight=75, tasks_max=64,
                          memory_mb_max=4096, cpu_mhz_max=8000, disk_mode='shared-reservation'),
            images=[{k: image[k] for k in ('image_revision',)} | dict(logical_pin=image['image_digest'],
                    policy_id=image['image_policy']['policy_id'])], tools=tools,
            directories={name: '/opt/fixed/'+name for name in DIRECTORIES},
            artifacts=[dict(path=filename, sha256='a'*64, executable=name not in ('ca', 'ansible_config'))
                       for name, filename in tools.items()])
        native = next(item for item in value['artifacts'] if item['path'] == tools['native'])
        native.update(kind='native', size=285793528)
        try:
            parsed = RuntimeManifest.parse(canonical_json(value))
        except Refused as error:
            self.fail('installed native closure needs a classified exact-size v2 manifest: '+str(error))
        self.assertEqual(parsed.tools['native'], Path(tools['native']))
        for mutate in ('foreign-classification', 'unclassified-native', 'over-cap', 'native-data', 'bool-size'):
            bad = copy.deepcopy(value)
            entry = next(item for item in bad['artifacts'] if item['path'] == tools['native'])
            if mutate == 'foreign-classification':
                bad['artifacts'][0].update(kind='native', size=285793528)
            elif mutate == 'unclassified-native':
                del entry['kind'], entry['size']
            elif mutate == 'over-cap':
                entry['size'] = 536870913
            elif mutate == 'native-data':
                entry['executable'] = False
            else:
                entry['size'] = True
            with self.subTest(mutate=mutate), self.assertRaises(Refused):
                RuntimeManifest.parse(canonical_json(bad))


if __name__ == '__main__':
    unittest.main()
