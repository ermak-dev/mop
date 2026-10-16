"""Контракт queue codec для публичного source profile CLIProxyAPI v8.0.16.

HYPOTHESIS: raw counters пересекаются, а queue records содержат credentials.
SOLUTION: только проверенный normalized breakdown и whitelist безопасных полей.
RESULT: отсутствие codec — 9 red; затем отдельный behavioral red для dedupe
при смене key-label salt. Нормализованная сумма/приватность/10 checks green.
STATUS: FIXED — see #656 (codec, не consumer/источник live/CLI cutover)
"""
import copy
import importlib
import json
from pathlib import Path
import unittest
from uuid import UUID

import hermetic  # noqa: F401 — до mop
from _lib import offline


SOURCE = UUID('9195305d-152b-42c0-80c6-edbce57a4ef4')
EXECUTION = '346be44c-1925-4c8f-99e4-8940b8766c84'
KEY = 'sk-synthetic-private-client-656'
SALT = b'usage-identifier-test-salt-32bytes'


def record():
    return {'timestamp': '2026-10-16T02:03:04Z', 'accounting_version': 2,
        'execution_id': EXECUTION, 'provider': 'anthropic', 'executor_type': 'claude',
        'model': 'claude-opus-5-5', 'alias': 'opus', 'api_key': KEY,
        'source': 'upstream-private-key', 'auth_index': 'private-upstream-account',
        'fail': {'status_code': 200, 'body': 'private response body'},
        'response_headers': {'Authorization': ['private-header']},
        'client_ip': '192.0.2.1', 'session_id': 'private-session', 'failed': False,
        'tokens': {'input_tokens': 15, 'output_tokens': 26, 'reasoning_tokens': 6,
            'cached_tokens': 4, 'cache_read_tokens': 4, 'cache_creation_tokens': 1, 'total_tokens': 41},
        'token_breakdown': {'schema_version': 2, 'quality': 'complete', 'total_tokens': 41,
            'unclassified_tokens': 0,
            'input': {'total_tokens': 15, 'uncached_tokens': 10, 'cache_read_tokens': 4, 'cache_write_tokens': 1},
            'output': {'total_tokens': 26, 'non_reasoning_tokens': 20, 'reasoning_tokens': 6}}}


class QueueCodec(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'mop/server/sessions/proxy_usage.py'
        self.assertTrue(path.exists(), 'strict proxy usage codec is not implemented')
        self.c = importlib.import_module('mop.server.sessions.proxy_usage')

    def decode(self, records, **kwargs):
        return self.c.decode_batch(json.dumps(records).encode(), source_id=SOURCE, key_salt=SALT, **kwargs)

    def test_normalized_partition_is_not_double_counted_with_legacy_counters(self):
        batch = self.decode([record()])
        self.assertEqual(len(batch.events), 1)
        event = batch.events[0]
        values = dict(event.counters)
        self.assertEqual(values['total'], 41)
        self.assertEqual(values['input'], 15)
        self.assertEqual(values['output'], 26)
        self.assertEqual(values['input_uncached'] + values['cache_read'] + values['cache_write'], 15)
        self.assertEqual(values['output_non_reasoning'] + values['reasoning'], 26)
        self.assertEqual(event.quality, 'complete')
        self.assertEqual(batch.rejected, 0)
        self.assertFalse(batch.source_complete, 'destructive RAM queue cannot certify a complete source')

    def test_sensitive_queue_fields_are_not_retained_in_records_or_diagnostics(self):
        event = self.decode([record()]).events[0]
        serialized = repr(event) + repr(vars(event))
        for private in (KEY, 'upstream-private-key', 'private-upstream-account',
                        'private response body', 'private-header', '192.0.2.1', 'private-session'):
            self.assertNotIn(private, serialized)
        self.assertNotIn('api_key', vars(event))
        self.assertTrue(event.key_id.startswith('key_'))

    def test_key_pseudonym_is_stable_scoped_and_uses_keyed_material(self):
        first = self.decode([record()]).events[0]
        self.assertEqual(first.key_id, self.decode([record()]).events[0].key_id)
        other = copy.deepcopy(record())
        other['api_key'] = 'another-private-key'
        self.assertNotEqual(first.key_id, self.decode([other]).events[0].key_id)
        other_salt = b'different-identifier-secret-32bytes'
        changed = self.c.decode_batch(json.dumps([record()]).encode(), source_id=SOURCE, key_salt=other_salt)
        self.assertNotEqual(first.key_id, changed.events[0].key_id)

    def test_key_label_rotation_cannot_make_the_same_execution_count_twice(self):
        first = self.decode([record()]).events[0]
        changed = self.c.decode_batch(json.dumps([record()]).encode(), source_id=SOURCE,
            key_salt=b'another-key-label-salt-32bytes-long').events[0]
        self.assertEqual(first.event_id, changed.event_id)
        self.assertNotEqual(first.key_id, changed.key_id)

    def test_malformed_counter_types_and_inconsistent_partitions_are_explicit_gaps(self):
        for bad in (False, -1, 1.5, '15', 2 ** 63):
            value = record()
            value['token_breakdown']['input']['total_tokens'] = bad
            batch = self.decode([value])
            self.assertEqual(batch.events, ())
            self.assertEqual(batch.rejected, 1)
        value = record()
        value['token_breakdown']['total_tokens'] = 99
        self.assertEqual(self.decode([value]).rejected, 1)

    def test_missing_categories_do_not_become_measured_zero(self):
        value = record()
        del value['token_breakdown']['input']['cache_write_tokens']
        batch = self.decode([value])
        self.assertEqual(len(batch.events), 1)
        self.assertIsNone(dict(batch.events[0].counters)['cache_write'])
        self.assertEqual(batch.events[0].quality, 'partial')
        self.assertFalse(batch.source_complete)

    def test_unknown_profile_or_raw_only_counters_are_not_assumed_additive(self):
        value = record()
        del value['token_breakdown']
        batch = self.decode([value])
        self.assertEqual(batch.rejected, 1)
        value = record()
        value['accounting_version'] = 999
        self.assertEqual(self.decode([value]).rejected, 1)

    def test_empty_queue_and_invalid_record_do_not_hide_loss_or_discard_valid_neighbors(self):
        empty = self.decode([])
        self.assertEqual(empty.events, ())
        self.assertFalse(empty.source_complete)
        batch = self.decode(['not an object', record()])
        self.assertEqual(batch.rejected, 1)
        self.assertEqual(len(batch.events), 1)

    def test_duplicate_identity_and_payload_fingerprint_are_separate(self):
        first = self.decode([record()]).events[0]
        changed = record()
        changed['alias'] = 'another-approved-alias'
        second = self.decode([changed]).events[0]
        self.assertEqual(first.event_id, second.event_id)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_bounded_strict_json_rejects_duplicate_keys_and_nonfinite_values(self):
        for body in (b'[{"execution_id":"a","execution_id":"b"}]', b'[NaN]', b'{}', b'['):
            with self.assertRaises(self.c.CodecError) as error:
                self.c.decode_batch(body, source_id=SOURCE, key_salt=SALT)
            self.assertNotIn(KEY, str(error.exception))
        with self.assertRaises(self.c.CodecError):
            self.c.decode_batch(b' ' * (self.c.MAX_BATCH_BYTES + 1), source_id=SOURCE, key_salt=SALT)


if __name__ == '__main__':
    with offline():
        unittest.main()
