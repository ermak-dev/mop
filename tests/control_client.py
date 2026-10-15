#!/usr/bin/env python3
"""Независимый HTTP-клиент #642: без сервера, пула и чужих credentials.

HYPOTHESIS: явный HTTPS-контекст и неизменяемая команда отделяют намерение
от потери ответа; клиент не должен угадывать исход эффекта или новый ключ.
SOLUTION: конечные маршруты, ограниченный транспорт и материализованные DTO.
RESULT: red отсутствующего ядра; дополнительные red проверили утечку cleanup,
surrogate-ввод, HTTP/1.0 body, framing, identity, debug и epoch/snapshot mix.
Green: транспортный двойник и собственный socketpair; не PostgreSQL/живой TLS.
STATUS: FIXED — see #642 (только раннее клиентское ядро).
"""
import dataclasses
import importlib.util
import json
import os
import ssl
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
from _lib import patched_env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

# Отсутствующая возможность — намеренный red, не сбой окружения/зависимостей.
assert importlib.util.find_spec("mop.client.control") is not None, \
    "#642 missing independent HTTP control client"
from mop.client import control as api

TOKEN = "private-test-token"
MESSAGE = "private-message-never-in-diagnostics"
KEY = "8e6449b9-65db-4148-aa68-a8b4020bd8b5"
DEADLINE = "2026-10-16T10:05:00Z"
REVISION = "90071992547409931234567890"


def operation(state="accepted", revision=REVISION):
    return {"id": "o_789", "request_id": KEY, "state": state,
            "revision": revision,
            "target": {"id": "p_123", "incarnation": "i_456"}}


def envelope(**fields):
    return {"schema_version": 1, "epoch": "e_1", **fields}


class Response:
    def __init__(self, status=200, data=None, raw=None, headers=None):
        self.status = status
        self.body = raw if raw is not None else json.dumps(data or {}).encode()
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.closed = False
        self.read_sizes = []

    def getheader(self, name, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), default)

    def read1(self, size):
        self.read_sizes.append(size)
        part, self.body = self.body[:size], self.body[size:]
        return part

    def close(self):
        self.closed = True


class Socket:
    def settimeout(self, value):
        self.timeout = value


class Connection:
    def __init__(self, fixture, *args, **kwargs):
        self.fixture, self.args, self.kwargs = fixture, args, kwargs
        self.closed = False
        self.sock = Socket()
        self.debuglevel = 1
        fixture.connections.append(self)

    def connect(self):
        if self.fixture.connect_failure:
            raise OSError(TOKEN)

    def request(self, method, path, body=None, headers=None):
        self.fixture.calls.append((method, path, body, headers))
        if self.fixture.send_failure:
            raise OSError(TOKEN + MESSAGE)

    def getresponse(self):
        value = self.fixture.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class Fixture:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls, self.connections = [], []
        self.connect_failure = self.send_failure = False

    def factory(self, *args, **kwargs):
        return Connection(self, *args, **kwargs)


