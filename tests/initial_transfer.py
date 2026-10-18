"""BUG/HYPOTHESIS #751: byte bounds должны замыкаться от admission до root copy.

Один pure profile считает ASCII JSON/base64, credential framing и private
outer framing; scalar upper bounds выведены из wire validators, не guessed
constant overhead. Metadata/consumer bytes учитываются агрегатом каждой фазы.
"""
import base64
import hashlib
import importlib
import json
import unittest
from dataclasses import replace
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _host_inputs_data import selected


def profile():
    return importlib.import_module('mop.common.initial_transfer')


class TransferProfile(unittest.TestCase):
    def test_exact_profile_matches_actual_http_credential_and_private_serializations(self):
        from mop.common.credentials import frame
        from mop.node.host_inputs import pack, admission_wire
        m = profile()
        for action in ('prepare_body', 'bootstrap_body'):
            a, s, value = selected(action)
            value['git_name'] = '\U0001f600\\\"'*30
            exact = m.measure(value, scope=s.wire(), admission=admission_wire(a))
            private = pack(a, s, value)
            self.assertEqual(exact.http, len(m.canonical_json(value)))
            self.assertEqual(exact.credential, len(frame(dict(ok=True, result=value))))
            self.assertEqual(exact.private, len(private.raw))
            self.assertGreaterEqual(m.measure(value).private, exact.private)

    def test_size_only_base64_calculation_is_exact_for_all_padding_residues(self):
        m = profile()
        for count in (0, 1, 2, 3, 4, 131072, 150000, 262144, 537974):
            self.assertEqual(m.base64_size(count), len(base64.b64encode(b'x'*count)))

    def test_full_canonical_maximum_uses_bounded_bootstrap_profile_only(self):
        from mop.common.bootstrap_envelope import bounded_content, encode, MAX_ENVELOPE_BYTES
        m = profile()
        raw = encode(bounded_content(b'x'*262144, b'y'*131072, '\U0001f600'*1024,
            'f'*64, tuple(uuid4() for _ in range(32))))
        self.assertEqual(len(raw), MAX_ENVELOPE_BYTES)
        self.assertEqual(m.base64_size(len(raw)), 717300)
        a, s, value = selected('bootstrap_body')
        value['bootstrap_digest'] = hashlib.sha256(raw).hexdigest()
        value['bootstrap'] = dict(envelope=base64.b64encode(raw).decode())
        measured = m.measure(value)
        measured.require('bootstrap_body')
        self.assertGreater(measured.http, 262144)
        self.assertLessEqual(measured.private, 1048576)
        self.assertEqual(m.phase_limit('bootstrap_body'), 1048576)
        for action in ('prepare_body', 'initial_identity', 'native_exec', 'confirm_ready'):
            self.assertEqual(m.phase_limit(action), 262144)
            with self.assertRaisesRegex(m.TransferError, 'transfer'):
                measured.require(action)

    def test_size_only_consumers_equal_actual_bytes_including_json_escaped_metadata(self):
        m = profile()
        a, s, value = selected()
        raw = b'x'*150001
        consumer = value['consumers'][0]
        consumer['name'] = '\U0001f600\\\"'*40
        consumer['value'] = base64.b64encode(raw).decode()
        exact = m.measure(value)
        empty = json.loads(json.dumps(value))
        empty['consumers'][0]['value'] = ''
        sized = m.measure(empty).add_binary(len(raw))
        self.assertEqual(sized, exact)

    def test_explicit_node_file_caps_remain_hard_for_bootstrap(self):
        from mop.node.host_inputs import Limits
        m = profile()
        self.assertEqual(Limits().file_bytes('prepare_body'), 262144)
        self.assertEqual(Limits().file_bytes('bootstrap_body'), m.phase_limit('bootstrap_body'))
        for cap in (1024, 262144, 500000):
            limits = Limits(max_file_bytes=cap)
            self.assertEqual(limits.file_bytes('bootstrap_body'), cap)
            self.assertEqual(limits.file_bytes('prepare_body'), cap)

    def test_bound_work_covers_every_real_wire_field_and_largest_counters(self):
        m = profile()
        from mop.node.http_execution import Work
        a, s, value = selected('bootstrap_body')
        bound = m.bound_work(a.work.role, a.work.bundle_digest, a.work.action)
        self.assertEqual(set(bound), set(a.work.wire()))
        self.assertEqual(Work.parse(bound).wire(), bound)
        self.assertGreaterEqual(len(m.canonical_json(bound)), len(m.canonical_json(a.work.wire())))


if __name__ == '__main__':
    unittest.main()
