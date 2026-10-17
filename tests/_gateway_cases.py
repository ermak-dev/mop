"""Контракты шлюза #647 на собственной PostgreSQL fixture."""
import asyncio
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

from _gateway_process import process_gateway as _process_gateway


def bound_client(gateway, *, cookies=None, enforce_csrf_checks=False):
    """Django test handler получает тот же owned binding, что ASGI ingress."""
    from django.test import AsyncClient
    from django.test.client import AsyncClientHandler
    from asgiref.sync import sync_to_async
    class BoundHandler(AsyncClientHandler):
        async def __call__(self, scope):
            try:
                return await super().__call__({**scope, 'mop.gateway': gateway})
            finally:
                # AsyncClient отключает request_finished DB cleanup ради test
                # transaction. asyncio.run выносит sync middleware в отдельный
                # thread: его соединение не закроет sync TransactionTestCase.
                await sync_to_async(connections.close_all)()
    client = AsyncClient(enforce_csrf_checks=enforce_csrf_checks)
    client.handler = BoundHandler(enforce_csrf_checks=enforce_csrf_checks)
    if cookies is not None:
        client.cookies = cookies
    return client


def login_browser(case, user):
    """Публичный login создаёт session binding; fixture не пишет private credential."""
    client = Client(enforce_csrf_checks=True)
    response = client.get('/api/v1/auth/csrf', secure=True, HTTP_HOST='testserver')
    case.assertEqual(response.status_code, 200)
    response = client.post('/api/v1/auth/login',
        {'username': user.username, 'password': 'fixture-only'}, secure=True,
        HTTP_HOST='testserver', HTTP_ORIGIN='https://testserver',
        HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
    case.assertEqual(response.status_code, 200)
    return client


def management_get(path, *, cookies=None):
    from mop.server.controller.runtime import Runtime
    from mop.server.gateway.service import Gateway
    async def run():
        runtime = Runtime()
        await runtime.start()
        try:
            client = bound_client(Gateway(runtime=runtime), cookies=cookies)
            return await client.get(path, secure=True)
        finally:
            await runtime.aclose()
    return asyncio.run(run())


# HYPOTHESIS: прежний HTTP gateway не подключён к Django и общему ORM breaker.
# SOLUTION: отдельный adapter, текущая session authority и короткие ORM claims.
# RED: PG16 — 14 собственных failures до реализации; отдельно известный restore/root.
# RESULT: общий runner 158/158 на PG16/Python3.12 и PG17/Python3.13,
# включая обычное удаление test DB и owned Docker cleanup; старый restore red закрыт #632.
# STATUS: FIXED — see #647; adapter/core, не full CI или новая установка.
@override_settings(ALLOWED_HOSTS=['testserver'])
class GatewayBoundary(TransactionTestCase):
    serialized_rollback = True

    def test_gateway_is_registered_and_guest_never_gets_panel(self):
        self.assertTrue(apps.is_installed('mop.server.gateway'), 'gateway app must be installed')
        for path in ('/llm/management.html', '/llm/raw/management.html', '/v8/management/config'):
            response = management_get(path)
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
        from mop.server.access.models import AllProjectMembership, LoginIdentity
        AllProjectMembership.objects.create(user=user, source=LoginIdentity.objects.get(user=user).source)
        client = login_browser(self, user)
        response = management_get('/llm/management.html', cookies=client.cookies)
        self.assertEqual(response.status_code, 403)
        permission = Permission.objects.get(content_type__app_label='gateway', codename='manage_proxy')
        user.user_permissions.add(permission)
        # Даже полный scope/permission не усыновляет session мимо trusted login.
        unbound = Client()
        unbound.force_login(user)
        self.assertEqual(management_get('/v8/management/config', cookies=unbound.cookies).status_code, 403)
        response = management_get('/v8/management/config', cookies=client.cookies)
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'proxy management upstream is not bound', response.content)
        user.user_permissions.clear()
        self.assertEqual(management_get('/llm/management.html', cookies=client.cookies).status_code, 403)


class GatewayPolicies(TransactionTestCase):
    serialized_rollback = True

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
    serialized_rollback = True

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


