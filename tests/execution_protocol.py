"""Framework-free v1 wire, node не импортирует эти серверные модули."""
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import _lib  # noqa: F401
import hermetic  # noqa: F401
from mop.server.sessions import protocol as p


# FEATURE: конечные типизированные DTO не превращают некорректный JSON в 500.
# HYPOTHESIS: membership по list/dict даёт TypeError вместо validation refusal.
# SOLUTION: строгие скалярные типы и конечные коды проверяются до membership.
# RESULT: шесть framework-free проверок green после реального validation red.
# STATUS: FIXED — see #639
class ExecutionWire(unittest.TestCase):
    def setUp(self):
        self.work = p.WorkV1(epoch=uuid4(), operation_id=uuid4(), step_id=uuid4(),
            target_id=uuid4(), incarnation_id=uuid4(), node_id=uuid4(), executor_epoch=uuid4(),
            owner_id=1, generation=2, revision=3, role='worker', bundle_digest='a'*64,
            grant_id=uuid4(), action='restart_session', not_after=datetime.now(timezone.utc)+timedelta(minutes=1))

    def test_wire_roundtrip_has_exact_v1_fields(self):
        wire = p.work_to_wire(self.work)
        self.assertEqual(p.work_from_wire(wire), self.work)
        self.assertEqual(wire['schema_version'], 1)
        self.assertEqual(wire['generation'], '2')

    def test_non_scalar_action_is_validation_refusal(self):
        for value in ([], {}, True, None):
            with self.subTest(value=value):
                wire = p.work_to_wire(self.work)
                wire['action'] = value
                with self.assertRaises(ValueError):
                    p.work_from_wire(wire)

    def test_extra_fields_and_ambiguous_numbers_are_denied(self):
        wire = p.work_to_wire(self.work)
        with self.assertRaises(ValueError):
            p.work_from_wire({**wire, 'shell': 'id'})
        for value in (True, 1, '01', '-1', '9223372036854775808'):
            with self.assertRaises(ValueError):
                p.work_from_wire({**wire, 'revision': value})
        with self.assertRaises(ValueError):
            p.work_from_wire({**wire, 'schema_version': True})

    def test_partial_role_naive_deadline_and_noncanonical_uuid_are_denied(self):
        wire = p.work_to_wire(self.work)
        for change in ({'role': ''}, {'bundle_digest': 'a'}, {'grant_id': ''},
                       {'not_after': '2026-10-16T00:00:00'}, {'node_id': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'}):
            with self.assertRaises(ValueError):
                p.work_from_wire({**wire, **change})

    def test_receipt_codes_reject_non_scalar_or_unregistered_values(self):
        for outcome, evidence in (([], 'confirmed'), ('succeeded', {}), ('success-ish', 'confirmed'),
                                  ('unknown', 'raw-secret-output')):
            with self.assertRaises(ValueError):
                p.ReceiptV1(uuid4(), uuid4(), 2, outcome, evidence)

    def test_fact_page_cannot_claim_foreign_node_or_arbitrary_fields(self):
        now = datetime.now(timezone.utc)
        item = p.FactV1(self.work.target_id, self.work.incarnation_id, 1, now, now,
                       'fresh', (('shell', 'id'),))
        with self.assertRaises(ValueError):
            p.FactPageV1(self.work.node_id, 'clone', True, (item,)).validate(
                node_id=self.work.node_id, kind='clone', limit=10)
        with self.assertRaises(ValueError):
            p.FactPageV1(uuid4(), 'clone', True, ()).validate(
                node_id=self.work.node_id, kind='clone', limit=10)


if __name__ == '__main__':
    unittest.main()
