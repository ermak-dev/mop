"""Strict stdlib ready DTO/client (#749), без Django/native implementation.

HYPOTHESIS: frozen DTO не должен скрывать duplicate/noncanonical policy keys;
wire parser должен отвергать extensions, bool/counters и substituted request.
SOLUTION: strict stdlib locators и canonical frozen snapshot; MachineClient
делает один POST и связывает ответ с exact request без retry/fallback.
RESULT: constructor RED на duplicate/noncanonical policy, затем 4/4 GREEN.
STATUS: FIXED — see #749
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from mop.common.readiness import ReadyRequest, ReadyEvidence
from mop.node.http_execution import MachineClient, Refused, Unavailable


def fixture():
    ids = {key: str(uuid4()) for key in ('epoch', 'operation_id', 'step_id', 'target_id',
        'incarnation_id', 'node_id', 'executor_epoch', 'grant_id')}
    until = datetime.now(timezone.utc)+timedelta(minutes=5)
    work = dict(schema_version=1, **ids, owner_id='1', generation='1', revision='1',
        role='worker', bundle_digest='b'*64, action='confirm_ready', not_after=until.isoformat())
    request = dict(schema_version=1, work=work, admission=dict(attempt_id=str(uuid4()),
        request_id=str(uuid4()), revision='1', lease_until=(until-timedelta(seconds=1)).isoformat()),
        scope=dict(**{key: ids[key] for key in
            ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')},
            session_id='owned-session', producer_epoch=str(uuid4())),
        physical=dict(pid='123', process_start=str(uuid4())+':123'))
    image = str(uuid4())
    value = dict(schema_version=1, request=request, launch=dict(bootstrap_revision=str(uuid4()),
        bootstrap_digest='a'*64, image_digest=hashlib.sha256(('image-revision:'+image).encode()).hexdigest(),
        display_name='pu-owned-1', image_revision=image,
        image_policy=dict(policy_id=str(uuid4()), cpu=100, memory_mb=256, disk_mb=512)),
        native_attempt_id=str(uuid4()), producer_id=str(uuid4()), producer_revision='1', receipt_id='42',
        confirmed_sequence='1', observed_sequence='2', observation_revision='3', model='selected-model')
    return request, value


class ReadinessCodec(unittest.TestCase):
    def test_roundtrip_is_exact_and_has_a_real_positive_receipt_identity(self):
        request, value = fixture()
        addressed = ReadyRequest.parse(request)
        evidence = ReadyEvidence.parse(value, addressed)
        self.assertEqual(evidence.wire(), value)
        self.assertEqual(evidence.receipt_id, 42)
        value['launch']['image_policy']['cpu'] = 999
        request['work']['owner_id'] = '999'
        self.assertEqual(dict(evidence.image_policy_fields)['cpu'], 100)
        self.assertEqual(evidence.request.work['owner_id'], '1')

    def test_constructor_cannot_hide_duplicate_or_noncanonical_policy(self):
        _, value = fixture()
        evidence = ReadyEvidence.parse(value)
        policies = (evidence.image_policy_fields+(evidence.image_policy_fields[0],),
                    tuple(reversed(evidence.image_policy_fields)))
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                replace(evidence, image_policy_fields=policy)

    def test_request_and_response_reject_extensions_and_scalar_substitutions(self):
        request, value = fixture()
        paths = [('schema_version',), ('request', 'schema_version'), ('request', 'work', 'schema_version'),
            ('receipt_id',), ('producer_revision',), ('confirmed_sequence',), ('observed_sequence',),
            ('observation_revision',), ('request', 'admission', 'revision'), ('request', 'physical', 'pid'),
            ('launch', 'image_policy', 'cpu')]
        for path in paths:
            for replacement in (True, False, None, [], {}, 0, 1.5, '01'):
                doc = deepcopy(value)
                parent = doc
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = replacement
                with self.subTest(path=path, replacement=replacement), self.assertRaises(ValueError):
                    ReadyEvidence.parse(doc)
        for path in ((), ('request',), ('request', 'work'), ('request', 'admission'),
                ('request', 'physical'), ('request', 'scope'), ('launch',), ('launch', 'image_policy')):
            doc = deepcopy(value)
            parent = doc
            for key in path:
                parent = parent[key]
            parent['caller_authority'] = 'not-authority'
            with self.subTest(extension=path), self.assertRaises(ValueError):
                ReadyEvidence.parse(doc)
        doc = deepcopy(value)
        doc['request']['admission']['request_id'] = str(uuid4())
        with self.assertRaises(ValueError):
            ReadyEvidence.parse(doc, ReadyRequest.parse(request))


class ReadinessClient(unittest.IsolatedAsyncioTestCase):
    async def test_strict_exact_query_uses_one_post_and_does_not_retry_failure(self):
        request, response = fixture()
        response = deepcopy(response)
        class Transport:
            def __init__(self):
                self.calls = []
                self.error = None
            async def request(self, method, path, payload=None):
                self.calls.append((method, path, payload))
                if self.error is not None:
                    raise self.error
                return response
        transport = Transport()
        client = MachineClient(transport)
        result = await client.read_ready_receipt(request)
        self.assertEqual(result.receipt_id, 42)
        self.assertEqual(transport.calls, [('POST', 'work/ready-receipt', request)])
        transport.calls.clear()
        transport.error = Unavailable('query unavailable')
        with self.assertRaises(Unavailable):
            await client.read_ready_receipt(request)
        self.assertEqual(len(transport.calls), 1, 'read-only POST must not become an automatic retry lane')
        transport.error = None
        response['request']['scope']['session_id'] = 'substituted-session'
        with self.assertRaises(Refused):
            await client.read_ready_receipt(request)


if __name__ == '__main__':
    unittest.main()
