"""#656: один public DTO для CLI/stat и dashboard, без transcript/NATS fallback.

HYPOTHESIS: клиентский повторный total смешивает parents и children/unknown.
SPEC: только total counter для grand total, nullable counts и ограниченные pages.
RED: 6 missing-adapter assertions до реализации.
SOLUTION: bounded callable query adapter, строгие typed DTO/coverage и parent totals.
RESULT: 6 checks green, без ORM/HTTP/NATS/transcript reads.
STATUS: FIXED — see #656 (bind HTTPS client в #642).
"""
import copy
import importlib
from pathlib import Path
import unittest
from _lib import offline
from _control_fixture import page  # noqa: F401 -- общая страница #656 (#675)
import hermetic  # noqa: F401


class UsageQuery(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'mop/client/usage.py').exists(),
                        'public usage/stat DTO adapter is missing')
        self.s = importlib.import_module('mop.client.usage')

    def test_stat_preserves_unknown_categories_and_sums_only_total_parent(self):
        result = self.s.stat(lambda **kwargs: page(), start='2026-10-16', end='2026-10-17')
        self.assertEqual(result['groups'][0]['counters']['total']['value'], '41')
        self.assertIsNone(result['groups'][0]['counters']['cache_read']['value'])
        self.assertEqual(result['groups'][0]['counters']['cache_read']['unknown_events'], 1)
        self.assertFalse(result['source_complete'])

    def test_empty_source_is_not_a_measured_zero_day(self):
        value = page()
        value['items'] = []
        result = self.s.stat(lambda **kwargs: value, start='2026-10-16', end='2026-10-17')
        self.assertEqual(result['groups'], [])
        self.assertFalse(result['source_complete'])

    def test_unsupported_attribution_never_falls_back_to_owner_or_transcript(self):
        with self.assertRaises(self.s.UsageQueryError):
            self.s.stat(lambda **kwargs: self.fail('must not query unsupported grouping'),
                        start='2026-10-16', end='2026-10-17', group_by='user')

    def test_bounded_pages_and_repeated_cursor_are_errors_not_partial_success(self):
        value = page()
        value['next'] = '1'*32
        with self.assertRaises(self.s.UsageQueryError):
            self.s.stat(lambda **kwargs: value, start='2026-10-16', end='2026-10-17', max_pages=2)

    def test_invalid_version_partial_coverage_or_period_cannot_be_silently_coerced(self):
        for mutation in ('version', 'period', 'negative', 'missing', 'bool'):
            value = page()
            if mutation == 'version':
                value['schema_version'] = 2
            elif mutation == 'period':
                value['period']['timezone'] = 'local'
            elif mutation == 'negative':
                value['items'][0]['counters']['total']['value'] = '-1'
            elif mutation == 'bool':
                value['items'][0]['counters']['total']['known_events'] = True
            else:
                del value['items'][0]['counters']['total']
            with self.subTest(mutation=mutation), self.assertRaises(self.s.UsageQueryError):
                self.s.stat(lambda **kwargs: value, start='2026-10-16', end='2026-10-17')

    def test_source_metadata_is_allowlisted_and_missing_cursor_is_not_end_of_data(self):
        value = page()
        value['sources'] = [{'id': '9195305d-152b-42c0-80c6-edbce57a4ef4',
                             'source_complete': False, 'api_key': 'must-not-return-this'}]
        with self.assertRaises(self.s.UsageQueryError):
            self.s.stat(lambda **kwargs: value, start='2026-10-16', end='2026-10-17')
        value = page()
        del value['next']
        with self.assertRaises(self.s.UsageQueryError):
            self.s.stat(lambda **kwargs: value, start='2026-10-16', end='2026-10-17')

    def test_key_and_model_grouping_do_not_merge_unrelated_dimensions(self):
        value = page()
        second = copy.deepcopy(value['items'][0])
        second['id'] = '1195305d-152b-42c0-80c6-edbce57a4ef4'
        second['model'] = 'other-model'
        value['items'].append(second)
        result = self.s.stat(lambda **kwargs: value, start='2026-10-16', end='2026-10-17', group_by='model')
        self.assertEqual(len(result['groups']), 2)


if __name__ == '__main__':
    with offline():
        unittest.main()
