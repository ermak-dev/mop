#!/usr/bin/env python3
"""Чистая конечная политика ротации #637: намерение раньше внешнего эффекта.

HYPOTHESIS: unknown требует readback, CAS и доказанного покоя, а не повтора.
Компенсация — два отдельных эффекта со своими отказами, не универсальный undo.
"""
import importlib
import pathlib
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class RotationTests(unittest.TestCase):
    def setUp(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "mop/server/secrets/rotation.py"
        self.assertTrue(path.exists(), "revisioned rotation safety policy is not implemented")
        self.r = importlib.import_module("mop.server.secrets.rotation")
        self.rotation = self.r.Rotation.stage(frozenset({"node-a/inc-1", "node-b/inc-2"}))

    def apply(self, method, snapshot=None, **kwargs):
        snapshot = snapshot or self.rotation
        result = getattr(snapshot, method)(expected_revision=snapshot.revision, **kwargs)
        self.assertEqual(result.revision, snapshot.revision + 1)
        return result

    def delivered(self):
        delivering = self.apply("begin_delivery")
        delivered = self.apply("record", delivering, outcome=self.r.Outcome.APPLIED,
                               receipts=delivering.required)
        return delivered

    def active(self):
        return self.apply("activate", self.delivered())

    def test_stages_receipts_and_retirement_are_explicit(self):
        r = self.r
        self.assertEqual(self.rotation.state, r.Stage.STAGED)
        self.assertEqual(self.rotation.revision, 0)
        delivering = self.apply("begin_delivery")
        self.assertEqual(delivering.state, r.Stage.DELIVERING)
        self.assertEqual(delivering.pending, r.Effect.DELIVER)
        with self.assertRaises(r.RotationError):
            self.apply("activate", delivering)
        for receipts in (frozenset(), frozenset({"node-a/inc-1"}),
                         frozenset({"node-a/inc-1", "node-b/inc-OLD"}),
                         delivering.required | {"extra"}):
            with self.assertRaises(r.RotationError):
                self.apply("record", delivering, outcome=r.Outcome.APPLIED, receipts=receipts)
        active = self.active()
        self.assertEqual(active.state, r.Stage.ACTIVE)
        retiring = self.apply("begin_retirement", active)
        self.assertEqual(retiring.pending, r.Effect.RETIRE_OLD)
        retired = self.apply("record", retiring, outcome=r.Outcome.APPLIED)
        self.assertEqual(retired.state, r.Stage.RETIRED)
        self.assertIsNone(retired.pending)
        self.assertEqual(self.rotation.state, r.Stage.STAGED)

    def test_crash_at_every_external_step_forbids_blind_replay(self):
        r = self.r
        failed = self.apply("record", self.apply("begin_retirement", self.active()),
                            outcome=r.Outcome.REJECTED)
        restoring = self.apply("begin_compensation", failed)
        restored = self.apply("record", restoring, outcome=r.Outcome.APPLIED)
        revoking = self.apply("begin_new_revocation", restored)
        snapshots = (self.apply("begin_delivery"),
                     self.apply("begin_retirement", self.active()), restoring, revoking)
        for snapshot in snapshots:
            uncertain = self.apply("record", snapshot, outcome=r.Outcome.UNKNOWN)
            self.assertEqual(uncertain.state, r.Stage.RECONCILING)
            self.assertEqual(uncertain.pending, snapshot.pending)
            for method in ("begin_delivery", "begin_retirement", "begin_compensation",
                           "begin_new_revocation", "retry_effect", "activate"):
                with self.assertRaises(r.RotationError):
                    self.apply(method, uncertain)
            still = self.apply("reconcile", uncertain, outcome=r.Observation.UNKNOWN)
            self.assertEqual(still.state, r.Stage.RECONCILING)
            with self.assertRaises(r.RotationError):
                self.apply("reconcile", uncertain, outcome=r.Observation.ABSENT, quiescent=False)
            absent = self.apply("reconcile", uncertain, outcome=r.Observation.ABSENT, quiescent=True)
            self.assertIsNone(absent.pending)
            # После readback новый эффект всё равно требует отдельного CAS-намерения.
            retried = self.apply("retry_effect", absent)
            self.assertEqual(retried.pending, snapshot.pending)
            self.assertEqual(retried.state, snapshot.state)
            with self.assertRaises(r.RotationError):
                self.apply("retry_effect", retried)
            kwargs = {"receipts": snapshot.required} if snapshot.pending == r.Effect.DELIVER else {}
            confirmed = self.apply("reconcile", uncertain, outcome=r.Observation.APPLIED, **kwargs)
            self.assertNotEqual(confirmed.state, r.Stage.RECONCILING)
            self.assertIsNone(confirmed.pending)

    def test_recovery_of_unacknowledged_committed_intent_requires_reconciliation(self):
        r = self.r
        pending = self.apply("begin_delivery")
        # Нельзя объявить отсутствие эффекта обычным результатом/таймаутом.
        with self.assertRaises(r.RotationError):
            self.apply("record", pending, outcome=r.Observation.ABSENT)
        with self.assertRaises(r.RotationError):
            self.apply("retry_effect", pending)
        with self.assertRaises(r.RotationError):
            self.apply("reconcile", pending, outcome=r.Observation.APPLIED, receipts=pending.required)
        unknown = self.apply("record", pending, outcome=r.Outcome.UNKNOWN)
        with self.assertRaises(r.RotationError):
            self.apply("reconcile", unknown, outcome=r.Observation.ABSENT, quiescent=True,
                       receipts=frozenset({"node-a/inc-1"}))

    def test_compensation_is_separate_fallible_and_preserves_both_causes(self):
        r = self.r
        for pending in (self.apply("begin_delivery"), self.apply("begin_retirement", self.active())):
            failed = self.apply("record", pending, outcome=r.Outcome.REJECTED)
            self.assertEqual(failed.state, r.Stage.FAILED)
            self.assertEqual(failed.original_failure, pending.pending)
            restoring = self.apply("begin_compensation", failed)
            self.assertEqual(restoring.pending, r.Effect.RESTORE_OLD)
            partial = self.apply("record", restoring, outcome=r.Outcome.REJECTED)
            self.assertEqual(partial.state, r.Stage.PARTIAL)
            self.assertEqual(partial.original_failure, pending.pending)
            self.assertEqual(partial.compensation_failure, r.Effect.RESTORE_OLD)
            with self.assertRaises(r.RotationError):
                self.apply("begin_new_revocation", partial)
            restored = self.apply("record", restoring, outcome=r.Outcome.APPLIED)
            revoking = self.apply("begin_new_revocation", restored)
            partial = self.apply("record", revoking, outcome=r.Outcome.REJECTED)
            self.assertEqual(partial.compensation_failure, r.Effect.REVOKE_NEW)
            self.assertEqual(partial.original_failure, pending.pending)
            done = self.apply("record", revoking, outcome=r.Outcome.APPLIED)
            self.assertEqual(done.state, r.Stage.COMPENSATED)
            self.assertEqual(done.original_failure, pending.pending)
            for terminal in (done, partial):
                with self.assertRaises(r.RotationError):
                    self.apply("begin_delivery", terminal)
        with self.assertRaises(r.RotationError):
            self.apply("begin_compensation", self.active())

    def test_cas_revisions_and_late_receipts_do_not_overwrite_new_decision(self):
        r = self.r
        pending = self.apply("begin_delivery")
        with self.assertRaises(r.RevisionConflict):
            pending.record(expected_revision=0, outcome=r.Outcome.APPLIED, receipts=pending.required)
        unknown = self.apply("record", pending, outcome=r.Outcome.UNKNOWN)
        with self.assertRaises(r.RevisionConflict):
            unknown.record(expected_revision=pending.revision, outcome=r.Outcome.APPLIED,
                           receipts=pending.required)
        for value in (True, -1, "1", None):
            with self.assertRaises(r.RotationError):
                pending.record(expected_revision=value, outcome=r.Outcome.UNKNOWN)
        for method, snapshot in (("activate", self.delivered()), ("begin_retirement", self.active()),
                                 ("begin_delivery", self.rotation), ("retry_effect", unknown)):
            with self.assertRaises(r.RevisionConflict):
                getattr(snapshot, method)(expected_revision=snapshot.revision + 1)

    def test_immutable_snapshot_and_rejection_of_untyped_or_unsafe_inputs(self):
        r = self.r
        recipients = {"receiver/inc-1"}
        staged = r.Rotation.stage(recipients)
        recipients.add("injected")
        self.assertEqual(staged.required, frozenset({"receiver/inc-1"}))
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            staged.revision = 9
        pending = self.apply("begin_delivery")
        for value in ("unknown", None, True):
            with self.assertRaises(r.RotationError):
                self.apply("record", pending, outcome=value)
        for required in (set(), {""}, "recipient", {3}):
            with self.assertRaises(r.RotationError):
                r.Rotation.stage(required)
        with self.assertRaises(r.RotationError):
            self.apply("record", pending, outcome=r.Outcome.UNKNOWN, receipts=pending.required)
        for method in ("activate", "begin_retirement", "begin_compensation", "begin_new_revocation", "retry_effect"):
            with self.assertRaises(r.RotationError) as caught:
                self.apply(method)
            self.assertNotIn("receiver/inc-1", str(caught.exception))
        self.assertNotIn("node-a", repr(pending))


if __name__ == "__main__":
    unittest.main()
