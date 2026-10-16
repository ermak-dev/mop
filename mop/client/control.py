"""Типизированный HTTPS-клиент #642 без Django, NATS и локального state store.

Контекст приходит от вызывающего. Query DTO сохраняют авторитет сервера:
отсутствие action permissions не означает разрешение всех действий, а User
не является живым адресом сессии. Конечные команды не принимают RPC verbs.
"""
from collections.abc import Mapping, Iterator
from dataclasses import dataclass, field
from datetime import datetime
import http.client
import io
import json
import math
import re
import socket
import ssl
import time
from types import MappingProxyType
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4


class ClientError(Exception):
    """Локальный отказ без отражения недоверенного ввода."""


class ProtocolError(ClientError):
    """Ответ не соответствует поддерживаемому контракту; исход эффекта неизвестен."""


class TransportError(ClientError):
    """Не удалось получить ответ; это не результат серверной операции."""


@dataclass(frozen=True)
class ErrorSnapshot:
    schema_version: int
    epoch: str | None
    http_status: int
    code: str
    reason: str = field(repr=False)
    retryable: bool = False
    retry_after: int | None = None
    reset: object = field(default=None, repr=False)


class APIError(ClientError):
    """Безопасная причина доступна явно, но не попадает в диагностику исключения."""

    def __init__(self, error: ErrorSnapshot):
        self.error = error
        super().__init__("control API request refused")


class Conflict(APIError):
    """Конфликт не разрешает повтор с новым request_id."""


class ResultExpired(APIError):
    """Tombstone старого ключа: новый эффект не разрешён."""


class ResetRequired(APIError):
    """Нужны новая авторизация и снимок, не пустая успешная страница."""


class UnknownSubmission(ClientError):
    """POST мог достичь admission COMMIT; восстановление только по тому же ключу."""

    def __init__(self, command: "PreparedCommand"):
        self.command = command
        self.request_id = command.request_id
        super().__init__("submission outcome unknown; recover by request_id")


class WaitTimeout(ClientError):
    """Локальное ожидание закончилось, серверную операцию это не отменяет."""

    def __init__(self, operation: "Operation | None"):
        self.operation = operation
        super().__init__("operation wait deadline exceeded")


def _require(condition):
    if not condition:
        raise ClientError("invalid control client input")


def _identity(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None)
    return value


def _revision(value):
    _require(isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,127}", value) is not None)
    return value


def _key(value):
    valid = False
    if isinstance(value, str):
        try:
            valid = str(UUID(value)) == value
        except ValueError:
            pass
    _require(valid)
    return value


def _deadline(value):
    valid = False
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            valid = True
        except ValueError:
            pass
    # Срок не сравнивается с часами клиента: идентичный retry может быть старым.
    _require(valid)
    return value


def _seconds(value, maximum):
    _require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum)
    return value


def _cursor(value):
    _require(isinstance(value, str) and 0 < len(value) <= 4096 and
             all(32 < ord(c) < 127 for c in value))
    return value


