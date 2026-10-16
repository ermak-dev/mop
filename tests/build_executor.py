"""Привилегированный протокол и ограниченный Ansible runner, без настоящего PVE."""
import _lib
import hermetic  # noqa: F401
from pathlib import Path
import tempfile
import sys
import unittest


# FEATURE: privileged executor принимает только durable ID, не shell/argv.
# HYPOTHESIS: ordinary worker не должен получать inventory/key или node authority.
# SOLUTION: scoped Unix protocol, root claim gate, private redacted bounded runner.
# RESULT: реальные subprocess, timeout/process group и private invocation checks.
# STATUS: FIXED — see #648; отдельная OS UID fixture исполняется в Docker SETUP.
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

    def test_pve_adapter_checks_external_result_instead_of_trusting_exit_zero(self):
        from mop.server.builds.adapters import AnsibleImageBuilder
        from mop.server.builds.contracts import StageResult, ImageAvailability
        from contextlib import contextmanager
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from uuid import uuid4
        node = uuid4()
        build = SimpleNamespace(target_nodes=(node,), source_digest='a'*64)
        ticket = SimpleNamespace(stage='build', build=build)
        @contextmanager
        def factory(build):
            yield object()
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(outcome='succeeded', quiescent=True, reason='exit zero'))
        adapter = AnsibleImageBuilder(invocations=factory, runner=runner,
            availability=lambda b, n: ImageAvailability(n, 'b'*64, True, datetime.now(timezone.utc), 'template'),
            inspector=lambda ticket: StageResult('unknown', False, 'readback unavailable'))
        self.assertEqual(adapter.apply(ticket).outcome, 'unknown')

    def test_pve_factory_pins_commit_and_rebuild_in_private_files_not_request_argv(self):
        from mop.server.builds.adapters import PveAnsibleInvocations
        from mop.server.builds.contracts import BuildInput
        import hashlib
        import json
        from uuid import uuid4
        node = uuid4()
        manifest = b'[{"vars":{"MOP_CORES":2,"app_mode":"test"},"tasks":[]}]'
        build = BuildInput(uuid4(), uuid4(), 'owned', 'https://example.invalid/owned.git', uuid4(),
            manifest, hashlib.sha256(manifest).hexdigest(), 'feature/image', 'a'*40, 'b'*64, 'rebuild', (node,), False)
        with tempfile.TemporaryDirectory() as directory:
            factory = PveAnsibleInvocations(executable='/usr/bin/true', inventory='/fixture/inventory',
                playbook='/fixture/build.yml', workspace=directory, settings={}, node_names={node:'owned'})
            with factory(build) as invocation:
                self.assertNotIn('a'*40, invocation.argv)
                private_files = [arg[1:] for arg in invocation.argv if arg.startswith('@')]
                value = json.loads(Path(private_files[-1]).read_text())
                self.assertEqual(value['mop_image_commit'], 'a'*40)
                self.assertEqual(value['mop_image_branch'], 'feature/image')
                self.assertIs(value['mop_fresh'], True)
                self.assertEqual(value['mop_project_asks'], {'MOP_CORES':2})
                self.assertEqual(Path(private_files[-1]).stat().st_mode & 0o777, 0o600)

    def test_real_build_budget_can_reach_two_hours_but_never_be_unbounded(self):
        from mop.server.builds.subprocess import SafeAnsibleSubprocess, TrustedInvocation
        with tempfile.TemporaryDirectory() as directory:
            runner = SafeAnsibleSubprocess(private_directory=directory, known_secrets=())
            invocation = TrustedInvocation((sys.executable, '-I', '-c', 'print("bounded")'), directory, ())
            self.assertEqual(runner.run(invocation, timeout=7200).outcome, 'succeeded')
            with self.assertRaises(ValueError):
                runner.run(invocation, timeout=7201)

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
