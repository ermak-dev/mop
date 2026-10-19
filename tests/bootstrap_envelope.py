"""Точные исполняемые bootstrap bytes против retained canonical digest (#751)."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hermetic  # noqa: F401
from _host_inputs_data import selected
from mop.common.initialcreate import parse_delivery


def canonical(value):
    # Формат существующего retained owner, не новый алгоритм сериализации.
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def encoded(raw):
    return base64.b64encode(raw).decode('ascii')


def document():
    return dict(format=1, tasks=encoded(b'- debug: msg=approved\r\n'),
        variables=encoded(b'approved: true\n'), provenance='fixture:private-provenance',
        commit='e'*40, secret_refs=[str(UUID(int=1)), str(UUID(int=2))])


def delivery(raw=None, *, digest=None):
    admission, scope, value = selected('bootstrap_body')
    raw = canonical(document()) if raw is None else raw
    value['bootstrap'] = dict(envelope=encoded(raw))
    value['bootstrap_digest'] = hashlib.sha256(raw).hexdigest() if digest is None else digest
    return admission, scope, value


# HYPOTHESIS: tasks/variables принимаются без связи с полным selected envelope;
# SHA256 транспорта доказывает перенос, но не согласие с одобренным content.
# SPEC: только exact canonical retained envelope; checksum формата 1 неизменен.
# RED: старый parser принял unapproved tasks с digest другого canonical envelope.
# SOLUTION: единый common codec и одно strict base64 поле envelope.
# RESULT: pure codec/body transport checks GREEN; bytes/checksum формата 1 прежние.
# STATUS: FIXED — see #751.
class BootstrapEnvelope(unittest.TestCase):
    def codec(self):
        from mop.common import bootstrap_envelope
        return bootstrap_envelope

    def refused(self, raw, *, digest=None):
        admission, _, value = delivery(raw, digest=digest)
        with self.assertRaises(ValueError) as error:
            parse_delivery(value, admission.work.wire())
        for private in ('private-provenance', 'unapproved-private', encoded(raw), repr(raw)):
            self.assertNotIn(private, str(error.exception))

    def test_mixed_fragments_are_not_the_selected_bootstrap(self):
        admission, _, value = delivery()
        approved = document()
        value['bootstrap'] = dict(tasks=encoded(b'- command: unapproved-private\n'),
            variables=approved['variables'])
        with self.assertRaises(ValueError, msg='mixed fragments accepted with another canonical envelope digest'):
            parse_delivery(value, admission.work.wire())

    def test_genuine_retained_envelope_roundtrip_preserves_exact_bytes_and_refs(self):
        raw = canonical(document())
        admission, _, value = delivery(raw)
        self.assertEqual(parse_delivery(value, admission.work.wire()), value)
        codec = self.codec()
        content = codec.decode_base64(value['bootstrap']['envelope'], value['bootstrap_digest'])
        self.assertEqual(content.tasks, b'- debug: msg=approved\r\n')
        self.assertEqual(content.variables, b'approved: true\n')
        self.assertEqual(content.provenance, document()['provenance'])
        self.assertEqual(content.commit, 'e'*40)
        self.assertEqual(content.secret_refs, (UUID(int=1), UUID(int=2)))
        self.assertNotIn('private-provenance', repr(content))
        self.assertEqual(codec.encode(content), raw)
        self.assertEqual(codec.decode(raw, hashlib.sha256(raw).hexdigest()), content)
        normalized = codec.bounded_content(content.tasks, content.variables, content.provenance,
            content.commit, tuple(reversed(content.secret_refs)))
        self.assertEqual(codec.encode(normalized), raw)

    def test_each_envelope_field_is_bound_to_the_selected_digest(self):
        original = document()
        digest = hashlib.sha256(canonical(original)).hexdigest()
        for key, item in dict(tasks=encoded(b'unapproved-private'), variables=encoded(b'unapproved-private'),
                provenance='other-private-provenance', commit='f'*64,
                secret_refs=[str(UUID(int=3))], format=2).items():
            with self.subTest(field=key):
                self.refused(canonical({**original, key: item}), digest=digest)

    def test_self_hashed_unknown_version_schema_provenance_refs_and_oversize_are_invalid(self):
        original = document()
        changes = [dict(unknown='unapproved-private'), dict(format=2), dict(format=True), dict(format=1.0),
            dict(tasks='%%%'), dict(tasks='YQ==='), dict(tasks='YR=='), dict(tasks=[]),
            dict(variables=encoded(b'x'*131073)), dict(tasks=encoded(b'x'*262145)),
            dict(provenance=''), dict(provenance='x'*1025), dict(provenance='private\nprovenance'),
            dict(provenance='https://user:private@host.invalid/repo'), dict(provenance='\ud800'),
            dict(commit='e'*39), dict(commit='E'*40), dict(secret_refs='not-a-list'),
            dict(secret_refs=[True]), dict(secret_refs=[str(UUID(int=1))]*2),
            dict(secret_refs=[str(UUID(int=i)) for i in range(33)]),
            dict(secret_refs=[str(UUID(int=2)), str(UUID(int=1))]),
            dict(secret_refs=['00000000000000000000000000000001']),
            dict(secret_refs=['AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA'])]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                self.refused(canonical({**original, **change}))
        for key in original:
            with self.subTest(missing=key):
                self.refused(canonical({k: v for k, v in original.items() if k != key}))

    def test_noncanonical_duplicate_truncated_and_nonobject_json_refuse(self):
        raw = canonical(document())
        for bad in (raw+b'\n', raw.replace(b',', b', ', 1), raw[:-1], b'[]', b'null', b'1',
                b'\xff', b'{"format":1,"format":1,'+raw[1:],
                json.dumps(document()).encode(), b'{"format":NaN}', b'['*2000+b']'*2000):
            self.refused(bad)

    def test_strict_outer_base64_and_no_duplicate_fragments_or_cross_phase_payload(self):
        admission, _, value = delivery()
        for bad in ('%%%', value['bootstrap']['envelope']+'=', '', 1, [],
                encoded(b'x'*(self.codec().MAX_ENVELOPE_BYTES+1))):
            changed = deepcopy(value)
            changed['bootstrap'] = dict(envelope=bad)
            with self.subTest(kind=type(bad).__name__), self.assertRaises(ValueError):
                parse_delivery(changed, admission.work.wire())
        for extra in (dict(tasks=document()['tasks']), dict(variables=document()['variables']), dict(version=1)):
            with self.assertRaises(ValueError):
                parse_delivery({**value, 'bootstrap': {**value['bootstrap'], **extra}}, admission.work.wire())
        for phase in ('prepare_body', 'initial_identity', 'native_exec', 'confirm_ready'):
            changed = deepcopy(value)
            changed['phase'] = changed['work']['action'] = phase
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                parse_delivery(changed, changed['work'])

    def test_empty_and_maximum_existing_retained_content_have_finite_unchanged_bounds(self):
        codec = self.codec()
        for tasks, variables, provenance, commit, refs in (
                (b'', b'', 'fixture', 'e'*40, ()),
                (b't'*262144, b'v'*131072, '\U00010000'*1024, 'e'*64,
                 tuple(UUID(int=i) for i in range(32)))):
            content = codec.bounded_content(tasks, variables, provenance, commit, refs)
            raw = codec.encode(content)
            self.assertEqual(codec.decode(raw, hashlib.sha256(raw).hexdigest()), content)
            admission, _, value = delivery(raw)
            self.assertEqual(parse_delivery(value, admission.work.wire()), value)
        self.assertEqual(len(raw), 537974)
        self.assertEqual(codec.MAX_ENVELOPE_BYTES, len(raw))
        self.assertEqual(len(encoded(raw)), 717300)
        self.refused(raw+b' ')

    def test_transport_hash_does_not_make_mixed_envelope_approved_for_the_body(self):
        from mop.node import host_inputs as transport
        from mop.node.http_execution import Refused
        admission, scope, value = delivery()
        raw = canonical({**document(), 'tasks': encoded(b'unapproved-private')})
        value['bootstrap']['envelope'] = encoded(raw)
        payload = transport.canonical_json(value)
        wire = transport.canonical_json(dict(schema_version=1, purpose=transport.PURPOSES['bootstrap_body'],
            scope=scope.wire(), admission=transport.admission_wire(admission),
            payload_digest=transport.digest(payload), payload=payload.decode()))
        descriptor = transport.Descriptor(scope, admission.attempt_id, transport.digest(wire))
        # Root подтверждает только opaque transport hash, не parse_delivery/YAML.
        envelope = transport.Envelope.parse(wire, descriptor, admission, backend_epoch=scope.epoch,
            node_id=scope.node_id, executor_epoch=scope.executor_epoch)
        with self.assertRaises(Refused):
            transport.parse_body(envelope)
        with self.assertRaises(Refused):
            transport.pack(admission, scope, value)


if __name__ == '__main__':
    unittest.main()
