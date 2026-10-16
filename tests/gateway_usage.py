"""Management panel не конкурирует с единственным usage consumer #656.

HYPOTHESIS: queue и subscriptions отнимают события у ingestion, даже при GET.
SOLUTION: зарезервировать usage source paths, сохранив безопасные key request counts.
RESULT: 36 behavioral red для разрешённого consumption, затем 5/5 файлов green.
STATUS: FIXED — see #656 / #647 (panel policy, не external exclusivity proof)
"""
import unittest
import hermetic  # noqa: F401
from _lib import offline
from mop.server.gateway.policy import validate


class ReservedUsage(unittest.TestCase):
    def test_browser_cannot_pop_queue_or_open_usage_subscriber(self):
        root = '/v8/management/observability/usage'
        for path in (root, root + '/queue', root + '/queue?count=100', root + '/stream',
                     root + '/events', root + '/queue/child'):
            for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(validate(method, path, b''), 403)

    def test_key_request_counters_and_unrelated_panel_reads_are_not_consumption(self):
        for path in ('/v8/management/observability/usage/api-keys',
                     '/v8/management/observability/usage/api-keys?count=5',
                     '/v8/management/config', '/v8/management/logs'):
            self.assertIsNone(validate('GET', path, b''))

    def test_escaped_paths_are_not_a_reserved_source_bypass(self):
        for path in ('/v8/management/observability/%75sage/queue',
                     '/v8/management/observability/usage/%71ueue',
                     '/v8/management/observability/usage/../usage/queue'):
            self.assertEqual(validate('GET', path, b''), 400)


if __name__ == '__main__':
    with offline():
        unittest.main()
