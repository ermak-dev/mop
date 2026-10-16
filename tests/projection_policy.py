"""Чистые policy/collector проверки без Django и старого usage collector."""
import asyncio
import importlib.util
import unittest
from unittest.mock import patch
import _lib  # noqa: F401
from mop.server.sessions import projection_policy as policy
from mop.common.state import State

# FEATURE: source I/O ограничен и не держит database transaction.
# HYPOTHESIS: нет bounded source port, отделённого от ORM read queries.
# SPEC: state и size имеют независимые lanes; ошибки источника не становятся []/0.
# RED: collector отсутствовал (2 assertions); safe DB refusal скрывался ExceptionGroup.
# SOLUTION: независимые bounded lanes, typed safe refusal и дренирование siblings.
# RESULT: 6/6 pure checks green; внешние source adapters ещё не связаны root.
# STATUS: FIXED — see #626.
class Policy(unittest.TestCase):
    def test_unknown_and_unavailable_never_free(self):
        self.assertEqual(policy.project_state(None, None, 'stale')['kind'], 'unknown')
        self.assertEqual(policy.project_state(None, None, 'unavailable')['kind'], 'silent')
        with patch('mop.common.state.verdict', return_value=State('busy')) as called:
            self.assertEqual(policy.project_state({'session':'busy 1 1'}, None, 'fresh')['kind'], 'busy')
            called.assert_called_once()

    def test_generic_facts_are_a_finite_typed_whitelist(self):
        for kind, value in [('state', {'history':'secret'}), ('clone', {'dirty':0}),
                            ('size', {'disk_bytes':True}), ('state', {'session':'message text 1 1'})]:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                policy.safe_payload(kind, value)
        self.assertEqual(policy.safe_payload('clone', {'dirty':0,'ahead':0}), {'dirty':0,'ahead':0})

    def collector(self):
        spec = importlib.util.find_spec('mop.server.sessions.source_collector')
        self.assertIsNotNone(spec, 'bounded async source collector port is required')
        from mop.server.sessions.source_collector import SourceCollector
        return SourceCollector

    def test_slow_size_lane_does_not_block_state_and_db_never_wraps_fetch(self):
        collector = self.collector()
        async def scenario():
            gate, started = asyncio.Event(), asyncio.Event()
            active = 0
            maximum = 0
            in_database = False
            calls = []
            class Source:
                async def read(self, kind, ticket):
                    nonlocal active, maximum
                    self_assert = not in_database
                    self_outer.assertTrue(self_assert, 'source I/O must be outside database callbacks')
                    active += 1
                    maximum = max(maximum, active)
                    try:
                        if kind == 'size':
                            started.set()
                            await gate.wait()
                        return ticket
                    finally:
                        active -= 1
            async def database(function, *args):
                nonlocal in_database
                in_database = True
                try:
                    return function(*args)
                finally:
                    in_database = False
            class Sink:
                def observed(self, kind, ticket, value):
                    self_outer.assertTrue(in_database)
                    calls.append((kind, value))
                def failed(self, kind, ticket, reason):
                    calls.append((kind, reason))
            self_outer = self
            instance = collector(source=Source(), sink=Sink(), database=database, concurrency=2, timeout=1)
            slow = asyncio.create_task(instance.collect('size', (1,2,3)))
            await started.wait()
            await instance.collect('state', (4,5,6))
            self.assertEqual([value for kind,value in calls if kind == 'state'], [4,5,6])
            self.assertFalse(slow.done())
            self.assertLessEqual(maximum, 4)
            gate.set()
            await slow
        asyncio.run(scenario())

    def test_known_safe_source_refusal_is_not_sanitized_a_second_time(self):
        from mop.server.sessions import source_collector
        self.assertTrue(hasattr(source_collector, 'SourceRefusal'), 'typed safe source refusal port is required')
        async def scenario():
            safe = source_collector.SourceRefusal('source protocol version is not supported')
            outer = self
            class Source:
                async def read(self, kind, ticket):
                    raise safe
            class Sink:
                def observed(self, *args):
                    outer.fail('refusal is not a fact')
                def failed(self, *args):
                    outer.fail('safe library refusal must not be replaced by a generic raw-source error')
            async def database(function, *args):
                return function(*args)
            with self.assertRaises(type(safe)) as caught:
                await self.collector()(source=Source(), sink=Sink(), database=database).collect('state', (1,))
            self.assertIs(caught.exception, safe)
        asyncio.run(scenario())

    def test_safe_database_refusal_preserves_its_reason_and_drains_workers(self):
        collector = self.collector()
        async def scenario():
            safe = ValueError('projection publication refused: producer changed')
            class Source:
                async def read(self, kind, ticket):
                    return ticket
            class Sink:
                def observed(self, *args):
                    raise safe
                def failed(self, *args):
                    self.fail('a database refusal must not become a source failure')
            async def database(function, *args):
                return function(*args)
            instance = collector(source=Source(), sink=Sink(), database=database)
            with self.assertRaises(ValueError) as caught:
                await instance.collect('state', (1,2))
            self.assertIs(caught.exception, safe)
        asyncio.run(scenario())

    def test_source_failure_is_explicit_sanitized_and_not_empty_success(self):
        collector = self.collector()
        async def scenario():
            seen = []
            class Source:
                async def read(self, kind, ticket):
                    raise RuntimeError('credential=private')
            class Sink:
                def observed(self, *args):
                    self.fail('failure must not be recorded as fresh empty facts')
                def failed(self, kind, ticket, reason):
                    seen.append(reason)
            async def database(function, *args):
                return function(*args)
            await collector(source=Source(), sink=Sink(), database=database).collect('state', (1,))
            self.assertEqual(seen, ['source_unavailable'])
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
