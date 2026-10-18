"""Body-only standby management: private input не является полномочиями #748.

HYPOTHESIS: eager Management требует active grant раньше native readiness;
пустой SDK startup разрывает цикл, но файл сам не доказывает Agent authority.
SPEC: typed expected runtime + private files + verified TLS + live catalogue;
единственный сериализованный владелец I/O, без replay и private inbox.
SOLUTION: explicit bounded handoff + live Management, один owner для SDK requests.
RESULT: исходный RED — отсутствующие CLI flags/DTO; дополнительные RED выявили
atime, late recovery после cancel, повторный HTTP budget, пустой selector и
потерю safe причины отказа. 17 проверок SDK/TLS/lifecycle проходят в strict mode.
STATUS: FIXED — see #748
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID

import hermetic  # noqa: F401
from _control_fixture import own_tls, NODE, PROJECT, KEY
from mop.client import control as api
from mop.common import management_context as dto

from _body_mcp_fixture import (ROOT, TOKEN, expiry, request, context, bootstrap,
    publish, catalogue, TLSCatalogue, LostReply, stop_arguments, Stdio)


class BodyStandby(unittest.TestCase):
    def test_sdk_empty_before_handoff_actual_catalogue_notification_and_live_revoke(self):
        with own_tls(TLSCatalogue) as (server, origin, cert, directory):
            server.calls, server.catalogue = [], catalogue()
            client = Stdio(self, directory)
            start = time.monotonic(); client.initialize()
            self.assertLess(time.monotonic()-start, 3)
            self.assertEqual(client.rpc(2, 'tools/list')['tools'], [])
            reply = client.rpc(3, 'tools/call', dict(name='project_list', arguments={'private': TOKEN}))
            self.assertTrue(reply['isError']); self.assertIn('unavailable', reply['content'][0]['text'])
            self.assertEqual(server.calls, [])
            publish(client.boot, bootstrap(origin, cert.read_text()))
            self.assertEqual(client.rpc(4, 'tools/list')['tools'], [])
            publish(client.secret, context())
            client.changed()
            tools = client.rpc(5, 'tools/list')['tools']
            self.assertIn('project_list', {tool['name'] for tool in tools})
            self.assertTrue(all(auth == 'Token '+TOKEN for _, auth in server.calls))
            reply = client.rpc(6, 'tools/call', dict(name='project_list'))
            self.assertFalse(reply.get('isError', False), reply)
            count = sum('/projects?' in path for path, _ in server.calls)
            server.catalogue['actions'] = []
            self.assertNotIn('project_list', {tool['name'] for tool in client.rpc(7, 'tools/list')['tools']})
            reply = client.rpc(8, 'tools/call', dict(name='project_list'))
            self.assertTrue(reply['isError'])
            self.assertEqual(sum('/projects?' in path for path, _ in server.calls), count)
            server.catalogue['actor']['user_id'] = 8
            self.assertEqual(client.rpc(9, 'tools/list')['tools'], [])
            server.catalogue = catalogue()
            self.assertEqual(client.rpc(10, 'tools/list')['tools'], [], 'identity change must not auto-rebind')
            client.close()

    def test_lost_effect_response_is_unknown_and_never_reposts(self):
        with own_tls(LostReply) as (server, origin, cert, directory):
            server.calls, server.catalogue = [], catalogue()
            server.catalogue['actions'] = ['operations.stop_puppet_job', 'operations.view_operation']
            client = Stdio(self, directory)
            client.initialize()
            publish(client.boot, bootstrap(origin, cert.read_text())); publish(client.secret, context())
            client.changed()
            reply = client.rpc(2, 'tools/call', dict(name='stop', arguments=stop_arguments()))
            self.assertFalse(reply.get('isError', False), reply)
            text = reply['content'][0]['text']
            self.assertIn('submission unknown', text); self.assertIn(KEY, text)
            self.assertEqual(sum('/commands/stop-job' in path for path, _ in server.calls), 1)
            self.assertEqual(sum('/operations' in path for path, _ in server.calls), 1)
            client.close()

    def test_invalid_published_handoff_exits_with_safe_reason_not_standby_success(self):
        with own_tls(TLSCatalogue) as (server, origin, cert, directory):
            server.calls, server.catalogue = [], catalogue()
            client = Stdio(self, directory)
            client.initialize()
            publish(client.boot, bootstrap(origin, cert.read_text()))
            publish(client.secret, dict(context(), installation_id='foreign-install'))
            try:
                client.process.wait(timeout=3)
                self.assertNotEqual(client.process.returncode, 0)
                errors = client.process.stderr.read()
                self.assertIn('invalid body management binding', errors)
                self.assertNotIn(TOKEN, errors)
                self.assertEqual(server.calls, [])
            finally:
                if client.process.poll() is None: client.process.kill(); client.process.wait(timeout=3)
                client.process.stdin.close(); client.reader.join(2)
                client.process.stdout.close(); client.process.stderr.close()

    def test_missing_handoff_deadline_is_failure_not_fake_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Stdio(self, directory, deadline=expiry(1))
            client.initialize()
            client.process.wait(timeout=5)
            self.assertNotEqual(client.process.returncode, 0)
            self.assertNotIn('ready', client.process.stderr.read().lower())
            # Cleanup этого ожидаемого failure не требует success code.
            client.process.stdin.close(); client.reader.join(2)
            client.process.stdout.close(); client.process.stderr.close()

    def test_eof_during_real_stalled_https_joins_within_http_bound_and_cleans_ca(self):
        entered, release = threading.Event(), threading.Event()
        class Stalled(TLSCatalogue):
            def do_GET(self):
                entered.set()
                release.wait(12)
                try: super().do_GET()
                except OSError: pass  # Клиент уже закрыл bounded TLS request.
        root = Path(tempfile.gettempdir())
        before = set(root.glob('mop-body-ca-*'))
        with own_tls(Stalled) as (server, origin, cert, directory):
            server.calls, server.catalogue = [], catalogue()
            client = Stdio(self, directory)
            client.initialize()
            publish(client.boot, bootstrap(origin, cert.read_text())); publish(client.secret, context())
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(set(root.glob('mop-body-ca-*'))-before)
                start = time.monotonic()
                client.close()
                self.assertLess(time.monotonic()-start, 11.5)
                self.assertFalse(set(root.glob('mop-body-ca-*'))-before)
            finally: release.set()

    def test_body_flags_cannot_mix_ordinary_selectors(self):
        from mop.cli import _control
        from _lib import run_command
        flags = ['--body-standby', '--body-bootstrap', '/private/boot', '--body-context', '/private/context',
            '--body-deadline', expiry()]
        from mop.cli import _mcp
        with patch.object(_mcp, 'serve_body', return_value=0) as serve:
            for extra in (['--check'], ['--project-id', PROJECT], ['--node-id', NODE], ['--client-id', KEY],
                    ['--project-id', '']):
                out, err, code = run_command(lambda argv: _control.invoke('mcp', argv), [*flags, *extra])
                self.assertEqual(code, 2)
                self.assertIn('body mode', err)
                self.assertEqual(out, '')
            serve.assert_not_called()

    def test_body_global_trust_partial_flags_and_unbounded_deadline_are_refused(self):
        from mop import cli
        from mop.cli import _control
        from _lib import run_command
        flags = ['--body-standby', '--body-bootstrap', '/private/boot', '--body-context', '/private/context',
            '--body-deadline', expiry()]
        for prefix in (['--api-origin', 'https://other.invalid'], ['--api-ca', '/never-read']):
            out, err, code = run_command(cli.main, [*prefix, 'mcp', *flags])
            self.assertEqual(code, 2); self.assertEqual(out, ''); self.assertIn('body mode', err)
        for partial in (['--body-standby'], flags[1:], flags[:-2]):
            _, _, code = run_command(lambda argv: _control.invoke('mcp', argv), partial)
            self.assertEqual(code, 2)
        for deadline in (expiry(-1), expiry(301), 'not-a-deadline', '2026-10-19T12:00:00'):
            _, err, code = run_command(lambda argv: _control.invoke('mcp', argv), [*flags[:-1], deadline])
            self.assertEqual(code, 1); self.assertIn('deadline', err)


class BootstrapContract(unittest.TestCase):
    def body_type(self):
        self.assertTrue(hasattr(dto, 'BodyBootstrap'), 'finite common body bootstrap DTO is missing')
        return dto.BodyBootstrap

    def test_common_dto_is_finite_and_hides_transport(self):
        with own_tls(TLSCatalogue) as (_, origin, cert, _):
            body = self.body_type().parse(bootstrap(origin, cert.read_text()))
            self.assertEqual(body.request, dto.ManagementRequest.parse(request()))
            self.assertEqual(body.project_id, UUID(PROJECT))
            self.assertNotIn(cert.read_text(), repr(body))
            self.assertNotIn(origin, repr(body))
            self.assertEqual(body.wire()['ca_pem'], cert.read_text())

    def test_extra_authority_paths_bad_origin_pem_uid_expiry_fail_closed(self):
        with own_tls(TLSCatalogue) as (_, origin, cert, _):
            original = bootstrap(origin, cert.read_text())
            variants = [dict(original, ca_file='/etc/passwd'), dict(original, permissions=['all']),
                dict(original, origin='http://localhost'), dict(original, origin=origin+'/api'),
                dict(original, origin='https://user:password@localhost'), dict(original, ca_pem='not PEM'),
                dict(original, ca_pem='x'*32769), dict(original, body_uid=True),
                dict(original, body_uid=-1), dict(original, expires_at=expiry(-1))]
            cls = self.body_type()
            for value in variants:
                with self.subTest(keys=set(value)), self.assertRaises(ValueError): cls.parse(value)


class PrivateInputs(unittest.TestCase):
    def test_catalogue_bind_shares_one_http_budget_instead_of_two_full_waits(self):
        from mop.client import body_management as body
        from mop.cli._mcp import Management
        class Slow(TLSCatalogue):
            def do_GET(self):
                time.sleep(.13)
                try: super().do_GET()
                except OSError: pass  # Клиент уже закрыл bounded TLS request.
        with own_tls(Slow) as (server, origin, cert, directory):
            server.calls, server.catalogue = [], catalogue()
            boot, secret = Path(directory)/'boot', Path(directory)/'secret'
            publish(boot, bootstrap(origin, cert.read_text())); publish(secret, context())
            limits = api.Limits(connect_timeout=.1, read_timeout=.2, request_timeout=.2, wait_timeout=.2)
            with patch.object(body, 'BODY_LIMITS', limits):
                manager = body.BodyManagement(boot, secret, expiry(), Management)
                try:
                    self.assertEqual(manager.tools(), [], 'initial catalogue plus refresh share one bound')
                    self.assertEqual(manager.state, 'failed')
                    self.assertIsNone(manager.private_ca)
                finally: manager.close()

    def test_old_valid_file_read_is_not_confused_with_atime_mutation(self):
        from mop.client.body_management import BodyFiles
        with tempfile.TemporaryDirectory() as directory:
            boot, secret = Path(directory)/'boot', Path(directory)/'secret'
            publish(boot, {'one': 1}); publish(secret, {'two': 2})
            os.utime(boot, (1, 2)); os.utime(secret, (1, 2))
            self.assertEqual(BodyFiles(boot, secret).read(), [{'one': 1}, {'two': 2}])

    def test_private_single_link_bounded_no_follow_identity_files(self):
        from mop.client.body_management import BodyFiles
        for defect in ('mode', 'directory_mode', 'hardlink', 'symlink', 'fifo', 'oversize',
                'duplicate', 'replacement', 'disappearance', 'directory_symlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)/'private'; parent.mkdir(mode=0o700)
                boot, secret = parent/'boot', parent/'secret'
                publish(boot, {'one': 1}); publish(secret, {'two': 2})
                files = BodyFiles(boot, secret)
                if defect == 'mode': secret.chmod(0o644)
                elif defect == 'directory_mode': parent.chmod(0o755)
                elif defect == 'hardlink': os.link(secret, parent/'alias')
                elif defect == 'symlink':
                    secret.unlink(); secret.symlink_to(boot)
                elif defect == 'fifo':
                    secret.unlink(); os.mkfifo(secret, 0o600)
                elif defect == 'oversize': secret.write_bytes(b'x'*16385)
                elif defect == 'duplicate': secret.write_text('{"one":1,"one":2}')
                elif defect in ('replacement', 'disappearance'):
                    files.read(); secret.unlink()
                    if defect == 'replacement': publish(secret, {'two': 2})
                elif defect == 'directory_symlink':
                    alias = Path(directory)/'alias'; alias.symlink_to(parent, target_is_directory=True)
                    files = BodyFiles(alias/'boot', alias/'secret')
                with self.assertRaises(api.ClientError): files.read()

    def test_foreign_runtime_binding_transport_or_catalogue_never_authorizes(self):
        from mop.client.body_management import BodyManagement
        from mop.cli._mcp import Management
        with own_tls(TLSCatalogue) as (server, origin, cert, directory):
            with own_tls(TLSCatalogue) as (_, _, foreign_ca, _):
                for defect in ('runtime', 'install', 'restore', 'project', 'grant', 'expired', 'uid',
                        'token', 'human', 'service', 'subject', 'epoch', 'actor_grant', 'scope', 'ca'):
                    with self.subTest(defect=defect), tempfile.TemporaryDirectory(dir=directory) as private:
                        boot, secret = Path(private)/'boot', Path(private)/'secret'
                        b, c, data = bootstrap(origin, cert.read_text()), context(), catalogue()
                        server.calls, server.catalogue = [], data
                        if defect == 'runtime': c['request']['scope']['session_id'] = 'another-session'
                        elif defect == 'install': c['installation_id'] = 'foreign-install'
                        elif defect == 'restore': c['restore_generation'] = NODE
                        elif defect == 'project': c['project_id'] = NODE
                        elif defect == 'grant': c['grant_id'] = NODE
                        elif defect == 'expired': c['expires_at'] = expiry(-1)
                        elif defect == 'uid': b['body_uid'] = os.geteuid()+1
                        elif defect == 'token': c['token'] = 'e'*40
                        elif defect == 'human': data['actor'].update(kind='human', human_owner_id=7, grant_id=None)
                        elif defect == 'service': data['actor'].update(kind='service', human_owner_id=None, grant_id=None)
                        elif defect == 'subject': data['actor']['user_id'] = 8
                        elif defect == 'epoch': data['epoch'] = NODE
                        elif defect == 'actor_grant': data['actor']['grant_id'] = NODE
                        elif defect == 'scope': data['scope']['project_id'] = NODE
                        elif defect == 'ca': b['ca_pem'] = foreign_ca.read_text()
                        publish(boot, b); publish(secret, c)
                        manager = BodyManagement(boot, secret, expiry(), Management)
                        try:
                            self.assertEqual(manager.tools(), [])
                            self.assertEqual(manager.state, 'failed')
                            self.assertIsNone(manager.private_ca)
                            reply = manager.call('project_list', {'private': TOKEN})
                            self.assertTrue(reply.model_dump(by_alias=True)['isError'])
                            self.assertNotIn(TOKEN, str(reply))
                            if defect in ('runtime', 'install', 'restore', 'project', 'grant', 'expired', 'uid'):
                                self.assertIn('invalid body management binding', reply.content[0].text,
                                    'safe local refusal reason must survive the lifecycle wrapper')
                            if defect in ('runtime', 'install', 'restore', 'project', 'grant', 'expired', 'uid', 'ca'):
                                self.assertEqual(server.calls, [])
                        finally: manager.close()

    def test_expiry_file_tamper_and_revoke_remove_binding_and_private_ca(self):
        from mop.client.body_management import BodyManagement
        from mop.cli._mcp import Management
        with own_tls(TLSCatalogue) as (server, origin, cert, directory):
            for defect in ('expiry', 'file', 'revoke'):
                with self.subTest(defect=defect), tempfile.TemporaryDirectory(dir=directory) as private:
                    boot, secret = Path(private)/'boot', Path(private)/'secret'
                    secret_value = context()
                    if defect == 'expiry': secret_value['expires_at'] = expiry(1)
                    publish(boot, bootstrap(origin, cert.read_text())); publish(secret, secret_value)
                    server.calls, server.catalogue = [], catalogue()
                    manager = BodyManagement(boot, secret, expiry(), Management)
                    try:
                        self.assertIn('project_list', {t.name for t in manager.tools()})
                        ca = Path(manager.private_ca.name)
                        self.assertEqual((ca/'ca.pem').stat().st_mode & 0o777, 0o600)
                        if defect == 'expiry':
                            time.sleep(max(0, (manager.context.expires_at-datetime.now(timezone.utc)).total_seconds())+.01)
                        elif defect == 'file': publish(secret, context())
                        elif defect == 'revoke': server.catalogue['actor']['grant_id'] = NODE
                        self.assertEqual(manager.tools(), [])
                        self.assertFalse(ca.exists())
                        server.catalogue = catalogue()
                        self.assertEqual(manager.tools(), [], 'failed binding never auto-rebinds')
                    finally: manager.close()

    def test_body_imports_do_not_load_legacy_authority(self):
        script = '''import sys
from mop.client import body_management
from mop.cli import _mcp
_mcp.commandlets()
for name in ('mop.common.bus', 'mop.client.channel', 'mop.common.context',
             'mop.common.soul', 'mop.common.busnames', 'mop.session', 'mop.node'):
    assert name not in sys.modules, name
'''
        result = subprocess.run([sys.executable, '-P', '-c', script], cwd=ROOT,
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')


class OwnerCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_real_post_joins_io_without_late_recovery_or_replay(self):
        from mop.client.body_management import BodyManagement, SerializedOwner
        from mop.cli._mcp import Management
        with own_tls(LostReply) as (server, origin, cert, directory):
            server.calls, server.catalogue = [], catalogue()
            server.catalogue['actions'] = ['operations.stop_puppet_job', 'operations.view_operation']
            server.post_entered, server.release = threading.Event(), threading.Event()
            boot, secret = Path(directory)/'boot', Path(directory)/'secret'
            publish(boot, bootstrap(origin, cert.read_text())); publish(secret, context())
            manager = BodyManagement(boot, secret, expiry(), Management)
            owner = SerializedOwner(manager)
            await owner.request('tools')
            ca = Path(manager.private_ca.name)
            task = asyncio.create_task(owner.request('call', 'stop', stop_arguments()))
            self.assertTrue(await asyncio.to_thread(server.post_entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            closing = asyncio.create_task(owner.aclose())
            await asyncio.sleep(0)
            self.assertTrue(ca.exists(), 'owner must retain trust until the active HTTP finishes')
            server.release.set()
            await asyncio.wait_for(closing, 3)
            self.assertFalse(ca.exists())
            self.assertEqual(sum('/commands/stop-job' in p for p, _ in server.calls), 1)
            self.assertFalse(any('/operations' in p for p, _ in server.calls),
                'cancelled owner must not start another HTTP lookup after its POST returns')
            self.assertFalse(any(t.name.startswith('mop-body-management') for t in threading.enumerate()))

    async def test_bounded_serial_owner_cancelled_queue_and_repeated_close_join(self):
        from mop.client.body_management import SerializedOwner
        entered, release = threading.Event(), threading.Event()
        class Blocking:
            stopping = threading.Event()
            calls, cleaned = [], False
            def call(self, value):
                self.calls.append(value); entered.set(); release.wait(4)
                return 'unknown'
            def close(self): self.cleaned = True
        management = Blocking()
        owner = SerializedOwner(management, capacity=1)
        first = asyncio.create_task(owner.request('call', 'first'))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        queued = asyncio.create_task(owner.request('call', 'never-run'))
        await asyncio.sleep(0)
        with self.assertRaises(api.ClientError): await owner.request('call', 'overflow')
        queued.cancel(); first.cancel()
        for task in (queued, first):
            with self.assertRaises(asyncio.CancelledError): await task
        closing = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0); closing.cancel(); await asyncio.sleep(0); closing.cancel()
        self.assertFalse(management.cleaned)
        release.set()
        with self.assertRaises(asyncio.CancelledError): await closing
        self.assertEqual(management.calls, ['first'])
        self.assertTrue(management.cleaned)
        self.assertTrue(owner.worker.done())
        self.assertFalse(any(t.name.startswith('mop-body-management') for t in threading.enumerate()))


if __name__ == '__main__':
    unittest.main()