class ControlClient(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patcher = patch.object(api.http.client, "HTTPSConnection", self.fixture.factory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.context = api.ServerContext("https://controller.example:8443", TOKEN)
        self.client = api.Client(self.context)
        self.target = api.Target("p_123", "i_456", REVISION)

    def restart(self, **kw):
        return self.client.prepare_restart(self.target, epoch="e_1", not_after=DEADLINE,
                                           request_id=KEY, **kw)

    def send(self, **kw):
        return self.client.prepare_send(self.target, epoch="e_1", not_after=DEADLINE,
                                       request_id=KEY, message=MESSAGE, **kw)

    def queue(self, status=200, **data):
        response = Response(status, envelope(**data))
        self.fixture.responses.append(response)
        return response

    def test_explicit_https_context_and_reject_unsafe_inputs(self):
        for origin in ("http://controller.example", "https://user:pw@host", "https://host/#x",
                       "https://host/api", "https://host?x=1", "https://host\\evil",
                       "https://host\n", "//host", "https://host:"):
            with self.subTest(origin=origin), self.assertRaises(api.ClientError):
                api.ServerContext(origin, TOKEN)
        for token in ("", "x\r\nHost: bad", "x y"):
            with self.assertRaises(api.ClientError):
                api.ServerContext("https://controller.example", token)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.context.token = "changed"
        self.assertNotIn(TOKEN, repr(self.context))
        for bad in ("../other", "p/x", "%2f", "x?y", "https://evil", ".", "x#y"):
            with self.subTest(bad=bad), self.assertRaises(api.ClientError):
                self.client.puppet(bad)
        self.assertEqual(self.fixture.calls, [])

    def test_auth_not_browser_csrf_and_cleanup(self):
        response = self.queue(operation=operation())
        result = self.client.operation("o_789")
        method, path, body, headers = self.fixture.calls[0]
        self.assertEqual((method, path, body), ("GET", "/api/v1/operations/o_789", None))
        self.assertEqual(headers["Authorization"], "Token " + TOKEN)
        self.assertEqual(headers["Accept"], "application/json")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-CSRFToken", headers)
        self.assertEqual(result.revision, REVISION)
        self.assertFalse(result.terminal)
        context = self.fixture.connections[0].kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(response.closed)
        self.assertTrue(self.fixture.connections[0].closed)

    def test_prepare_before_io_exact_envelope_and_duplicate_identity(self):
        command = self.restart(fresh=False)
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(json.loads(command.body), envelope(
            request_id=KEY, incarnation="i_456", expected_revision=REVISION,
            not_after=DEADLINE, params={"fresh": False}))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            command.not_after = "2030-01-01T00:00:00Z"
        # Старая дата допустима для повторной отправки: порядок lookup задаёт сервер.
        for _ in range(2):
            self.queue(202, operation=operation())
            result = self.client.submit(command)
            self.assertEqual(result.http_status, 202)
            self.assertEqual(result.operation.state, "accepted")
            self.assertFalse(result.operation.terminal)
        self.assertEqual(self.fixture.calls[0][2], self.fixture.calls[1][2])
        self.assertEqual(self.fixture.calls[0][1], "/api/v1/puppets/p_123/commands/restart")
        self.assertEqual(self.fixture.calls[0][3]["Content-Type"], "application/json")
        self.assertNotEqual(self.client.prepare_restart(self.target, epoch="e_1", not_after=DEADLINE).request_id,
                            self.client.prepare_restart(self.target, epoch="e_1", not_after=DEADLINE).request_id)

    def test_send_finite_parameters_and_payload_safe_diagnostics(self):
        command = self.send(priority="later", wait_seconds=2, notify_when_idle=True)
        self.queue(202, operation=operation())
        self.client.submit(command)
        self.assertEqual(self.fixture.calls[0][1], "/api/v1/messages")
        body = json.loads(self.fixture.calls[0][2])
        self.assertEqual(body["target_id"], "p_123")
        self.assertEqual(body["params"], {"message": MESSAGE, "priority": "later",
                                          "wait_seconds": 2, "notify_when_idle": True})
        self.assertNotIn(MESSAGE, repr(command))
        self.assertNotIn(MESSAGE, str(command))
        for kw in ({"priority": "shell"}, {"wait_seconds": -1}, {"wait_seconds": 99999},
                   {"notify_when_idle": "true"}):
            with self.assertRaises(api.ClientError):
                self.send(**kw)
        for kw in ({"fresh": "false"},):
            with self.assertRaises(api.ClientError):
                self.restart(**kw)

    def test_lost_response_is_unknown_with_exact_key_and_get_recovery(self):
        command = self.send()
        self.fixture.responses.append(TimeoutError(TOKEN + MESSAGE))
        with self.assertRaises(api.UnknownSubmission) as caught:
            self.client.submit(command)
        unknown = caught.exception
        self.assertIs(unknown.command, command)
        self.assertEqual(unknown.request_id, KEY)
        self.assertIsNone(unknown.__context__)
        for text in (str(unknown), repr(unknown), repr(unknown.args)):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(MESSAGE, text)
        self.queue(operation=operation("reconciling"))
        recovered = self.client.lookup_operation(unknown.request_id)
        self.assertEqual(recovered.state, "reconciling")
        self.assertEqual(self.fixture.calls[1][0:2],
                         ("GET", "/api/v1/operations?request_id=" + KEY))
        self.assertEqual(len(self.fixture.calls), 2)
        self.assertTrue(all(c.closed for c in self.fixture.connections))

    def test_failure_during_send_unknown_but_connect_failure_is_transport(self):
        self.fixture.send_failure = True
        with self.assertRaises(api.UnknownSubmission):
            self.client.submit(self.restart())
        self.fixture.connect_failure = True
        with self.assertRaises(api.TransportError) as caught:
            self.client.submit(self.restart())
        self.assertNotIn(TOKEN, repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertTrue(all(c.closed for c in self.fixture.connections))

    def test_redirects_never_forward_token_or_change_method(self):
        for status in (301, 302, 303, 307, 308):
            for location in ("https://evil.example/steal", "/api/v1/operations/o_789"):
                self.fixture.responses.append(Response(status, headers={"Location": location}))
                before = len(self.fixture.calls)
                with self.assertRaises(api.UnknownSubmission):
                    self.client.submit(self.send())
                self.assertEqual(len(self.fixture.calls), before + 1)
                self.assertEqual(self.fixture.calls[-1][0], "POST")
        self.fixture.responses.append(Response(302, headers={"Location": "https://evil.example"}))
        with self.assertRaises(api.ProtocolError):
            self.client.operation("o_789")
        self.assertTrue(all(c.closed for c in self.fixture.connections))

    def test_safe_api_errors_preserved_once_distinct_outcomes(self):
        cases = [(403, "permission_denied", api.APIError), (404, "not_found", api.APIError),
                 (409, "request_conflict", api.Conflict), (410, "result_expired", api.ResultExpired),
                 (410, "reset_required", api.ResetRequired), (429, "rate_limited", api.APIError),
                 (503, "unavailable", api.APIError)]
        for status, code, error_type in cases:
            reason = "target changed; refresh before submitting"
            response = Response(status, envelope(error={"code": code, "message": reason,
                                "retryable": status in (429, 503)}, reset=code == "reset_required"),
                                headers={"Retry-After": "7"})
            self.fixture.responses.append(response)
            with self.subTest(code=code), self.assertRaises(error_type) as caught:
                self.client.operation("o_789")
            error = caught.exception.error
            self.assertEqual((error.http_status, error.code, error.reason), (status, code, reason))
            self.assertEqual(error.retry_after, 7)
            self.assertNotIn(reason, repr(error))
            with self.assertRaises(dataclasses.FrozenInstanceError):
                error.code = "changed"
            self.assertTrue(response.closed)

    def test_malformed_untrusted_errors_sanitized_without_exception_context(self):
        for data in ({"error": TOKEN + MESSAGE},
                     envelope(error={"code": TOKEN, "message": MESSAGE, "retryable": False}),
                     {"schema_version": 2, "error": {"code": "not_found", "message": MESSAGE}}):
            self.fixture.responses.append(Response(503, data))
            with self.assertRaises(api.ProtocolError) as caught:
                self.client.operation("o_789")
            for text in (str(caught.exception), repr(caught.exception), repr(caught.exception.args)):
                self.assertNotIn(TOKEN, text)
                self.assertNotIn(MESSAGE, text)
            self.assertIsNone(caught.exception.__context__)

    def test_wire_validation_bounds_and_no_false_submission_success(self):
        invalid = [b"not json " + TOKEN.encode(), b'{"schema_version":1,"schema_version":2}',
                   json.dumps(envelope(operation=operation(revision=12))).encode(),
                   json.dumps({**envelope(operation=operation()), "schema_version": True}).encode(),
                   json.dumps(envelope(operation=operation("imaginary"))).encode()]
        for raw in invalid:
            response = Response(raw=raw)
            self.fixture.responses.append(response)
            with self.assertRaises(api.ProtocolError):
                self.client.operation("o_789")
            self.assertTrue(response.closed)
        self.fixture.responses.append(Response(202, raw=b"bad"))
        with self.assertRaises(api.UnknownSubmission):
            self.client.submit(self.restart())
        self.fixture.responses.append(Response(202, envelope(operation={**operation(), "request_id":
                                                             "00000000-0000-4000-8000-000000000000"})))
        with self.assertRaises(api.UnknownSubmission):
            self.client.submit(self.restart())
        small = api.Client(self.context, limits=api.Limits(max_response_bytes=100, max_request_bytes=100))
        response = Response(raw=b"x" * 101)
        self.fixture.responses.append(response)
        with self.assertRaises(api.ProtocolError):
            small.operation("o_789")
        self.assertLessEqual(sum(response.read_sizes), 102)
        self.assertTrue(response.closed)
        before = len(self.fixture.calls)
        with self.assertRaises(api.ClientError):
            small.submit(self.restart())
        self.assertEqual(len(self.fixture.calls), before)

    def test_materialized_snapshot_details_pending_operations_and_revisions(self):
        data = {"puppets": [{"id": "p_123", "revision": REVISION, "facts": {"dirty": True}}],
                "cursor": "opaque/+position", "pending_operations": [operation()],
                "snapshot_id": "s_1", "next_cursor": None}
        self.queue(**data)
        snapshot = self.client.snapshot(project_id="pr_1", page_size=100)
        data["puppets"][0]["facts"]["dirty"] = False
        self.assertTrue(snapshot.data["puppets"][0]["facts"]["dirty"])
        self.assertEqual(snapshot.data["puppets"][0]["revision"], REVISION)
        self.assertEqual(snapshot.pending_operations[0].revision, REVISION)
        with self.assertRaises(TypeError):
            snapshot.data["puppets"][0]["facts"]["dirty"] = False
        self.queue(puppet={"id": "p_123", "revision": REVISION})
        details = self.client.puppet("p_123")
        self.assertEqual(details.data["puppet"]["revision"], REVISION)
        self.assertNotIn("puppets", repr(snapshot))

    def test_cursor_pagination_never_follows_url_and_binds_epoch_snapshot(self):
        cursor = "https://evil.example/a?token=not-a-credential"
        self.queue(results=[operation()], next_cursor=cursor)
        self.queue(results=[operation("running")], next_cursor=None)
        pages = list(self.client.operation_pages(page_size=1))
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1].items[0].state, "running")
        self.assertIn("cursor=https%3A%2F%2Fevil.example", self.fixture.calls[-1][1])
        self.assertTrue(self.fixture.calls[-1][1].startswith("/api/v1/operations?"))
        self.queue(results=[operation()], next_cursor="same")
        self.queue(results=[operation()], next_cursor="same")
        with self.assertRaises(api.ProtocolError):
            list(self.client.operation_pages())
        self.queue(results=[], next_cursor="after")
        self.fixture.responses.append(Response(data={**envelope(results=[], next_cursor=None), "epoch": "e_new"}))
        with self.assertRaises(api.ResetRequired):
            list(self.client.operation_pages())
        for size in (0, 501, True):
            with self.assertRaises(api.ClientError):
                self.client.operations_page(page_size=size)

    def test_wait_is_get_only_bounded_and_does_not_make_unknown_terminal(self):
        self.queue(operation=operation("unknown"))
        result = self.client.wait_operation("o_789", timeout=0.5)
        self.assertEqual(result.state, "unknown")
        self.assertTrue(result.terminal)
        self.queue(operation=operation("running"))
        with self.assertRaises(api.WaitTimeout) as caught:
            self.client.wait_operation("o_789", timeout=0.001, poll_interval=0.1)
        self.assertEqual(caught.exception.operation.state, "running")
        self.assertTrue(all(call[0] == "GET" for call in self.fixture.calls))
        with self.assertRaises(api.ClientError):
            self.client.wait_operation("o_789", timeout=99999)

    def test_transport_close_delimited_body_and_deadline_reader(self):
        import socket
        # Собственный socketpair проверяет настоящий http.client, не TLS/PG.
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        body = json.dumps(envelope(operation=operation(), padding="x" * 20000)).encode()
        right.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + body)
        right.shutdown(socket.SHUT_WR)
        wrapped = api._DeadlineSocket(left, api.time.monotonic() + 1, 0.5)
        connection = api.http.client.HTTPConnection("fixture.invalid")
        connection.sock = wrapped
        connection._HTTPConnection__state = api.http.client._CS_REQ_SENT
        response = connection.getresponse()
        # HTTP/1.0 закрывает connection перед чтением body: файл ещё владеет socket.
        self.assertIsNone(connection.sock)
        try:
            self.assertEqual(response.read(), body)
        finally:
            response.close()
            connection.close()
        self.assertEqual(left.fileno(), -1)
        other, peer = socket.socketpair()
        self.addCleanup(other.close)
        self.addCleanup(peer.close)
        timed = api._DeadlineSocket(other, api.time.monotonic() - 1, 1)
        stream = timed.makefile("rb")
        try:
            with self.assertRaises(TimeoutError):
                stream.read(1)
        finally:
            stream.close()
            timed.close()

    def test_wrong_resource_and_truncated_framing_are_not_valid_results(self):
        self.queue(operation=operation())
        with self.assertRaises(api.ProtocolError):
            self.client.operation("o_other")
    def test_truncated_framing_is_not_valid_result(self):
        response = Response(data=envelope(operation=operation()), headers={"Content-Length": "10000"})
        self.fixture.responses.append(response)
        with self.assertRaises(api.ProtocolError):
            self.client.operation("o_789")
        self.assertTrue(response.closed)

    def test_cleanup_failure_does_not_leak_and_still_closes_connection(self):
        response = self.queue(operation=operation())
        def bad_close():
            raise OSError(TOKEN + MESSAGE)
        response.close = bad_close
        with self.assertRaises(api.TransportError) as caught:
            self.client.operation("o_789")
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(TOKEN, repr(caught.exception.args))
        self.assertTrue(self.fixture.connections[-1].closed)

    def test_surrogate_message_and_bad_deadlines_fail_without_payload_in_args(self):
        with self.assertRaises(api.ClientError) as caught:
            self.client.prepare_send(self.target, epoch="e_1", not_after=DEADLINE,
                                     message=MESSAGE + "\ud800")
        self.assertNotIn(MESSAGE, repr(caught.exception.args))
        for value in ("2026-10-16", "2026-99-16T10:05:00Z", "2026-10-16T10:05:00+02:00"):
            with self.assertRaises(api.ClientError):
                self.client.prepare_restart(self.target, epoch="e_1", not_after=value)
        self.assertEqual(self.fixture.calls, [])

    def test_untrusted_nested_mutation_and_configuration_limits(self):
        for kw in ({"connect_timeout": 0}, {"read_timeout": float("nan")},
                   {"max_pages": True}, {"max_request_bytes": -1}):
            with self.assertRaises(api.ClientError):
                api.Limits(**kw)
        for command in (self.restart(), self.send()):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                command.target.revision = "1"
        for raw in (b'{"schema_version":1,"epoch":"e_1","data":NaN}',
                    (b'{"schema_version":1,"epoch":"e_1","data":' + b'[' * 70 + b'0' + b']' * 70 + b'}')):
            self.fixture.responses.append(Response(raw=raw))
            with self.assertRaises(api.ProtocolError):
                self.client.operation("o_789")

    def test_no_transport_debug_logging_and_explicit_ca_trust(self):
        self.queue(operation=operation())
        self.client.operation("o_789")
        self.assertEqual(self.fixture.connections[-1].debuglevel, 0)
        with patch.object(api.ssl, "create_default_context", wraps=ssl.create_default_context) as create:
            api.Client(api.ServerContext("https://controller.example", TOKEN))
            create.assert_called_once_with(cafile=None)
        with patch.object(api.ssl, "create_default_context", side_effect=OSError(TOKEN)):
            with self.assertRaises(api.ClientError) as caught:
                api.Client(api.ServerContext("https://controller.example", TOKEN, "/explicit/test-ca.pem"))
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_wait_checks_resource_and_epoch_on_every_read(self):
        self.queue(operation=operation())
        with self.assertRaises(api.ProtocolError):
            self.client.wait_operation("o_other", timeout=0.1)
    def test_wait_rejects_epoch_change(self):
        self.queue(operation=operation("running"))
        self.fixture.responses.append(Response(data={**envelope(operation=operation("succeeded")), "epoch": "e_new"}))
        with self.assertRaises(api.ResetRequired):
            self.client.wait_operation("o_789", timeout=0.1, poll_interval=0.001)

    def test_snapshot_pagination_rejects_mixed_identity_and_scoped_cursor_loop(self):
        self.queue(puppets=[], cursor="position", snapshot_id="s_1", next_cursor="next",
                   pending_operations=[operation()])
        self.queue(puppets=[], cursor="position", snapshot_id="s_1", next_cursor=None)
        pages = list(self.client.snapshot_pages(project_id="pr_1", page_size=10))
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].pending_operations[0].id, "o_789")
        self.assertIn("project_id=pr_1", self.fixture.calls[-1][1])
        self.assertIn("cursor=next", self.fixture.calls[-1][1])
        for second in ({"snapshot_id": "s_other", "cursor": "position"},
                       {"snapshot_id": "s_1", "cursor": "different"}):
            self.queue(puppets=[], cursor="position", snapshot_id="s_1", next_cursor="next")
            self.queue(puppets=[], next_cursor=None, **second)
            with self.assertRaises(api.ResetRequired):
                list(self.client.snapshot_pages())
        self.queue(puppets=[], cursor="position", snapshot_id="s_1", next_cursor="same")
        self.queue(puppets=[], cursor="position", snapshot_id="s_1", next_cursor="same")
        with self.assertRaises(api.ProtocolError):
            list(self.client.snapshot_pages())

    def test_identical_retry_returns_prior_operation_even_after_deadline(self):
        command = self.restart()
        self.queue(200, operation=operation("succeeded"))
        result = self.client.submit(command)
        self.assertEqual(result.operation.state, "succeeded")
        self.assertEqual(result.operation.request_id, KEY)
        self.assertTrue(result.operation.terminal)
        self.assertEqual(self.fixture.calls[-1][2], command.body)

    def test_no_server_imports_or_ambient_configuration(self):
        import ast
        with open(api.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        imports = [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.Import, ast.ImportFrom))]
        for node in imports:
            names = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            self.assertFalse(any(n and (n.startswith(("django", "rest_framework", "mop.server")) or
                                                 n in ("os", "mop.common.config", "mop.common.creds")) for n in names))
        self.assertNotIn("execute", dir(self.client))
        self.assertNotIn("call", dir(self.client))
        with patched_env(HTTPS_PROXY="https://evil.example", MOP_SERVER_LAN="evil"):
            self.queue(operation=operation())
            self.client.operation("o_789")
        self.assertEqual(self.fixture.connections[-1].args[0], "controller.example")


if __name__ == "__main__":
    unittest.main()