@dataclass(frozen=True)
class ServerContext:
    """Единственный выбранный origin и transient token; ca_file — явное доверие CA.

    HTTP не разрешён даже по loopback: отдельного insecure production-пути нет.
    Никаких credentials в URL, base path, query, fragment или upstream override.
    """

    origin: str
    token: str = field(repr=False)
    ca_file: str | None = field(default=None, repr=False)

    def __post_init__(self):
        valid = False
        if isinstance(self.origin, str) and all(32 < ord(c) < 127 for c in self.origin):
            try:
                url = urlsplit(self.origin)
                valid = (url.scheme == "https" and bool(url.hostname) and
                         url.username is None and url.password is None and
                         url.path in ("", "/") and not url.query and not url.fragment and
                         "?" not in self.origin and "#" not in self.origin and
                         "\\" not in self.origin and not url.netloc.endswith(":") and
                         (url.port is None or 0 < url.port <= 65535))
            except ValueError:
                pass
        _require(valid)
        _require(isinstance(self.token, str) and re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", self.token) is not None)
        _require(self.ca_file is None or isinstance(self.ca_file, str) and bool(self.ca_file) and
                 "\x00" not in self.ca_file)
        object.__setattr__(self, "origin", self.origin.rstrip("/"))


@dataclass(frozen=True)
class Limits:
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    request_timeout: float = 30.0
    wait_timeout: float = 60.0
    max_request_bytes: int = 65536
    max_response_bytes: int = 2 * 1024 * 1024
    max_pages: int = 100

    def __post_init__(self):
        for name in ("connect_timeout", "read_timeout", "request_timeout", "wait_timeout"):
            _seconds(getattr(self, name), 3600)
        for name, maximum in (("max_request_bytes", 1024 * 1024),
                              ("max_response_bytes", 16 * 1024 * 1024), ("max_pages", 1000)):
            value = getattr(self, name)
            _require(type(value) is int and 0 < value <= maximum)


@dataclass(frozen=True)
class Target:
    """Одно неразрывное CAS-binding, не имя и не самостоятельно собранная модель."""

    id: str
    incarnation: str
    revision: str

    def __post_init__(self):
        _identity(self.id)
        _identity(self.incarnation)
        _revision(self.revision)


@dataclass(frozen=True)
class PreparedCommand:
    """Намерение полностью фиксируется до I/O; mutable params не принимаются.

    Конструктор тоже проверяет конечный набор вариантов: submit не является
    универсальным HTTP/RPC-методом даже для созданного напрямую значения.
    """

    action: str
    target: Target
    epoch: str
    request_id: str
    not_after: str
    fresh: bool = False
    message: str | None = field(default=None, repr=False)
    priority: str = "next"
    wait_seconds: int = 0
    notify_when_idle: bool = False

    def __post_init__(self):
        _require(type(self.target) is Target)
        _identity(self.epoch)
        _key(self.request_id)
        _deadline(self.not_after)
        _require(self.action in ("restart", "send") and type(self.fresh) is bool and
                 type(self.notify_when_idle) is bool and self.priority in ("now", "next", "later") and
                 type(self.wait_seconds) is int and 0 <= self.wait_seconds <= 60)
        if self.action == "restart":
            _require(self.message is None and self.priority == "next" and
                     self.wait_seconds == 0 and not self.notify_when_idle)
        else:
            valid = False
            if not self.fresh and isinstance(self.message, str):
                try:
                    valid = 0 < len(self.message.encode("utf-8")) <= 65536
                except UnicodeError:
                    pass
            _require(valid)

    @property
    def body(self) -> bytes:
        params = {"fresh": self.fresh} if self.action == "restart" else {
            "message": self.message, "priority": self.priority,
            "wait_seconds": self.wait_seconds, "notify_when_idle": self.notify_when_idle}
        data = {"schema_version": 1, "epoch": self.epoch, "request_id": self.request_id,
                "incarnation": self.target.incarnation, "expected_revision": self.target.revision,
                "not_after": self.not_after, "params": params}
        if self.action == "send":
            data["target_id"] = self.target.id
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@dataclass(frozen=True)
class NodeCommand:
    """#625 eligibility binding: у узла нет incarnation пользовательской сессии."""

    action: str
    node_id: str
    epoch: str
    request_id: str
    not_after: str
    expected_revision: str

    def __post_init__(self):
        _require(self.action in ("drain", "enable"))
        for value in (self.node_id, self.epoch, self.request_id):
            _key(value)
        _deadline(self.not_after)
        _revision(self.expected_revision)
        _require(1 <= int(self.expected_revision) < 2 ** 63)

    @property
    def body(self) -> bytes:
        return json.dumps({"schema_version": 1, "epoch": self.epoch,
            "request_id": self.request_id, "not_after": self.not_after,
            "expected_revision": self.expected_revision, "params": {}},
            sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True)
class Identity:
    user_id: int
    principal_id: str
    kind: str
    human_owner_id: int | None
    node_id: str | None
    grant_id: str | None
    action_permissions: frozenset[str] | None = None


@dataclass(frozen=True)
class Health:
    epoch: str
    state: str
    effects_enabled: bool


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    origin: str
    revision: str
    puppet_limit: int | None


@dataclass(frozen=True)
class Address:
    id: str
    session_id: str
    address: str
    project_id: str
    external_session_id: str
    display_name: str
    expires_at: datetime
    state: str
    live: bool


@dataclass(frozen=True)
class AddressPage:
    items: tuple[Address, ...]


@dataclass(frozen=True)
class ProjectPage:
    items: tuple[Project, ...]
    next_cursor: str | None = field(repr=False)


_STATES = frozenset(("accepted", "running", "succeeded", "refused", "expired", "cancelled",
                     "failed", "partial", "reconciling", "unknown"))
_TERMINAL = _STATES - {"accepted", "running", "reconciling"}


@dataclass(frozen=True)
class Operation:
    schema_version: int
    epoch: str
    id: str
    request_id: str
    state: str
    revision: str
    target_id: str
    incarnation: str | None
    data: Mapping = field(repr=False)

    @property
    def terminal(self) -> bool:
        # unknown завершает wait, но не доказывает освобождение claims.
        return self.state in _TERMINAL


@dataclass(frozen=True)
class Submission:
    http_status: int
    operation: Operation


@dataclass(frozen=True)
class Snapshot:
    schema_version: int
    epoch: str
    data: Mapping = field(repr=False)
    pending_operations: tuple[Operation, ...] = ()


@dataclass(frozen=True)
class Page:
    schema_version: int
    epoch: str
    items: tuple[Operation, ...]
    next_cursor: str | None = field(repr=False)


def _freeze(value, depth=0):
    if depth > 64:
        raise ValueError("invalid response nesting")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("revision", "expected_revision"):
                _revision(item)
        return MappingProxyType({k: _freeze(v, depth + 1) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v, depth + 1) for v in value)
    return value


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON field")
        data[key] = value
    return data


def _json(raw):
    failed = False
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON number")))
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported schema")
        if "epoch" in data:
            _identity(data["epoch"])
        frozen = _freeze(data)
    except (ValueError, TypeError, RecursionError, ClientError):
        failed = True
    if failed:
        raise ProtocolError("invalid control API response")
    return frozen


def _operation(data, epoch):
    failed = False
    try:
        _identity(data["id"])
        _key(data["request_id"])
        _revision(data["revision"])
        _identity(data["target"]["id"])
        _identity(epoch)
        incarnation = data["target"].get("incarnation")
        if data["target"].get("kind") == "node":
            _key(data["target"]["id"])
            _revision(data["target"]["revision"])
            _revision(data["target"]["source_revision"])
            _require(incarnation is None)
        else:
            _identity(incarnation)
        if data["state"] not in _STATES or data.get("epoch", epoch) != epoch:
            raise ValueError("invalid operation binding")
        result = Operation(1, epoch, data["id"], data["request_id"], data["state"], data["revision"],
                           data["target"]["id"], incarnation, data)
    except (KeyError, TypeError, ValueError, ClientError):
        failed = True
    if failed:
        raise ProtocolError("invalid operation response")
    return result


# Только известные безопасные категории. Неправильный envelope/сырой upstream
# переводится один раз; известная библиотечная причина сохраняется буквально.
_CODES = frozenset(("validation_error", "not_authenticated", "authentication_failed", "invalid_token",
                    "permission_denied", "not_found", "request_conflict", "revision_conflict",
                    "incarnation_conflict", "resource_conflict", "epoch_conflict", "stale_epoch",
                    "result_expired", "cursor_expired", "invalid_cursor", "reset_required",
                    "rate_limited", "throttled", "unavailable", "authority_unavailable",
                    "database_unavailable", "deadline_expired", "access_denied",
                    "authentication_required", "method_not_allowed", "request_refused", "csrf_failed",
                    "database_outcome_unknown", "installation_unavailable", "deadline_horizon",
                    "effects_disabled", "admission_limit", "claim_conflict", "operation_conflict",
                    "transaction_boundary", "controller_unavailable", "delivery_capacity",
                    "delivery_unavailable", "delivery_conflict"))


def _api_error(data, status, retry_after):
    failed = False
    try:
        error = data["error"]
        code, reason, retryable = error["code"], error["message"], error["retryable"]
        if code not in _CODES or not isinstance(reason, str) or len(reason) > 4096 or type(retryable) is not bool:
            raise ValueError("invalid error")
        snapshot = ErrorSnapshot(1, data.get("epoch"), status, code, reason, retryable,
                                 retry_after, data.get("reset"))
        cls = APIError
        if code in ("reset_required", "cursor_expired", "invalid_cursor", "stale_epoch", "epoch_conflict"):
            cls = ResetRequired
        elif status == 409:
            cls = Conflict
        elif code == "result_expired":
            cls = ResultExpired
    except (KeyError, ValueError, TypeError):
        failed = True
    if failed:
        raise ProtocolError("invalid control API error response")
    return cls(snapshot)


class _DeadlineReader(io.RawIOBase):
    """Каждый recv проверяет общий срок, включая медленные заголовки."""

    def __init__(self, sock):
        self.sock = sock

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock._timeout()
        return self.sock._socket.recv_into(buffer)

    def close(self):
        if not self.closed:
            super().close()
            self.sock._release_file()


class _DeadlineSocket:
    """http.client не даёт slow-drip продлевать общий срок ответа."""

    def __init__(self, sock, deadline, read_timeout):
        self._socket, self._deadline, self._read_timeout = sock, deadline, read_timeout
        self._files, self._closed = 0, False

    def _timeout(self):
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("control request deadline exceeded")
        self._socket.settimeout(min(left, self._read_timeout))

    def makefile(self, mode):
        self._files += 1
        return io.BufferedReader(_DeadlineReader(self))

    def _release_file(self):
        self._files -= 1
        if self._closed and not self._files:
            self._socket.close()

    def sendall(self, data):
        self._timeout()
        self._socket.sendall(data)

    def close(self):
        # Как socket.makefile: HTTP/1.0 может закрыть connection, пока body
        # ещё читает его файл. Последний владелец закрывает настоящий socket.
        self._closed = True
        if not self._files:
            self._socket.close()


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """TCP-адреса и TLS handshake делят один абсолютный срок подключения."""

    def __init__(self, host, port, *, context, timeout, deadline):
        super().__init__(host, port, context=context, timeout=timeout)
        self._connect_deadline = min(deadline, time.monotonic() + timeout)
        self._create_connection = self._connect_tcp

    def _remaining(self):
        left = self._connect_deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("control connection deadline exceeded")
        return left

    def _connect_tcp(self, address, timeout, source_address=None):
        host, port = address
        last_error = None
        for family, kind, protocol, _, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            remaining = self._remaining()
            sock = None
            try:
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(remaining)
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                if sock is not None:
                    sock.close()
                last_error = error
        if last_error is not None:
            raise last_error
        raise OSError("control host has no usable addresses")

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock.settimeout(self._remaining())
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tunnel_host or self.host,
            do_handshake_on_connect=False)
        self.sock.settimeout(self._remaining())
        self.sock.do_handshake()


