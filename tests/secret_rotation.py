#!/usr/bin/env python3
"""Чистая конечная политика ротации #637: намерение раньше внешнего эффекта.

HYPOTHESIS: unknown требует readback, CAS и доказанного покоя, а не повтора.
Компенсация — два отдельных эффекта со своими отказами, не универсальный undo.
SOLUTION: конечные состояния, CAS-намерения, подтверждение и отдельный readback.
RESULT: локальные проверки проходят; транзакции и внешние эффекты не реализованы.
STATUS: FIXED — see #637 (только чистая политика, не вся задача).
"""
import importlib
import pathlib
import unittest
from dataclasses import FrozenInstanceError

import hermetic  # noqa: F401 -- изоляция установки до импорта mop
from _lib import offline


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

    def test_confirmed_partial_delivery_can_be_compensated_without_replay(self):
        # HYPOTHESIS: подтверждённая частичная доставка с закончившимся
        # исполнителем не имеет пути к компенсации и навсегда висит в unknown.
        # SOLUTION: отдельный readback PARTIAL и recovery_required сохраняют
        # реальные receipts, не открывая повтор исходного эффекта.
        r = self.r
        observation = getattr(r.Observation, "PARTIAL", None)
        self.assertIsNotNone(observation, "confirmed partial readback must be representable")
        reason = getattr(r.FailureReason, "PARTIAL", None)
        self.assertIsNotNone(reason, "partial application needs a distinct safe reason")
        pending = self.apply("begin_delivery")
        unknown = self.apply("record", pending, outcome=r.Outcome.UNKNOWN)
        receipts = frozenset({"node-a/inc-1"})
        with self.assertRaises(r.RotationError):
            self.apply("reconcile", unknown, outcome=observation, receipts=receipts,
                       quiescent=False)
        recovered = self.apply("reconcile", unknown, outcome=observation,
                               receipts=receipts, quiescent=True)
        self.assertEqual(recovered.state.value, "recovery_required")
        self.assertEqual(recovered.receipts, receipts)
        self.assertEqual(recovered.original_failure, r.Effect.DELIVER)
        self.assertEqual(recovered.original_reason, reason)
        self.assertIsNone(recovered.pending)
        self.assertIsNone(recovered.retryable)
        from dataclasses import replace
        for changes in ({"original_reason": r.FailureReason.PRECONDITION},
                        {"state": r.Stage.FAILED},
                        {"receipts": recovered.required | {"unrelated/inc-3"}}):
            with self.assertRaises(r.RotationError):
                replace(recovered, **changes)
        for method in ("retry_effect", "activate", "begin_delivery", "begin_retirement"):
            with self.assertRaises(r.RotationError):
                self.apply(method, recovered)
        with self.assertRaises(r.RevisionConflict):
            recovered.begin_compensation(expected_revision=unknown.revision)
        restoring = self.apply("begin_compensation", recovered)
        restored = self.apply("record", restoring, outcome=r.Outcome.APPLIED)
        revoking = self.apply("begin_new_revocation", restored)
        done = self.apply("record", revoking, outcome=r.Outcome.APPLIED)
        self.assertEqual(done.state, r.Stage.COMPENSATED)
        self.assertEqual(done.receipts, receipts)
        self.assertEqual(done.original_reason, reason)

    def test_partial_observation_needs_quiescence_and_cannot_masquerade_as_rejection(self):
        r = self.r
        observation = getattr(r.Observation, "PARTIAL", None)
        self.assertIsNotNone(observation, "partial readback policy is missing")
        reason = getattr(r.FailureReason, "PARTIAL", None)
        self.assertIsNotNone(reason)
        retiring = self.apply("begin_retirement", self.active())
        unknown = self.apply("record", retiring, outcome=r.Outcome.UNKNOWN)
        with self.assertRaises(r.RotationError):
            self.apply("reconcile", unknown, outcome=observation, quiescent=False)
        recovered = self.apply("reconcile", unknown, outcome=observation, quiescent=True)
        self.assertEqual(recovered.state.value, "recovery_required")
        self.assertEqual(recovered.original_failure, r.Effect.RETIRE_OLD)
        self.assertEqual(recovered.receipts, recovered.required)
        restoring = self.apply("begin_compensation", recovered)
        uncertain_restore = self.apply("record", restoring, outcome=r.Outcome.UNKNOWN)
        partial = self.apply("reconcile", uncertain_restore, outcome=observation, quiescent=True)
        self.assertEqual(partial.state, r.Stage.PARTIAL)
        self.assertEqual(partial.original_reason, reason)
        self.assertEqual(partial.compensation_reason, reason)
        self.assertEqual(partial.compensation_failure, r.Effect.RESTORE_OLD)
        with self.assertRaises(r.RotationError):
            self.apply("retry_effect", partial)
        for pending in (self.apply("begin_delivery"), restoring):
            with self.assertRaises(r.RotationError):
                self.apply("record", pending, outcome=r.Outcome.REJECTED, reason=reason)

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

    def test_original_and_compensation_reasons_are_retained_without_raw_errors(self):
        r = self.r
        self.assertTrue(hasattr(r, "FailureReason"), "rotation must retain both safe failure reasons")
        failed = self.apply("record", self.apply("begin_delivery"), outcome=r.Outcome.REJECTED,
                            reason=r.FailureReason.PRECONDITION)
        restoring = self.apply("begin_compensation", failed)
        unknown = self.apply("record", restoring, outcome=r.Outcome.UNKNOWN)
        self.assertEqual(unknown.original_reason, r.FailureReason.PRECONDITION)
        observed = self.apply("reconcile", unknown, outcome=r.Observation.APPLIED)
        revoking = self.apply("begin_new_revocation", observed)
        partial = self.apply("record", revoking, outcome=r.Outcome.REJECTED,
                             reason=r.FailureReason.UNAVAILABLE)
        self.assertEqual(partial.original_reason, r.FailureReason.PRECONDITION)
        self.assertEqual(partial.compensation_reason, r.FailureReason.UNAVAILABLE)
        from dataclasses import replace
        for changes in ({"original_reason": None}, {"compensation_reason": None},
                        {"original_reason": "synthetic credential: do not echo"}):
            with self.assertRaises(r.RotationError):
                replace(partial, **changes)
        pending = self.apply("begin_delivery")
        for outcome in (r.Outcome.APPLIED, r.Outcome.UNKNOWN):
            with self.assertRaises(r.RotationError):
                self.apply("record", pending, outcome=outcome, reason=r.FailureReason.REFUSED,
                           receipts=pending.required if outcome is r.Outcome.APPLIED else frozenset())
        with self.assertRaises(r.RotationError) as caught:
            self.apply("record", pending, outcome=r.Outcome.REJECTED,
                       reason="synthetic credential: do not echo")
        self.assertNotIn("synthetic credential", str(caught.exception))

    def test_restored_snapshots_reject_inconsistent_safety_facts(self):
        from dataclasses import replace
        r = self.r
        for changes in (
            {"state": r.Stage.ACTIVE},
            {"state": r.Stage.RETIRED},
            {"state": r.Stage.FAILED},
            {"state": r.Stage.RECOVERY_REQUIRED},
            {"state": r.Stage.PARTIAL},
            {"state": r.Stage.OLD_RESTORED},
            {"state": r.Stage.COMPENSATED},
            {"state": r.Stage.DELIVERING},
            {"original_failure": r.Effect.DELIVER},
            {"compensation_failure": r.Effect.RESTORE_OLD},
            {"state": r.Stage.STAGED, "receipts": self.rotation.required},
        ):
            with self.assertRaises(r.RotationError):
                replace(self.rotation, **changes)
        active = self.active()
        with self.assertRaises(r.RotationError):
            replace(active, receipts=frozenset())
        with self.assertRaises(r.RotationError):
            replace(active, original_failure=r.Effect.RETIRE_OLD)
        failed = self.apply("record", self.apply("begin_delivery"), outcome=r.Outcome.REJECTED)
        with self.assertRaises(r.RotationError):
            replace(failed, original_failure=r.Effect.RESTORE_OLD)

    def test_transition_entry_points_are_finite_for_each_reachable_stage(self):
        r = self.r
        delivering = self.apply("begin_delivery")
        delivered = self.delivered()
        active = self.active()
        retiring = self.apply("begin_retirement", active)
        retired = self.apply("record", retiring, outcome=r.Outcome.APPLIED)
        unknown = self.apply("record", retiring, outcome=r.Outcome.UNKNOWN)
        absent = self.apply("reconcile", unknown, outcome=r.Observation.ABSENT, quiescent=True)
        failed = self.apply("record", retiring, outcome=r.Outcome.REJECTED)
        restoring = self.apply("begin_compensation", failed)
        restored = self.apply("record", restoring, outcome=r.Outcome.APPLIED)
        revoking = self.apply("begin_new_revocation", restored)
        done = self.apply("record", revoking, outcome=r.Outcome.APPLIED)
        partial = self.apply("record", revoking, outcome=r.Outcome.REJECTED)
        uncertain_delivery = self.apply("record", delivering, outcome=r.Outcome.UNKNOWN)
        recovery = self.apply("reconcile", uncertain_delivery, outcome=r.Observation.PARTIAL,
                              receipts=frozenset({"node-a/inc-1"}), quiescent=True)
        entries = ("begin_delivery", "activate", "begin_retirement", "begin_compensation",
                   "begin_new_revocation", "retry_effect")
        for snapshot, allowed in ((self.rotation, "begin_delivery"), (delivering, None),
                                  (delivered, "activate"), (active, "begin_retirement"),
                                  (retiring, None), (retired, None), (unknown, None),
                                  (absent, "retry_effect"), (failed, "begin_compensation"),
                                  (recovery, "begin_compensation"),
                                  (restoring, None), (restored, "begin_new_revocation"),
                                  (revoking, None), (done, None), (partial, None)):
            for method in entries:
                if method == allowed:
                    self.apply(method, snapshot)
                else:
                    with self.assertRaises(r.RotationError):
                        self.apply(method, snapshot)


if __name__ == "__main__":
    with offline():
        unittest.main()
