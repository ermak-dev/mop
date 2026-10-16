"""Привилегированный протокол и ограниченный Ansible runner, без настоящего PVE."""
import _lib
import hermetic  # noqa: F401
from pathlib import Path
import tempfile
import sys
import unittest


# FEATURE: privileged executor принимает только durable ID, не shell/argv.
# HYPOTHESIS: ordinary worker не должен получать inventory/key или node authority.
class ExecutorContract(unittest.TestCase):
    def test_protocol_rejects_arbitrary_argv_and_requires_all_fences(self):
        from mop.server.builds.executor import decode_request
        from uuid import uuid4
        import json
        request = dict(version=1, intent_id=str(uuid4()), step_id=str(uuid4()), token=str(uuid4()), epoch=str(uuid4()), verb='apply')
        self.assertEqual(decode_request(json.dumps(request).encode()).verb, 'apply')
        for key in ('argv', 'node_id', 'shell', 'inventory', 'owner_id'):
            with self.assertRaises(ValueError):
                decode_request(json.dumps({**request, key:'untrusted'}).encode())
        with self.assertRaises(ValueError):
            decode_request(b'x'*2049)

    def test_stdout_and_private_logs_bounded_redacted_even_across_chunks(self):
        from mop.server.builds.subprocess import SafeAnsibleSubprocess, TrustedInvocation
        with tempfile.TemporaryDirectory() as directory:
            code = "import os; os.write(1,b'x'*70000+b'secr'); os.write(1,b'et-value\\n'+b'y'*70000)"
            runner = SafeAnsibleSubprocess(private_directory=directory, known_secrets=('secret-value',), max_log_bytes=4096)
            result = runner.run(TrustedInvocation((sys.executable, '-I', '-c', code), directory, (('PATH','/usr/bin:/bin'),)), timeout=5)
            self.assertEqual(result.outcome, 'succeeded')
            self.assertTrue(result.quiescent)
            self.assertLessEqual(len(result.tail.encode()), 4096)
            for path in Path(directory).glob('*.log'):
                self.assertLessEqual(path.stat().st_size, 4096)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(b'secret-value', path.read_bytes())

    def test_timeout_kills_owned_group_and_retains_failure_not_fake_success(self):
        from mop.server.builds.subprocess import SafeAnsibleSubprocess, TrustedInvocation
        with tempfile.TemporaryDirectory() as directory:
            runner = SafeAnsibleSubprocess(private_directory=directory, known_secrets=())
            result = runner.run(TrustedInvocation((sys.executable, '-I', '-c', 'import time; time.sleep(30)'), directory, ()), timeout=.15)
            self.assertNotEqual(result.outcome, 'succeeded')
            self.assertTrue(result.quiescent)
            self.assertIn('timeout', result.reason)


if __name__ == '__main__':
    unittest.main()