# HYPOTHESIS: force_login обходит trusted login hook и не создаёт binding.
# RED: PG17 — credential binding is unavailable в setUp; boundary 403 вместо 503.
# SOLUTION: enrollment, явный global scope и публичный login с настоящим CSRF.
# RESULT: PG17 — 34/34, включая CSRF, redaction, cancel и удержание physical claim.
# STATUS: FIXED — see #647
@override_settings(ALLOWED_HOSTS=['testserver'])
class GatewayExecution(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.assertTrue(apps.is_installed('mop.server.gateway'), 'gateway app must be installed')
        from mop.server.access.services import bind_local_identity, session_context
        from mop.server.controller.runtime import initialize
        initialize()
        self.user = get_user_model().objects.create_user('gateway-human', password='fixture-only')
        bind_local_identity(self.user.pk)
        from mop.server.access.models import AllProjectMembership, LoginIdentity
        AllProjectMembership.objects.create(user=self.user,
            source=LoginIdentity.objects.get(user=self.user).source)
        self.user.user_permissions.add(Permission.objects.get(
            content_type__app_label='gateway', codename='manage_proxy'))
        self.client = login_browser(self, self.user)
        self.actor = session_context(self.client.session.session_key)
        self.key_id = uuid.uuid4()

    def tearDown(self):
        from mop.server.controller.runtime import close_pools
        close_pools()
        super().tearDown()

    def run_gateway(self, method='GET', path='/v8/management/config', body=b'', headers=None,
                    upstream=None, source=None, panel=False):
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
                return await gateway.request(self.actor, method, path, body, headers or {}, panel=panel)
            finally:
                await runtime.aclose()
        return asyncio.run(run())

    def test_two_postgresql_processes_make_one_rejected_probe_and_restart_remembers(self):
        self._multiprocess_refusal(401)

    def test_two_process_403_cooldown_survives_worker_restart_and_key_rotation(self):
        self._multiprocess_refusal(403)

    def _multiprocess_refusal(self, upstream_status):
        import multiprocessing
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import time
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                time.sleep(.2)
                self.send_response(upstream_status)
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
            from django.conf import settings
            bootstrap = dict(settings.BOOTSTRAP)
            database = connections['default'].settings_dict['NAME']
            for _ in range(2):
                child = ctx.Process(target=_process_gateway, args=(bootstrap, database, self.actor,
                    self.key_id, server.server_port, start, results))
                child.start()
                children.append(child)
            start.set()
            statuses = [results.get(timeout=20) for _ in children]
            self.assertEqual(sorted(statuses), [503, 503])
            self.assertEqual(len(requests), 1)
            child = ctx.Process(target=_process_gateway, args=(bootstrap, database, self.actor,
                self.key_id, server.server_port, start, results))
            child.start()
            children.append(child)
            self.assertEqual(results.get(timeout=20), 503)
            self.assertEqual(len(requests), 1)
            child = ctx.Process(target=_process_gateway, args=(bootstrap, database, self.actor,
                uuid.uuid4(), server.server_port, start, results, 'two-process-rotated-key'))
            child.start()
            children.append(child)
            self.assertEqual(results.get(timeout=20), 503)
            self.assertEqual(len(requests), 1 if upstream_status == 403 else 2)
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

    def test_panel_relative_redirect_is_bounded_and_never_changes_loopback_target(self):
        from mop.server.gateway.values import Reply
        calls = []
        def once(*args):
            calls.append(args[1])
            return (Reply(302, b'', (('Location', '/management.html'),)) if len(calls) == 1
                    else Reply(200, b'<html><head></head></html>', ()))
        reply = self.run_gateway(path='/management.html', upstream=once, panel=True)
        self.assertEqual(reply.status, 200)
        self.assertEqual(calls, ['/management.html', '/management.html'])
        calls.clear()
        def loop(*args):
            calls.append(args[1])
            return Reply(302, b'', (('Location', 'management.html'),))
        self.assertEqual(self.run_gateway(path='/management.html', upstream=loop, panel=True).status, 502)
        self.assertEqual(len(calls), 3)
        calls.clear()
        def external(*args):
            calls.append(args[1])
            return Reply(302, b'', (('Location', 'http://evil.invalid/management.html'),))
        self.assertEqual(self.run_gateway(path='/management.html', upstream=external, panel=True).status, 502)
        self.assertEqual(len(calls), 1)

    def test_whitelisted_headers_and_decoded_json_cannot_echo_management_credential(self):
        from mop.server.gateway.values import Reply
        header = self.run_gateway(upstream=lambda *args: Reply(200, b'{}',
            (('X-Cpa-Version', 'fixture-private-management-key'),)))
        self.assertEqual(header.status, 502)
        escaped = b'{"error":"\\u0066ixture-private-management-key"}'
        body = self.run_gateway(path='/v8/management/credentials',
            upstream=lambda *args: Reply(200, escaped, (('Content-Type', 'application/json'),)))
        self.assertEqual(body.status, 502)
        self.assertNotIn(b'fixture-private', body.body)

    def test_nested_management_hash_and_malformed_secret_paths_do_not_leak(self):
        from mop.server.gateway.values import Reply
        calls = []
        def upstream(*args):
            calls.append(args[1])
            return Reply(200, b'{"nested":{"management":{"secret-key":"$2b$hash"}}}',
                         (('Content-Type', 'application/json'),))
        for path in ('/v8/management/config/management', '/v8/management/config/management/secret-key'):
            reply = self.run_gateway(path=path, upstream=upstream)
            self.assertIn(reply.status, (400, 403))
            self.assertNotIn(b'$2b$hash', reply.body)
        reply = self.run_gateway(path='/v8/management/config', upstream=upstream)
        self.assertEqual(reply.status, 200)
        self.assertNotIn(b'$2b$hash', reply.body)

    def test_permission_downgrade_and_secret_version_loss_after_wait(self):
        from mop.server.gateway.values import Reply, Credential
        def demote(*args):
            get_user_model().objects.get(pk=self.user.pk).user_permissions.clear()
            connections.close_all()
            return Reply(200, b'private-body', ())
        self.assertEqual(self.run_gateway(upstream=demote).status, 403)
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label='gateway', codename='manage_proxy'))
        key = Credential(self.key_id, 'fixture-private-management-key')
        class Source:
            valid = True
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return self.valid
        source = Source()
        def rotate(*args):
            source.valid = False
            return Reply(200, b'private-body', ())
        reply = self.run_gateway(source=source, upstream=rotate)
        self.assertEqual(reply.status, 503)
        self.assertNotIn(b'private-body', reply.body)

    def test_real_owned_http_adapter_shares_redaction_header_and_redirect_contract(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from mop.server.gateway.transport import LoopbackHTTP
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.values import Credential
        from mop.server.controller.runtime import Runtime
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(302 if 'redirect' in self.path else 200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Set-Cookie', 'upstream=secret')
                self.send_header('WWW-Authenticate', 'secret')
                self.send_header('X-Cpa-Version', 'fixture')
                self.send_header('Location', 'http://evil.invalid/')
                self.end_headers()
                self.wfile.write(b'{"management":{"secret-key":"private-hash"}}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        key = Credential(self.key_id, 'fixture-http-key')
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return key == credential
        async def run():
            runtime = Runtime()
            await runtime.start()
            try:
                gateway = Gateway(runtime=runtime, source=Source(), transport=LoopbackHTTP(server.server_port))
                reply = await gateway.request(self.actor, 'GET', '/v8/management/config', b'',
                    {'Cookie': 'browser=secret', 'Authorization': 'Bearer browser-secret'})
                self.assertEqual(reply.status, 200)
                self.assertNotIn(b'private-hash', reply.body)
                self.assertEqual(dict(reply.headers)['X-Cpa-Version'], 'fixture')
                self.assertNotIn('Set-Cookie', dict(reply.headers))
                self.assertNotIn('WWW-Authenticate', dict(reply.headers))
                redirect = await gateway.request(self.actor, 'GET', '/v8/management/config?redirect=1', b'', {})
                self.assertEqual(redirect.status, 502)
            finally:
                await runtime.aclose()
        try:
            asyncio.run(run())
            self.assertEqual(len(seen), 2)
            self.assertNotIn('Cookie', seen[0])
            self.assertEqual(seen[0]['Authorization'], 'Bearer fixture-http-key')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_stock_panel_csrf_adapter_and_unsafe_django_session_requests(self):
        import re
        from mop.server.controller.runtime import Runtime
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.values import Credential, Reply
        key = Credential(self.key_id, 'fixture-http-key')
        seen = []
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return credential == key
        class Transport:
            def call(self, method, path, body, *args):
                seen.append((method, path))
                return Reply(200, b'<!doctype html><html><head><script>stock()</script></head></html>'
                    if path == '/management.html' else b'{}', ())
        async def run():
            runtime = Runtime()
            await runtime.start()
            try:
                gateway = Gateway(runtime=runtime, source=Source(), transport=Transport())
                client = bound_client(gateway, cookies=self.client.cookies, enforce_csrf_checks=True)
                panel = await client.get('/llm/raw/management.html', secure=True)
                self.assertEqual(panel.status_code, 200)
                text = panel.content.decode()
                self.assertLess(text.index('const csrf = '), text.index('stock()'))
                self.assertIn('X-CSRFToken', text)
                self.assertIn("credentials:'same-origin'", text)
                self.assertIn('XMLHttpRequest', text)
                self.assertNotIn(key.value, text)
                self.assertNotIn('<iframe', text)
                token = re.search(r'const csrf = "([A-Za-z0-9]+)"', text).group(1)
                missing = await client.put('/v8/management/config', '{}', content_type='application/json',
                    secure=True, headers={'Origin': 'https://testserver'})
                self.assertEqual(missing.status_code, 403)
                success = await client.put('/v8/management/config', '{}', content_type='application/json',
                    secure=True, headers={'Origin': 'https://testserver', 'X-CSRFToken': token})
                self.assertEqual(success.status_code, 200)
                foreign = await client.put('/v8/management/config', '{}', content_type='application/json',
                    secure=True, headers={'Origin': 'https://evil.invalid', 'X-CSRFToken': token})
                self.assertEqual(foreign.status_code, 403)
                logout_no_csrf = await client.post('/api/v1/auth/logout', secure=True)
                self.assertEqual(logout_no_csrf.status_code, 403)
                logout = await client.post('/api/v1/auth/logout', secure=True,
                    headers={'Origin': 'https://testserver', 'X-CSRFToken': token})
                self.assertEqual(logout.status_code, 200)
                self.assertEqual((await client.get('/llm/raw/management.html', secure=True)).status_code, 403)
            finally:
                await runtime.aclose()
        asyncio.run(run())
        self.assertEqual(seen, [('GET', '/management.html'), ('PUT', '/v8/management/config')])

    def test_composed_asgi_preserves_bounded_large_json_and_csrf_checked_multipart(self):
        from http.cookies import SimpleCookie
        from asgiref.testing import ApplicationCommunicator
        from mop.server.controller.asgi import ControllerApplication
        from mop.server.controller.runtime import Runtime
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.values import Credential, Reply
        key = Credential(self.key_id, 'fixture-composed-key')
        seen = []
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return key == credential
        class Transport:
            def call(self, method, path, body, headers, credential):
                seen.append((path, len(body), headers.get('Content-Type')))
                return Reply(200, b'{}', ())
        async def run():
            runtime = Runtime()
            gateway = Gateway(runtime=runtime, source=Source(), transport=Transport())
            app = ControllerApplication(runtime=runtime, gateway=gateway, startup=lambda: None)
            lifespan = ApplicationCommunicator(app, {'type': 'lifespan'})
            await lifespan.send_input({'type': 'lifespan.startup'})
            self.assertEqual((await lifespan.receive_output())['type'], 'lifespan.startup.complete')
            cookie = self.client.cookies.output(header='', sep=';').strip().encode()
            async def call(method, path, body=b'', extra=()):
                scope = {'type':'http', 'asgi':{'version':'3.0'}, 'http_version':'1.1',
                    'scheme':'https', 'server':('testserver',443), 'client':('127.0.0.1',1),
                    'method':method, 'path':path, 'raw_path':path.encode(), 'query_string':b'',
                    'headers':[(b'host',b'testserver'),(b'cookie',cookie),
                        (b'content-length',str(len(body)).encode()), *extra]}
                http = ApplicationCommunicator(app, scope)
                await http.send_input({'type':'http.request', 'body':body, 'more_body':False})
                start = await http.receive_output(timeout=5)
                while (await http.receive_output(timeout=5)).get('more_body', False):
                    pass
                await http.wait(timeout=5)
                return start
            try:
                start = await call('GET', '/llm/management.html')
                self.assertEqual(start['status'], 200)
                cookies = SimpleCookie()
                for name, value in start['headers']:
                    if name.lower() == b'set-cookie':
                        cookies.load(value.decode())
                token = cookies['csrftoken'].value.encode()
                cookie += b'; csrftoken=' + token
                auth = ((b'origin',b'https://testserver'),(b'x-csrftoken',token))
                large = json.dumps({'large': 'x' * (3 * 1024 * 1024)}).encode()
                start = await call('PUT', '/v8/management/config', large,
                    (*auth,(b'content-type',b'application/json')))
                self.assertEqual(start['status'], 200)
                multipart = (b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="a.json"\r\n'
                             b'Content-Type: application/json\r\n\r\n{}\r\n--fixture--\r\n')
                start = await call('POST', '/v8/management/credentials', multipart,
                    (*auth,(b'content-type',b'multipart/form-data; boundary=fixture')))
                self.assertEqual(start['status'], 200)
            finally:
                await lifespan.send_input({'type':'lifespan.shutdown'})
                self.assertEqual((await lifespan.receive_output(timeout=5))['type'], 'lifespan.shutdown.complete')
                await lifespan.wait(timeout=5)
        asyncio.run(run())
        self.assertEqual(len(seen), 2)
        self.assertGreater(seen[0][1], 3 * 1024 * 1024)
        self.assertEqual(seen[1][2], 'multipart/form-data; boundary=fixture')

    def test_root_bound_secret_source_uses_encrypted_current_version_and_no_file_fallback(self):
        from mop.server.secrets.services import SecretStore
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        from mop.server.gateway.authority import ControllerSecretGate, SecretCredentialSource
        from mop.server.gateway.values import Reply
        class Allow:
            def authorize(self, *args, **kwargs):
                pass
        crypto = XChaCha20Provider(Keyring('fixture', {'fixture': b'y' * 32}))
        secret = SecretStore(provider=crypto, authorizer=Allow()).create(self.actor, project_id=None,
            name='MANAGEMENT_KEY', kind='variable', purpose='proxy', value=b'only-encrypted-loopback-key')
        source = SecretCredentialSource(SecretStore(provider=crypto,
            controller_gate=ControllerSecretGate(secret.id)))
        seen = []
        def upstream(method, path, body, headers, credential):
            self.assertFalse(connections['default'].in_atomic_block)
            seen.append((credential.value, credential.version_id))
            return Reply(200, b'{}', ())
        self.assertEqual(self.run_gateway(source=source, upstream=upstream).status, 200)
        self.assertEqual(seen, [('only-encrypted-loopback-key', secret.active_version_id)])
        from mop.server.secrets.models import SecretAudit
        audit = tuple(SecretAudit.objects.filter(action='proxy_issue').values('action', 'actor_id', 'revision'))
        self.assertEqual(len(audit), 1)
        self.assertNotIn('only-encrypted', repr(audit))
        missing = SecretCredentialSource(SecretStore(provider=crypto,
            controller_gate=ControllerSecretGate(uuid.uuid4())))
        missing_reply = self.run_gateway(source=missing)
        self.assertEqual(missing_reply.status, 503)
        self.assertIn(b'proxy management secret is missing', missing_reply.body)

    # HYPOTHESIS: первый session lookup проходит мимо обработки bounded DB errors
    # в Gateway.request и превращает backpressure/DB outage в HTTP 500.
    # RED: PG16 — 156/158, оба исключения вышли через Django view.
    # SOLUTION: единая HTTP-граница переводит ошибки начальной/повторной проверки.
    # RESULT: PG16/17 — 158/158 с нормальным DB teardown; сырая причина не выходит.
    # STATUS: FIXED — see #647
    def test_owned_session_lookup_capacity_is_stable_429_without_upstream(self):
        from mop.server.controller.runtime import Runtime
        from mop.server.gateway.service import Gateway
        entered, release = threading.Event(), threading.Event()
        def busy():
            entered.set()
            release.wait(3)
        async def run():
            runtime = Runtime(database_limit=1, queue_limit=0)
            await runtime.start()
            task = asyncio.create_task(runtime.database(busy))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(.005)
                client = bound_client(Gateway(runtime=runtime), cookies=self.client.cookies)
                reply = await client.get('/llm/management.html', secure=True)
                self.assertEqual(reply.status_code, 429)
                self.assertIn(b'proxy management capacity exhausted', reply.content)
            finally:
                release.set()
                await task
                await runtime.aclose()
        asyncio.run(run())

    def test_owned_session_lookup_database_outage_is_stable_503(self):
        from django.db import DatabaseError
        from mop.server.controller.runtime import Runtime
        from mop.server.gateway.service import Gateway
        async def run():
            runtime = Runtime()
            await runtime.start()
            try:
                client = bound_client(Gateway(runtime=runtime), cookies=self.client.cookies)
                with patch('mop.server.gateway.authority.HumanAuthority.session',
                           side_effect=DatabaseError('private-driver-error')):
                    reply = await client.get('/llm/management.html', secure=True)
                self.assertEqual(reply.status_code, 503)
                self.assertIn(b'proxy management authority unavailable', reply.content)
                self.assertNotIn(b'private-driver-error', reply.content)
            finally:
                await runtime.aclose()
        asyncio.run(run())

    def test_service_and_delegated_never_proxy_even_with_staff_superuser_flags(self):
        # #684: вид Actor и делегирование (grant_id) закрывают proxy-панель
        # даже с флагами staff/superuser; human остаётся единственным входом.
        get_user_model().objects.filter(pk=self.user.pk).update(kind='service',
            is_staff=True, is_superuser=True)
        self.assertEqual(management_get('/llm/management.html', cookies=self.client.cookies).status_code, 403)
        get_user_model().objects.filter(pk=self.user.pk).update(kind='agent',
            is_staff=False, is_superuser=False)
        # Агент аутентифицируется тем же путём (#684): вид сам не является
        # запретом, панель проверяет permission живого Actor.
        self.assertEqual(management_get('/llm/management.html', cookies=self.client.cookies).status_code, 200)
        get_user_model().objects.filter(pk=self.user.pk).update(kind='human')
        self.assertEqual(management_get('/llm/management.html', cookies=self.client.cookies).status_code, 200)

    def test_slow_upstream_leaves_rest_loop_heartbeat_and_ws_transport_available(self):
        import time
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.values import Credential, Reply
        from mop.server.controller.runtime import Runtime
        from mop.server.realtime.gateway import Gateway as RealtimeGateway
        key = Credential(self.key_id, 'fixture-heartbeat-key')
        entered = threading.Event()
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return credential == key
        class Slow:
            def call(self, *args):
                entered.set()
                time.sleep(.4)
                return Reply(200, b'{}', ())
        async def run():
            runtime = Runtime(blocking_limit=2, effect_limit=2, queue_limit=2)
            await runtime.start()
            gateway = Gateway(runtime=runtime, source=Source(), transport=Slow())
            task = asyncio.create_task(gateway.request(self.actor, 'GET', '/v8/management/config', b'', {}))
            try:
                until = time.monotonic() + 2
                while not entered.is_set() and time.monotonic() < until:
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                started = time.monotonic()
                client = bound_client(gateway, cookies=self.client.cookies)
                self.assertEqual((await client.get('/api/v1/health', secure=True)).status_code, 200)
                messages = []
                async def send(message):
                    messages.append(message)
                # RED: PG17 — удалённый unbound_websocket не давал дойти до probe.
                # SOLUTION: текущий WS adapter сохраняет отказ неизвестному route.
                realtime = RealtimeGateway(runtime=runtime)
                await realtime.start()
                try:
                    await realtime({'type': 'websocket', 'path': '/ws/unregistered'}, None, send)
                finally:
                    await realtime.aclose()
                self.assertEqual(messages[0]['code'], 4404)
                await asyncio.sleep(.01)
                self.assertLess(time.monotonic() - started, .3)
                self.assertEqual((await task).status, 200)
            finally:
                await task
                await runtime.aclose()
                self.assertEqual(runtime.pending, 0)
        asyncio.run(run())

    def test_caller_timeout_holds_claim_and_waiter_rechecks_current_identity(self):
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.values import Credential, Reply
        from mop.server.gateway.models import Circuit
        from mop.server.controller.runtime import Runtime
        key = Credential(self.key_id, 'fixture-held-key')
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return credential == key
        class Slow:
            def call(self, *args):
                calls.append(args[1])
                entered.set()
                release.wait(3)
                return Reply(200, b'private-result', ())
        async def run():
            runtime = Runtime(blocking_limit=3, effect_limit=2, queue_limit=2)
            await runtime.start()
            gateway = Gateway(runtime=runtime, source=Source(), transport=Slow(), wait_seconds=.2)
            first = asyncio.create_task(gateway.request(self.actor, 'PUT', '/v8/management/config', b'{}', {}))
            try:
                while not entered.is_set():
                    await asyncio.sleep(.005)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(first), timeout=.02)
                self.assertEqual(await runtime.database(lambda: Circuit.objects.get(pk=1).state), 'running')
                waiter = asyncio.create_task(gateway.request(self.actor, 'GET', '/v8/management/config', b'', {}))
                await asyncio.sleep(.03)
                await runtime.database(lambda: get_user_model().objects.get(pk=self.user.pk).user_permissions.clear())
                self.assertEqual((await waiter).status, 403)
                self.assertEqual(len(calls), 1)
                self.assertEqual(await runtime.database(lambda: Circuit.objects.get(pk=1).state), 'running')
                release.set()
                self.assertEqual((await first).status, 403)
            finally:
                release.set()
                await first
                await runtime.aclose()
                self.assertEqual(runtime.pending, 0)
        asyncio.run(run())

    def test_cancelled_write_does_not_release_executor_slot_while_http_is_running(self):
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.values import Credential, Reply
        from mop.server.gateway.models import Circuit
        from mop.server.controller.runtime import Runtime
        key = Credential(self.key_id, 'fixture-cancel-key')
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, credential):
                return credential == key
        class Slow:
            def call(self, *args):
                calls.append(args[1])
                entered.set()
                release.wait(3)
                return Reply(200, b'{}', ())
        async def run():
            runtime = Runtime(blocking_limit=2, effect_limit=2, queue_limit=2)
            await runtime.start()
            gateway = Gateway(runtime=runtime, source=Source(), transport=Slow(), wait_seconds=.1)
            task = asyncio.create_task(gateway.request(self.actor, 'PUT', '/v8/management/config', b'{}', {}))
            try:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(.005)
                task.cancel()
                # Отмена caller быстрая; owned effect, HTTP и claim остаются живы.
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=.2)
                self.assertGreater(runtime.pending, 0)
                self.assertEqual(await runtime.database(lambda: Circuit.objects.get(pk=1).state), 'running')
                waiter = await gateway.request(self.actor, 'GET', '/v8/management/config', b'', {})
                self.assertEqual(waiter.status, 429)
                self.assertEqual(len(calls), 1)
                self.assertEqual(await runtime.database(lambda: Circuit.objects.get(pk=1).state), 'running')
                release.set()
                async with asyncio.timeout(2):
                    while await runtime.database(lambda: Circuit.objects.get(pk=1).state) != 'idle':
                        await asyncio.sleep(.005)
                    while runtime.pending:
                        await asyncio.sleep(.005)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await runtime.aclose()
                self.assertEqual(runtime.pending, 0)
        asyncio.run(run())

    def test_adapter_exception_is_unknown_write_not_replay_or_secret_in_error(self):
        def upstream(*args):
            raise TimeoutError('fixture-private-management-key')
        reply = self.run_gateway('PUT', '/v8/management/config', b'{}', upstream=upstream)
        self.assertEqual(reply.status, 504)
        self.assertIn(b'unknown', reply.body)
        self.assertNotIn(b'fixture-private', reply.body)
        self.assertEqual(self.run_gateway('PUT', '/v8/management/config', b'{}').status, 409)


