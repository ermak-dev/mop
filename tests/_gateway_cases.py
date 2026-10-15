"""Контракты шлюза #647 на собственной PostgreSQL fixture."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import threading
import uuid
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connections
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

def _process_gateway(database, actor, version, port, start, results):
    # Spawn открывает собственные Django/PG connections, не наследует pool/socket.
    from mop.server.controller.runtime import configure, Runtime, close_pools
    configure()
    from django.conf import settings
    settings.DATABASES['default']['NAME'] = database
    connections['default'].settings_dict['NAME'] = database
    from mop.server.gateway.service import Gateway
    from mop.server.gateway.transport import LoopbackHTTP
    from mop.server.gateway.values import Credential
    key = Credential(version, 'two-process-fixture-key')
    class Source:
        def get(self, actor):
            return key
        def current(self, actor, value):
            return value == key
    async def run():
        runtime = Runtime(blocking_limit=2, queue_limit=2)
        await runtime.start()
        try:
            gateway = Gateway(runtime=runtime, source=Source(), transport=LoopbackHTTP(port), wait_seconds=2)
            return (await gateway.request(actor, 'GET', '/v8/management/config', b'', {})).status
        finally:
            await runtime.aclose()
    try:
        start.wait(10)
        results.put(asyncio.run(run()))
    except Exception as exc:
        results.put(type(exc).__name__)
    finally:
        close_pools()


# HYPOTHESIS: прежний HTTP gateway не подключён к Django и общему ORM breaker.
# SOLUTION: отдельный adapter, текущая session authority и короткие ORM claims.
# Собственный red — отсутствующие маршруты/поведение, не restore/root foundation.
@override_settings(ALLOWED_HOSTS=['testserver'])
class GatewayBoundary(TransactionTestCase):
    def test_gateway_is_registered_and_guest_never_gets_panel(self):
        self.assertTrue(apps.is_installed('mop.server.gateway'), 'gateway app must be installed')
        for path in ('/llm/management.html', '/llm/raw/management.html', '/v8/management/config'):
            response = Client().get(path, secure=True)
            self.assertEqual(response.status_code, 403)
            self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    def test_unsafe_management_requires_django_csrf_even_before_proxy(self):
        response = Client(enforce_csrf_checks=True).put('/v8/management/config', '{}',
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 403)

    def test_inference_is_not_a_management_cookie_route(self):
        response = Client().get('/v1/models', secure=True)
        self.assertEqual(response.status_code, 404)

    def test_current_human_permission_not_staff_flag_and_unbound_default_deny(self):
        from mop.server.access.services import bind_local_identity
        user = get_user_model().objects.create_user('panel', password='fixture-only', is_staff=True)
        bind_local_identity(user.pk)
        client = Client()
        client.force_login(user)
        response = client.get('/llm/management.html', secure=True)
        self.assertEqual(response.status_code, 403)
        permission = Permission.objects.get(content_type__app_label='gateway', codename='manage_proxy')
        user.user_permissions.add(permission)
        response = client.get('/v8/management/config', secure=True)
        self.assertEqual(response.status_code, 503)
        user.user_permissions.clear()
        self.assertEqual(client.get('/llm/management.html', secure=True).status_code, 403)


class GatewayPolicies(TransactionTestCase):
    def test_guard_and_redaction_have_one_owner_for_old_and_new_adapters(self):
        self.assertTrue(apps.is_installed('mop.server.gateway'), 'gateway app must be installed')
        from mop.server import management_policy as policy, web
        self.assertIs(web.guard_management_write, policy.guard_management_write)
        self.assertIs(web.redact_management_read, policy.redact_management_read)
        # Нельзя пропустить вложенный ключ из-за первого безопасного поля.
        result = policy.guard_management_write('PUT', '/v8/management/config',
            b'{"secret-key":"managed-by-mop","management":{"secret-key":"replacement"}}', lambda: 'key')
        self.assertEqual(result.get('status'), 403)
        for path, body in (('/v8/management/config', b'{"management":"raw-private-hash"}'),
                           ('/v8/management/config.yaml', b'management: raw-private-hash')):
            self.assertIsNone(policy.redact_management_read(path, body))


class GatewayCoordination(TransactionTestCase):
    def test_breaker_claims_are_durable_singleflight_not_local_locks(self):
        self.assertTrue(apps.is_installed('mop.server.gateway'), 'gateway ORM must be installed')
        from mop.server.gateway.breaker import Breaker
        from mop.server.gateway.values import Credential
        key = Credential(uuid.uuid4(), 'fixture-management-key')
        now = timezone.now()
        first, second = Breaker(clock=lambda: now), Breaker(clock=lambda: now)
        claim = first.claim(key, write=False)
        self.assertEqual(claim.state, 'claimed')
        self.assertEqual(second.claim(key, write=False).state, 'waiting')
        first.complete(claim, 403)
        self.assertEqual(second.claim(key, write=False).state, 'rejected')
        rotated = Credential(uuid.uuid4(), 'rotated-management-key')
        self.assertEqual(Breaker(clock=lambda: now).claim(rotated, write=False).state, 'cooldown')
        later = Breaker(clock=lambda: now + timedelta(seconds=1801))
        self.assertEqual(later.claim(rotated, write=False).state, 'claimed')
        self.assertNotIn('fixture-management-key', repr(claim))

    def test_write_wait_timeout_and_worker_restart_do_not_release_unknown_effect(self):
        self.assertTrue(apps.is_installed('mop.server.gateway'), 'gateway ORM must be installed')
        from mop.server.gateway.breaker import Breaker
        from mop.server.gateway.values import Credential
        now = timezone.now()
        key = Credential(uuid.uuid4(), 'fixture-key')
        first = Breaker(clock=lambda: now)
        claim = first.claim(key, write=True)
        restarted = Breaker(clock=lambda: now + timedelta(hours=1))
        self.assertEqual(restarted.claim(key, write=True).state, 'unknown')
        first.complete(claim, None)
        self.assertEqual(restarted.claim(key, write=True).state, 'unknown')


class GatewayExecution(TransactionTestCase):
    def setUp(self):
        self.assertTrue(apps.is_installed('mop.server.gateway'), 'gateway app must be installed')
        from mop.server.access.services import bind_local_identity, session_context
        self.user = get_user_model().objects.create_user('gateway-human', password='fixture-only')
        bind_local_identity(self.user.pk)
        self.user.user_permissions.add(Permission.objects.get(
            content_type__app_label='gateway', codename='manage_proxy'))
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.actor = session_context(self.client.session.session_key)
        self.key_id = uuid.uuid4()

    def run_gateway(self, method='GET', path='/v8/management/config', body=b'', headers=None,
                    upstream=None, source=None):
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.values import Credential, Reply
        from mop.server.controller.runtime import Runtime
        key = Credential(self.key_id, 'fixture-private-management-key')
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return credential == key
        class Transport:
            def call(self, method, path, body, headers, credential):
                if upstream:
                    return upstream(method, path, body, headers, credential)
                return Reply(200, b'{"management":{"secret-key":"upstream-hash"}}',
                    (('Content-Type', 'application/json'), ('Set-Cookie', 'secret=cookie')))
        async def run():
            runtime = Runtime(blocking_limit=2, effect_limit=2, queue_limit=2)
            await runtime.start()
            try:
                gateway = Gateway(runtime=runtime, source=source or Source(), transport=Transport())
                return await gateway.request(self.actor, method, path, body, headers or {})
            finally:
                await runtime.aclose()
        return asyncio.run(run())

    def test_two_postgresql_processes_make_one_rejected_probe_and_restart_remembers(self):
        import multiprocessing
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import time
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                time.sleep(.2)
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'refused')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        ctx = multiprocessing.get_context('spawn')
        start, results = ctx.Event(), ctx.Queue()
        children = []
        try:
            database = connections['default'].settings_dict['NAME']
            for _ in range(2):
                child = ctx.Process(target=_process_gateway, args=(database, self.actor,
                    self.key_id, server.server_port, start, results))
                child.start()
                children.append(child)
            start.set()
            statuses = [results.get(timeout=20) for _ in children]
            self.assertEqual(sorted(statuses), [503, 503])
            self.assertEqual(len(requests), 1)
            child = ctx.Process(target=_process_gateway, args=(database, self.actor,
                self.key_id, server.server_port, start, results))
            child.start()
            children.append(child)
            self.assertEqual(results.get(timeout=20), 503)
            self.assertEqual(len(requests), 1)
        finally:
            for child in children:
                child.join(5)
                if child.is_alive():
                    child.terminate()
                    child.join()
            results.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_config_redaction_and_strip_client_and_upstream_credentials(self):
        from mop.server.gateway.values import Reply
        seen = []
        def upstream(method, path, body, headers, credential):
            seen.append(headers)
            return Reply(200, b'{"management":{"secret-key":"upstream-hash"}}',
                (('Content-Type', 'application/json'), ('Set-Cookie', 'secret=cookie'),
                 ('Authorization', 'Bearer secret'), ('X-Cpa-Version', 'fixture')))
        reply = self.run_gateway(headers={'Authorization': 'Bearer attacker', 'Cookie': 'session=x',
            'Host': 'evil', 'Accept': 'application/json'}, upstream=upstream)
        self.assertEqual(reply.status, 200)
        self.assertNotIn(b'upstream-hash', reply.body)
        self.assertEqual(seen, [{'Accept': 'application/json'}])
        self.assertEqual(dict(reply.headers).get('X-Cpa-Version'), 'fixture')
        self.assertNotIn('Set-Cookie', dict(reply.headers))
        self.assertNotIn('Authorization', dict(reply.headers))

    def test_permission_logout_or_version_loss_after_http_wait_denies_read(self):
        from mop.server.gateway.values import Reply
        def upstream(*args):
            from django.contrib.sessions.models import Session
            Session.objects.filter(pk=self.client.session.session_key).delete()
            connections.close_all()
            return Reply(200, b'private-read', ())
        reply = self.run_gateway(upstream=upstream)
        self.assertEqual(reply.status, 403)
        self.assertNotIn(b'private-read', reply.body)

    def test_json_and_yaml_put_and_patch_preserve_key_and_refuse_replacement(self):
        import yaml
        from mop.server.gateway.values import Reply
        seen = []
        def upstream(method, path, body, *args):
            seen.append((method, path, body))
            return Reply(200, b'{}', ())
        for method, suffix, body in (
            ('PUT', 'config', b'{"debug":true}'),
            ('PATCH', 'config', b'{"management":{"secret-key":"managed-by-mop","allow-remote":false}}'),
            ('PUT', 'config.yaml', b'debug: true\n'),
            ('PATCH', 'config.yaml', b'management:\n  secret-key: managed-by-mop\n')):
            reply = self.run_gateway(method, '/v8/management/' + suffix, body, upstream=upstream)
            self.assertEqual(reply.status, 200)
            sent = yaml.safe_load(seen[-1][2])
            if method == 'PUT' or suffix.endswith('yaml'):
                self.assertEqual(sent['management']['secret-key'], 'fixture-private-management-key')
            else:
                self.assertNotIn('secret-key', sent['management'])
        calls = len(seen)
        reply = self.run_gateway('PUT', '/v8/management/config',
            b'{"management":{"secret-key":"replacement"}}', upstream=upstream)
        self.assertEqual(reply.status, 403)
        self.assertEqual(len(seen), calls)

    def test_redirects_paths_methods_limits_and_secret_echo_are_fail_closed(self):
        from mop.server.gateway.values import Reply
        for path in ('https://evil.test/v8/management/config', '/v1/models',
                     '/v8/management/../config', '/v8/management/%2e%2e/config',
                     '/v8/management/unlisted', '/v8/management/config?x=' + 'x' * 4097):
            self.assertEqual(self.run_gateway(path=path).status, 400)
        self.assertEqual(self.run_gateway(method='CONNECT').status, 405)
        self.assertEqual(self.run_gateway(body=b'x' * (4 * 1024 * 1024 + 1)).status, 413)
        self.assertEqual(self.run_gateway(upstream=lambda *a: Reply(302, b'https://evil.test',
            (('Location', 'https://evil.test'),))).status, 502)
        self.assertEqual(self.run_gateway(upstream=lambda *a: Reply(200,
            b'fixture-private-management-key', ())).status, 502)

    def test_adapter_exception_is_unknown_write_not_replay_or_secret_in_error(self):
        def upstream(*args):
            raise TimeoutError('fixture-private-management-key')
        reply = self.run_gateway('PUT', '/v8/management/config', b'{}', upstream=upstream)
        self.assertEqual(reply.status, 504)
        self.assertIn(b'unknown', reply.body)
        self.assertNotIn(b'fixture-private', reply.body)
        self.assertEqual(self.run_gateway('PUT', '/v8/management/config', b'{}').status, 409)


class ProxyCredentialBoundary(TransactionTestCase):
    def test_controller_secret_gate_is_purpose_bound_and_rechecks_after_decryption(self):
        from mop.server.secrets.services import SecretStore
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        from mop.server.controller.runtime import initialize
        initialize()
        class Allow:
            def authorize(self, *args, **kwargs):
                pass
        crypto = XChaCha20Provider(Keyring('fixture', {'fixture': b'x' * 32}))
        writer = SecretStore(provider=crypto, authorizer=Allow())
        secret = writer.create(None, project_id=None, name='MANAGEMENT', kind='variable',
            purpose='proxy', value=b'only-loopback-not-browser')
        self.assertTrue(callable(getattr(writer, 'issue_proxy_management', None)),
            'controller-specific secret consumer is missing')
        class Gate:
            def resolve(self, actor):
                if actor != 'current-human':
                    raise PermissionError('current human required')
                return secret.id
        reader = SecretStore(provider=crypto, controller_gate=Gate())
        issued = reader.issue_proxy_management('current-human')
        self.assertEqual(issued.value, b'only-loopback-not-browser')
        self.assertEqual(issued.version_id, secret.active_version_id)
        self.assertNotIn('only-loopback-not-browser', repr(issued))
        with self.assertRaises(PermissionError):
            reader.issue_proxy_management('service')
        class Revoking:
            def decrypt(self, *args):
                from mop.server.secrets.models import Secret
                row = Secret.objects.get(pk=secret.id)
                row.revoked = True
                row.save()
                return crypto.decrypt(*args)
        with self.assertRaises(ValueError):
            SecretStore(provider=Revoking(), controller_gate=Gate()).issue_proxy_management('current-human')


    def test_secret_store_has_narrow_default_deny_controller_consumer(self):
        from mop.server.secrets.services import SecretStore
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        store = SecretStore(provider=XChaCha20Provider(Keyring('fixture', {'fixture': b'x' * 32})))
        self.assertTrue(callable(getattr(store, 'issue_proxy_management', None)),
            'controller gateway needs an explicit default-deny secret issuance port')
        with self.assertRaises(PermissionError):
            store.issue_proxy_management(None)
