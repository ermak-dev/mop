#!/usr/bin/env python3
"""Сброс клона исполнителем узла (#625).

HYPOTHESIS: действия wipe_clone нет ни в протоколе узла, ни в LocalExecutor.
SPEC: снос только после доказанной тишины сессии от доверенного порта тела и
свежей перепроверки допуска перед самим сносом; живая или недоказанная тишина —
failed-before-effect без эффекта; неподтверждённый снос — unknown, не успех;
без порта тела — отказ до эффекта; исполнитель сам не шеллит в обход порта.
RED: протокол узла не знал wipe_clone (Work.parse отказывал).
SOLUTION: BodyPort quiescent/wipe, ветка wipe_clone в LocalExecutor.
RESULT: 6/6.
STATUS: FIXED — see #625
"""
import unittest
from pathlib import Path

import _lib  # noqa: F401 -- общий временный корень процесса (#338)
import hermetic  # noqa: F401
from _body_fixture import wire
from mop.node.http_execution import ACTIONS, EVIDENCE, Work, Refused, Unavailable, EffectGuard
from mop.node.execution_adapters import BoundTarget, ActionInput, LocalExecutor


def bound(work):
    root = Path('/tmp/mop-wipe-fake-binding')
    return BoundTarget(work.target_id, work.incarnation_id, work.node_id, work.executor_epoch,
        work.generation, work.role, work.bundle_digest, work.grant_id, root, root/'tmux', '%0', root/'launcher')


class Body:
    def __init__(self, *, quiet=True, wiped=True):
        self.quiet, self.wiped, self.calls = quiet, wiped, []

    async def quiescent(self, work, target):
        self.calls.append('quiescent')
        return self.quiet

    async def wipe(self, work, target):
        self.calls.append('wipe')
        return self.wiped


class NoShell:
    async def run(self, *args, **kwargs):
        raise AssertionError('wipe must go through the body port, not a local shell')

    async def aclose(self):
        pass


class NodeWipe(unittest.IsolatedAsyncioTestCase):
    def executor(self, work, body):
        target = bound(work)

        class Resolver:
            async def resolve(self, value):
                return target, ActionInput('Owner', 'owner@example.test')

            async def reconcile(self, *args):
                return True
        return LocalExecutor(NoShell(), Resolver(), body=body)

    def guard(self, log, *, refuse_at=None):
        async def authorize():
            log.append('gate')
            if refuse_at is not None and log.count('gate') == refuse_at:
                raise Refused('generation changed')
        return EffectGuard(authorize, lambda: None)

    def test_protocol_names_wipe_clone_and_its_confirmation(self):
        self.assertIn('wipe_clone', ACTIONS)
        self.assertIn('clone-wiped', EVIDENCE)
        self.assertEqual(Work.parse(wire(action='wipe_clone')).action, 'wipe_clone')

    async def test_quiet_body_is_wiped_after_a_fresh_admission_recheck(self):
        work, body, log = Work.parse(wire(action='wipe_clone')), Body(), []
        executor = self.executor(work, body)
        guard = self.guard(log)
        original = body.wipe

        async def wipe(work, target):
            log.append('wipe')
            return await original(work, target)
        body.wipe = wipe
        self.assertEqual(await executor.execute(work, guard), ('succeeded', 'clone-wiped'))
        self.assertEqual(body.calls, ['quiescent', 'wipe'])
        # Последняя перепроверка допуска стоит прямо перед сносом.
        self.assertEqual(log[-2:], ['gate', 'wipe'])
        await executor.aclose()

    async def test_live_or_unproven_quiet_refuses_before_any_destruction(self):
        for quiet in (False, None, 'yes', 1):
            work, body = Work.parse(wire(action='wipe_clone')), Body(quiet=quiet)
            executor = self.executor(work, body)
            self.assertEqual(await executor.execute(work, self.guard([])), ('failed', 'failed-before-effect'), quiet)
            self.assertEqual(body.calls, ['quiescent'], quiet)
            await executor.aclose()

    async def test_unconfirmed_wipe_is_unknown_not_success(self):
        for wiped in (False, None, 'done'):
            work, body = Work.parse(wire(action='wipe_clone')), Body(wiped=wiped)
            executor = self.executor(work, body)
            self.assertEqual(await executor.execute(work, self.guard([])), ('unknown', 'effect-running'), wiped)
            await executor.aclose()

    async def test_missing_body_port_refuses_before_effect(self):
        work = Work.parse(wire(action='wipe_clone'))
        executor = self.executor(work, None)
        with self.assertRaises(Unavailable):
            await executor.execute(work, self.guard([]))
        await executor.aclose()

    async def test_revocation_after_quiescence_stops_the_wipe(self):
        work, body, log = Work.parse(wire(action='wipe_clone')), Body(), []
        executor = self.executor(work, body)
        with self.assertRaisesRegex(Refused, 'generation changed'):
            await executor.execute(work, self.guard(log, refuse_at=2))
        self.assertEqual(body.calls, ['quiescent'])
        await executor.aclose()


if __name__ == '__main__':
    unittest.main()