class GatewayTransportContracts(TransactionTestCase):
    serialized_rollback = True

    # HYPOTHESIS: prebuilt Gateway может жить вне владельца ASGI shutdown.
    # RED: PG17 — ValueError not raised до identity check в bind_http.
    # SOLUTION: root принимает только Gateway с тем же Runtime.
    # RESULT: PG16/17 — оба ownership assertions проходят.
    # STATUS: FIXED — see #647
    def test_composition_refuses_gateway_owned_by_another_runtime(self):
        from mop.server.controller.runtime import Runtime
        from mop.server.gateway.composition import bind_http
        from mop.server.gateway.service import Gateway
        async def run():
            owner, other = Runtime(), Runtime()
            async def http(*args):
                pass
            try:
                with self.assertRaisesRegex(ValueError, 'runtime'):
                    bind_http(http, owner, Gateway(runtime=other))
                self.assertIs(bind_http(http, owner, Gateway(runtime=owner)).gateway.runtime, owner)
            finally:
                await owner.aclose()
                await other.aclose()
        asyncio.run(run())

    def test_owned_http_fixture_enforces_body_headers_deadline_and_never_uses_proxy_env(self):
        import os
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from mop.server.gateway.transport import LoopbackHTTP, TransportUnavailable
        from mop.server.gateway.policy import MAX_BODY
        from mop.server.gateway.values import Credential
        mode = ['large']
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                if mode[0] == 'large':
                    self.send_header('Content-Length', str(MAX_BODY + 1))
                elif mode[0] == 'headers':
                    for i in range(65):
                        self.send_header('X-Extra-' + str(i), 'a')
                elif mode[0] == 'encoding':
                    self.send_header('Content-Encoding', 'gzip')
                elif mode[0] == 'slow':
                    self.send_header('Content-Length', '100')
                self.end_headers()
                try:
                    if mode[0] == 'slow':
                        for _ in range(10):
                            self.wfile.write(b'x')
                            self.wfile.flush()
                            time.sleep(.04)
                    else:
                        self.wfile.write(b'{}')
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        key = Credential(uuid.uuid4(), 'fixture-only-key')
        transport = LoopbackHTTP(server.server_port, timeout=.15)
        try:
            with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1'}):
                for value in ('large', 'headers', 'encoding', 'slow'):
                    mode[0] = value
                    started = time.monotonic()
                    with self.assertRaises(TransportUnavailable):
                        transport.call('GET', '/v8/management/config', b'', {}, key)
                    self.assertLess(time.monotonic() - started, .8)
            self.assertEqual(len(seen), 4)
            with self.assertRaises(ValueError):
                LoopbackHTTP('http://attacker.invalid')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_asgi_ingress_rejects_body_before_django_or_upstream(self):
        from mop.server.gateway.composition import GatewayHTTP
        from mop.server.gateway.policy import MAX_BODY
        messages, calls = [], []
        async def http(*args):
            calls.append(args)
        async def receive():
            return {'type': 'http.request', 'body': b'x' * (MAX_BODY + 1), 'more_body': False}
        async def send(message):
            messages.append(message)
        asyncio.run(GatewayHTTP(http, None)({'type': 'http', 'path': '/v8/management/config'}, receive, send))
        self.assertEqual(messages[0]['status'], 413)
        self.assertEqual(calls, [])


    def test_asgi_ingress_rejects_conflicting_or_malformed_lengths_without_django(self):
        from mop.server.gateway.composition import GatewayHTTP
        for headers, expected in (([(b'content-length', b'bad')], 400),
                ([(b'content-length', b'999999999')], 413),
                ([(b'content-length', b'0')], 400),
                ([(b'content-length', b'1'), (b'content-length', b'2')], 400)):
            calls, messages = [], []
            async def http(*args):
                calls.append(args)
            async def receive():
                return {'type': 'http.request', 'body': b'x', 'more_body': False}
            async def send(message):
                messages.append(message)
            asyncio.run(GatewayHTTP(http, None)({'type': 'http', 'path': '/v8/management/config',
                'headers': headers}, receive, send))
            self.assertEqual(calls, [])
            self.assertEqual(messages[0]['status'], expected)


class ProxyCredentialBoundary(TransactionTestCase):
    serialized_rollback = True

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