class Client:
    """Синхронный token-клиент без ambient proxy/netrc/cookie и авто-retry.

    Каждый запрос владеет своим HTTPSConnection/response и закрывает оба.
    DNS использует системный resolver; его собственные сроки задаёт ОС.
    Сертификат проверяется всегда, CA выбирается только явным контекстом.
    """

    def __init__(self, context: ServerContext, *, limits: Limits = Limits()):
        _require(type(context) is ServerContext and type(limits) is Limits)
        self._context, self._limits = context, limits
        failed = False
        try:
            self._tls = ssl.create_default_context(cafile=context.ca_file)
        except (OSError, ValueError, ssl.SSLError):
            failed = True
        if failed:
            raise ClientError("invalid TLS trust configuration")
        self._tls.minimum_version = ssl.TLSVersion.TLSv1_2

    def prepare_restart(self, target: Target, *, epoch: str, not_after: str,
                        request_id: str | None = None, fresh: bool = False) -> PreparedCommand:
        return PreparedCommand("restart", target, epoch, str(uuid4()) if request_id is None else request_id,
                               not_after, fresh=fresh)

    def prepare_send(self, target: Target, *, epoch: str, not_after: str, message: str,
                     request_id: str | None = None, priority: str = "next", wait_seconds: int = 0,
                     notify_when_idle: bool = False) -> PreparedCommand:
        return PreparedCommand("send", target, epoch, str(uuid4()) if request_id is None else request_id,
                               not_after, message=message, priority=priority,
                               wait_seconds=wait_seconds, notify_when_idle=notify_when_idle)

    def prepare_node_drain(self, node_id: str, *, epoch: str, expected_revision: str,
                           not_after: str, request_id: str | None = None) -> NodeCommand:
        return NodeCommand("drain", node_id, epoch, str(uuid4()) if request_id is None else request_id,
                           not_after, expected_revision)

    def prepare_node_enable(self, node_id: str, *, epoch: str, expected_revision: str,
                            not_after: str, request_id: str | None = None) -> NodeCommand:
        return NodeCommand("enable", node_id, epoch, str(uuid4()) if request_id is None else request_id,
                           not_after, expected_revision)

    def _exchange(self, method, path, body=None, *, command=None, budget=None):
        limits = self._limits
        _require(body is None or len(body) <= limits.max_request_bytes)
        deadline = time.monotonic() + min(limits.request_timeout, budget or limits.request_timeout)
        url = urlsplit(self._context.origin)
        connection = response = None
        possible_post = failed = False
        headers = {"Authorization": "Token " + self._context.token, "Accept": "application/json",
                   "Accept-Encoding": "identity", "Cache-Control": "no-store"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("control request deadline exceeded")
            connection = _DeadlineHTTPSConnection(
                url.hostname, url.port or 443, context=self._tls,
                timeout=min(limits.connect_timeout, remaining), deadline=deadline)
            connection.debuglevel = 0
            connection.connect()
            connection.sock = _DeadlineSocket(connection.sock, deadline, limits.read_timeout)
            possible_post = method == "POST"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            # Никаких redirect, в том числе same-origin и POST -> GET.
            if 300 <= status < 400:
                raise ProtocolError("control API redirects are not allowed")
            if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise ProtocolError("unsupported response encoding")
            media = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if media != "application/json":
                raise ProtocolError("invalid control API response media type")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or len(length) > 10 or
                                       int(length) > limits.max_response_bytes):
                raise ProtocolError("control API response exceeds size limit")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("control request deadline exceeded")
                chunk = response.read1(min(16384, limits.max_response_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limits.max_response_bytes:
                    raise ProtocolError("control API response exceeds size limit")
            if length is not None and size != int(length):
                raise ProtocolError("truncated control API response")
            retry_header = response.getheader("Retry-After", "")
            retry_after = int(retry_header) if retry_header.isdecimal() and len(retry_header) <= 6 else None
            raw = b"".join(chunks)
        except (OSError, http.client.HTTPException, TimeoutError, ValueError):
            failed = True
        finally:
            # Даже отказ одного close не лишает второго владельца cleanup и
            # не выносит сырое исключение адаптера в diagnostics.
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except (OSError, http.client.HTTPException):
                        failed = True
        # Вне except: __context__ тоже не удерживает сырое исключение с секретами.
        if failed:
            if command is not None and possible_post:
                raise UnknownSubmission(command)
            raise TransportError("control API transport unavailable")
        data = _json(raw)
        if not 200 <= status < 300:
            error = _api_error(data, status, retry_after)
            if command is not None and error.error.code == 'database_outcome_unknown':
                raise UnknownSubmission(command)
            raise error
        return status, data

    def _get(self, path, *, budget=None):
        status, data = self._exchange("GET", path, budget=budget)
        if status != 200:
            raise ProtocolError("unexpected control API query status")
        return data

    def submit(self, command: PreparedCommand | NodeCommand) -> Submission:
        _require(type(command) in (PreparedCommand, NodeCommand))
        if type(command) is NodeCommand:
            path = f"/api/v1/nodes/{command.node_id}/commands/{command.action}"
            target_id, incarnation = command.node_id, None
        else:
            path = (f"/api/v1/puppets/{command.target.id}/commands/restart" if command.action == "restart"
                    else "/api/v1/messages")
            target_id, incarnation = command.target.id, command.target.incarnation
        # Слишком большой локальный ввод — ещё не неизвестный admission.
        body = command.body
        _require(len(body) <= self._limits.max_request_bytes)
        failed = False
        try:
            status, data = self._exchange("POST", path, body, command=command)
            if status not in (200, 202):
                raise ProtocolError("unexpected control API submission status")
            result = self._read_operation(data)
            if (result.request_id != command.request_id or result.epoch != command.epoch or
                    result.target_id != target_id or result.incarnation != incarnation):
                raise ProtocolError("operation binding mismatch")
        except ProtocolError:
            failed = True
        if failed:
            raise UnknownSubmission(command)
        return Submission(status, result)

    @staticmethod
    def _read_operation(data):
        if not isinstance(data.get("operation"), Mapping) or not isinstance(data.get("epoch"), str):
            raise ProtocolError("missing operation response")
        return _operation(data["operation"], data["epoch"])

    def operation(self, operation_id: str) -> Operation:
        result = self._read_operation(self._get("/api/v1/operations/" + _identity(operation_id)))
        if result.id != operation_id:
            raise ProtocolError("operation resource identity mismatch")
        return result

    def lookup_operation(self, request_id: str, *, epoch: str | None = None) -> Operation:
        params = {"request_id": _key(request_id)}
        if epoch is not None:
            params["epoch"] = _key(epoch)
        result = self._read_operation(self._get("/api/v1/operations?" + urlencode(params)))
        if result.request_id != request_id or epoch is not None and result.epoch != epoch:
            raise ProtocolError("operation request identity mismatch")
        return result

    def identity(self) -> Identity:
        data = self._get("/api/v1/identity")
        failed = False
        try:
            _key(data["principal_id"])
            _require(type(data["user_id"]) is int and data["user_id"] > 0)
            _require(data["kind"] in ("human", "grant", "service"))
            for name in ("node_id", "grant_id"):
                if data[name] is not None:
                    _key(data[name])
            owner = data["human_owner_id"]
            _require(owner is None or type(owner) is int and owner > 0)
            actions = data.get("action_permissions")
            _require(actions is None or isinstance(actions, tuple) and all(
                isinstance(action, str) and re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", action)
                for action in actions))
            result = Identity(data["user_id"], data["principal_id"], data["kind"], owner,
                data["node_id"], data["grant_id"], None if actions is None else frozenset(actions))
        except (KeyError, TypeError, ClientError):
            failed = True
        if failed:
            raise ProtocolError("invalid identity response")
        return result

    def health(self) -> Health:
        data = self._get("/api/v1/health")
        failed = False
        try:
            _key(data["epoch"])
            _require(data["state"] == "ready" and type(data["effects_enabled"]) is bool)
            result = Health(data["epoch"], data["state"], data["effects_enabled"])
        except (KeyError, ClientError):
            failed = True
        if failed:
            raise ProtocolError("invalid health response")
        return result

    @staticmethod
    def _address(data):
        for name in ('id', 'session_id', 'project_id'):
            _key(data[name])
        _require(isinstance(data['address'], str) and data['address'].startswith('private:'))
        _key(data['address'][8:])
        for name, limit in (('external_session_id', 256), ('display_name', 128)):
            _require(isinstance(data[name], str) and 0 < len(data[name]) <= limit)
        expires = datetime.fromisoformat(data['expires_at'].replace('Z', '+00:00'))
        _require(expires.utcoffset() is not None and data['state'] in ('registered', 'expired') and
                 type(data['live']) is bool)
        return Address(data['id'], data['session_id'], data['address'], data['project_id'],
            data['external_session_id'], data['display_name'], expires, data['state'], data['live'])

    def addresses(self, project_id: str, *, limit: int = 100) -> AddressPage:
        _key(project_id)
        _require(type(limit) is int and 1 <= limit <= 500)
        data = self._get('/api/v1/addresses?' + urlencode({'schema_version': 1,
            'project_id': project_id, 'limit': limit}))
        failed = False
        try:
            rows = data['addresses']
            _require(isinstance(rows, tuple) and len(rows) <= limit)
            values = tuple(self._address(row) for row in rows)
            _require(all(row.project_id == project_id for row in values))
            result = AddressPage(values)
        except (KeyError, TypeError, ValueError, AttributeError, ClientError):
            failed = True
        if failed:
            raise ProtocolError('invalid address page')
        return result

    def address(self, endpoint_id: str) -> Address:
        data = self._get('/api/v1/addresses/' + _key(endpoint_id))
        failed = False
        try:
            result = self._address(data['endpoint'])
            _require(result.id == endpoint_id)
        except (KeyError, TypeError, ValueError, AttributeError, ClientError):
            failed = True
        if failed:
            raise ProtocolError('invalid address response')
        return result

    def projects_page(self, *, cursor: str | None = None, page_size: int = 100) -> ProjectPage:
        data = self._get("/api/v1/projects?" + self._page_query(page_size, cursor))
        failed = False
        try:
            rows, following = data["results"], data["next_cursor"]
            _require(isinstance(rows, tuple) and len(rows) <= page_size)
            if following is not None:
                _cursor(following)
            result = ProjectPage(tuple(self._project(row) for row in rows), following)
        except (KeyError, TypeError, ClientError):
            failed = True
        if failed:
            raise ProtocolError("invalid project page")
        return result

    @staticmethod
    def _project(data):
        _key(data["id"])
        _revision(data["revision"])
        _require(all(isinstance(data[name], str) and data[name] for name in ("name", "origin")))
        limit = data["puppet_limit"]
        _require(limit is None or type(limit) is int and limit >= 0)
        return Project(data["id"], data["name"], data["origin"], data["revision"], limit)

    def project(self, project_id: str) -> Project:
        data = self._get("/api/v1/projects/" + _key(project_id))
        failed = False
        try:
            result = self._project(data)
            _require(result.id == project_id)
        except (KeyError, TypeError, ClientError):
            failed = True
        if failed:
            raise ProtocolError("invalid project response")
        return result

    @staticmethod
    def _snapshot(data):
        pending = data.get("pending_operations", ())
        if not isinstance(pending, tuple):
            raise ProtocolError("invalid pending operations")
        return Snapshot(1, data["epoch"], data, tuple(_operation(op, data["epoch"]) for op in pending))

    @staticmethod
    def _page_query(page_size, cursor=None, project_id=None):
        _require(type(page_size) is int and 1 <= page_size <= 500)
        params = {"page_size": page_size}
        if cursor is not None:
            params["cursor"] = _cursor(cursor)
        if project_id is not None:
            params["project_id"] = _identity(project_id)
        return urlencode(params)

    def snapshot(self, *, project_id: str | None = None, cursor: str | None = None,
                 page_size: int = 100) -> Snapshot:
        return self._snapshot(self._get("/api/v1/snapshot?" + self._page_query(page_size, cursor, project_id)))

    def puppet(self, puppet_id: str) -> Snapshot:
        return self._snapshot(self._get("/api/v1/puppets/" + _identity(puppet_id)))

    def operations_page(self, *, cursor: str | None = None, page_size: int = 100) -> Page:
        data = self._get("/api/v1/operations?" + self._page_query(page_size, cursor))
        results, following = data.get("results"), data.get("next_cursor")
        if not isinstance(results, tuple) or len(results) > page_size or "next_cursor" not in data:
            raise ProtocolError("invalid operation page")
        if following is not None:
            failed = False
            try:
                _cursor(following)
            except ClientError:
                failed = True
            if failed:
                raise ProtocolError("invalid page cursor")
        return Page(1, data["epoch"], tuple(_operation(op, data["epoch"]) for op in results), following)

    def _pages(self, fetch, binding):
        cursor, identity, seen = None, None, set()
        for _ in range(self._limits.max_pages):
            page = fetch(cursor)
            current, following = binding(page)
            if identity is not None and current != identity:
                raise ResetRequired(ErrorSnapshot(1, page.epoch, 409, "reset_required", "snapshot binding changed"))
            identity = current
            if following is not None:
                invalid = False
                try:
                    _cursor(following)
                except ClientError:
                    invalid = True
                if invalid or following in seen:
                    raise ProtocolError("invalid or repeated control API page cursor")
                seen.add(following)
            yield page
            cursor = following
            if cursor is None:
                return
        raise ProtocolError("control API page limit exceeded")

    def operation_pages(self, *, page_size: int = 100) -> Iterator[Page]:
        return self._pages(lambda cursor: self.operations_page(cursor=cursor, page_size=page_size),
                           lambda page: ((page.epoch,), page.next_cursor))

    def snapshot_pages(self, *, project_id: str | None = None, page_size: int = 100) -> Iterator[Snapshot]:
        def binding(page):
            snapshot_id, position = page.data.get("snapshot_id"), page.data.get("cursor")
            if not isinstance(snapshot_id, str) or not snapshot_id or not isinstance(position, str) or not position:
                raise ProtocolError("missing snapshot page binding")
            if "next_cursor" not in page.data:
                raise ProtocolError("missing snapshot page cursor")
            return (page.epoch, snapshot_id, position), page.data["next_cursor"]
        return self._pages(lambda cursor: self.snapshot(project_id=project_id, cursor=cursor, page_size=page_size),
                           binding)

    def wait_operation(self, operation_id: str, *, timeout: float | None = None,
                       poll_interval: float = 0.25, initial: Operation | None = None) -> Operation:
        _identity(operation_id)
        _require(initial is None or type(initial) is Operation and initial.id == operation_id)
        timeout = self._limits.wait_timeout if timeout is None else timeout
        _seconds(timeout, self._limits.wait_timeout)
        _seconds(poll_interval, self._limits.wait_timeout)
        deadline, last = time.monotonic() + timeout, initial
        if last is not None and last.terminal:
            return last
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise WaitTimeout(last)
            wait_expired = False
            try:
                data = self._get("/api/v1/operations/" + operation_id, budget=left)
            except TransportError:
                if time.monotonic() < deadline:
                    raise
                wait_expired = True
            if wait_expired:
                raise WaitTimeout(last)
            current = self._read_operation(data)
            if current.id != operation_id:
                raise ProtocolError("operation resource identity mismatch")
            if last is not None and current.epoch != last.epoch:
                raise ResetRequired(ErrorSnapshot(1, current.epoch, 409, "reset_required", "installation epoch changed"))
            if last is not None and (current.request_id, current.target_id, current.incarnation) != (
                    last.request_id, last.target_id, last.incarnation):
                raise ProtocolError('operation wait binding changed')
            last = current
            if last.terminal:
                return last
            left = deadline - time.monotonic()
            if left <= 0:
                raise WaitTimeout(last)
            time.sleep(min(poll_interval, left))
