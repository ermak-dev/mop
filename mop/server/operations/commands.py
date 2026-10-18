"""Конечные command values; auth, current epoch и допуск срока принадлежат сервису.

SendCommand — transient ввод: до сохранения handler заменяет message защищённой
версией секрета. Ни command/asdict, ни canonical plaintext не идут в audit/outbox.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import hmac
import json
import math
import re
from uuid import UUID

MAX_BODY_BYTES = 65536
_MAX_REVISION = 10 ** 128 - 1
_COMMON = frozenset({"schema_version", "epoch", "request_id", "incarnation",
                     "expected_revision", "not_after", "params"})
_FIELDS = _COMMON | {"target_id", "body", "fresh", "message", "priority",
                     "wait_seconds", "notify_when_idle", "digest_key",
                     "project_id", "node_id", "definition_id"}
# Создание (#643): цели ещё нет -- ни incarnation, ни expected_revision; target,
# инкарнацию и владельца назначает сервер, не тело запроса.
_CREATE = frozenset({"schema_version", "epoch", "request_id", "not_after", "params"})
_MESSAGES = {
    "invalid_field": "invalid command input",
    "unknown_fields": "unknown command fields are not allowed",
    "unsupported_schema": "unsupported command schema version",
    "invalid_json": "invalid JSON command body",
    "duplicate_fields": "duplicate JSON input fields are not allowed",
    "body_limit": "command body exceeds size limit",
}


class InvalidCommand(ValueError):
    def __init__(self, code="invalid_field", *, field=None):
        self.code = code if type(code) is str and code in _MESSAGES else "invalid_field"
        self.field = field if type(field) is str and field in _FIELDS else None
        message = _MESSAGES[self.code]
        super().__init__(f"{message}: {self.field}" if self.field else message)


def _require(condition, field):
    if not condition:
        raise InvalidCommand(field=field)


def _shape(value, allowed, required, field):
    _require(type(value) is dict and all(type(key) is str for key in value), field)
    if value.keys() - allowed:
        raise InvalidCommand("unknown_fields")
    _require(not required - value.keys(), field)


def _uuid(value, field):
    result = None
    if type(value) is UUID:
        return value
    if type(value) is str:
        try:
            result = UUID(value)
        except ValueError:
            pass
    _require(result is not None, field)
    return result


def _revision(value):
    _require(type(value) is str and re.fullmatch(r"0|[1-9][0-9]{0,127}", value) is not None,
             "expected_revision")
    return int(value)


def _deadline(value):
    _require(type(value) is str and re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value) is not None, "not_after")
    result = None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    _require(result is not None, "not_after")
    return result


class Priority(StrEnum):
    NOW = "now"
    NEXT = "next"
    LATER = "later"


@dataclass(frozen=True)
class TargetIntent:
    epoch: UUID
    request_id: UUID
    target_id: UUID
    incarnation: UUID
    expected_revision: int
    not_after: datetime

    def __post_init__(self):
        for name in ("epoch", "request_id", "target_id", "incarnation"):
            _require(type(getattr(self, name)) is UUID, name)
        _require(type(self.expected_revision) is int and 0 <= self.expected_revision <= _MAX_REVISION,
                 "expected_revision")
        _require(type(self.not_after) is datetime and type(self.not_after.tzinfo) is timezone
                 and self.not_after.utcoffset().total_seconds() == 0, "not_after")


@dataclass(frozen=True)
class RestartCommand:
    intent: TargetIntent
    fresh: bool

    def __post_init__(self):
        _require(type(self.intent) is TargetIntent, "body")
        _require(type(self.fresh) is bool, "fresh")


@dataclass(frozen=True)
class StopJobCommand:
    intent: TargetIntent

    def __post_init__(self):
        _require(type(self.intent) is TargetIntent, "body")


def parse_stop_job(target_id, body):
    _shape(body, _COMMON, _COMMON, "body")
    _shape(body["params"], set(), set(), "params")
    return StopJobCommand(_intent(target_id, body))


@dataclass(frozen=True)
class DeleteCommand:
    """Удаление цели (#625): без force и параметров -- чужую работу защищает право."""
    intent: TargetIntent

    def __post_init__(self):
        _require(type(self.intent) is TargetIntent, "body")


def parse_delete(target_id, body):
    _shape(body, _COMMON, _COMMON, "body")
    _shape(body["params"], set(), set(), "params")
    return DeleteCommand(_intent(target_id, body))


@dataclass(frozen=True)
class WipeCommand:
    """Сброс клона цели; force и пути не параметры — чужую работу защищает право."""
    intent: TargetIntent

    def __post_init__(self):
        _require(type(self.intent) is TargetIntent, "body")


def parse_wipe(target_id, body):
    _shape(body, _COMMON, _COMMON, "body")
    _shape(body["params"], set(), set(), "params")
    return WipeCommand(_intent(target_id, body))


@dataclass(frozen=True)
class SendCommand:
    intent: TargetIntent
    message: str = field(repr=False)
    priority: Priority = Priority.NEXT
    wait_seconds: int = 0
    notify_when_idle: bool = False

    def __post_init__(self):
        _require(type(self.intent) is TargetIntent, "body")
        valid_message = False
        if type(self.message) is str and self.message.strip():
            try:
                valid_message = len(self.message.encode("utf-8")) <= MAX_BODY_BYTES
            except UnicodeError:
                pass
        _require(valid_message, "message")
        _require(type(self.priority) is Priority, "priority")
        _require(type(self.wait_seconds) is int and 0 <= self.wait_seconds <= 60, "wait_seconds")
        _require(type(self.notify_when_idle) is bool, "notify_when_idle")


@dataclass(frozen=True)
class DispatchCommand(SendCommand):
    """Отдельный intent, не флаг обхода receiving policy."""


def parse_dispatch(body):
    value = parse_send(body)
    return DispatchCommand(value.intent, value.message, value.priority,
                           value.wait_seconds, value.notify_when_idle)


@dataclass(frozen=True)
class CreateIntent:
    epoch: UUID
    request_id: UUID
    project_id: UUID
    not_after: datetime

    def __post_init__(self):
        for name in ("epoch", "request_id", "project_id"):
            _require(type(getattr(self, name)) is UUID, name)
        _require(type(self.not_after) is datetime and type(self.not_after.tzinfo) is timezone
                 and self.not_after.utcoffset().total_seconds() == 0, "not_after")


@dataclass(frozen=True)
class CreateCommand:
    """Новый папет (#643): узел и одобренное определение роли; остальное -- сервера."""
    intent: CreateIntent
    node_id: UUID
    definition_id: UUID

    def __post_init__(self):
        _require(type(self.intent) is CreateIntent, "body")
        _require(type(self.node_id) is UUID, "node_id")
        _require(type(self.definition_id) is UUID, "definition_id")


def parse_create(project_id, body):
    _shape(body, _CREATE, _CREATE, "body")
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise InvalidCommand("unsupported_schema")
    params = body["params"]
    _shape(params, {"node_id", "definition_id"}, {"node_id", "definition_id"}, "params")
    intent = CreateIntent(_uuid(body["epoch"], "epoch"), _uuid(body["request_id"], "request_id"),
                          _uuid(project_id, "project_id"), _deadline(body["not_after"]))
    return CreateCommand(intent, _uuid(params["node_id"], "node_id"),
                         _uuid(params["definition_id"], "definition_id"))


def _intent(target_id, body):
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise InvalidCommand("unsupported_schema")
    return TargetIntent(_uuid(body["epoch"], "epoch"), _uuid(body["request_id"], "request_id"),
                        _uuid(target_id, "target_id"), _uuid(body["incarnation"], "incarnation"),
                        _revision(body["expected_revision"]), _deadline(body["not_after"]))


def parse_restart(target_id, body):
    _shape(body, _COMMON, _COMMON, "body")
    _shape(body["params"], {"fresh"}, {"fresh"}, "params")
    return RestartCommand(_intent(target_id, body), body["params"]["fresh"])


def parse_send(body):
    allowed = _COMMON | {"target_id"}
    _shape(body, allowed, allowed, "body")
    params = body["params"]
    _shape(params, {"message", "priority", "wait_seconds", "notify_when_idle"}, {"message"}, "params")
    priority, parsed_priority = params.get("priority", "next"), None
    if type(priority) is str:
        try:
            parsed_priority = Priority(priority)
        except ValueError:
            pass
    _require(parsed_priority is not None, "priority")
    return SendCommand(_intent(body["target_id"], body), params["message"], parsed_priority,
                       params.get("wait_seconds", 0), params.get("notify_when_idle", False))


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InvalidCommand("duplicate_fields")
        value[key] = item
    return value


def _json_number(value, *, integer=False):
    if len(value) > 128:
        raise ValueError("JSON number exceeds size limit")
    number = int(value) if integer else float(value)
    if not integer and not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _json_depth(value, depth=0):
    if depth > 32:
        raise InvalidCommand("invalid_json")
    if type(value) is dict:
        for item in value.values():
            _json_depth(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_depth(item, depth + 1)


def decode_body(raw):
    _require(type(raw) is bytes, "body")
    if len(raw) > MAX_BODY_BYTES:
        raise InvalidCommand("body_limit")
    error, value = None, None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object,
                           parse_int=lambda text: _json_number(text, integer=True),
                           parse_float=_json_number, parse_constant=lambda text: _json_number(text))
        _json_depth(value)
    except InvalidCommand as failure:
        error = failure.code
    except (ValueError, UnicodeError, RecursionError):
        error = "invalid_json"
    if error is not None:
        raise InvalidCommand(error)
    _require(type(value) is dict, "body")
    return value


def intent_digest(command, *, key):
    """HMAC скрывает угадываемый plaintext; ключ — отдельный bootstrap-purpose, не root_digest."""
    _require(type(key) is bytes and 32 <= len(key) <= 1024, "digest_key")
    _require(type(command) in (RestartCommand, SendCommand, DispatchCommand), "body")
    identity = command.intent
    params = {"fresh": command.fresh} if type(command) is RestartCommand else {
        "message": command.message, "priority": command.priority.value,
        "wait_seconds": command.wait_seconds, "notify_when_idle": command.notify_when_idle,
    }
    value = {"schema_version": 1, "action": "restart" if type(command) is RestartCommand else "dispatch" if type(command) is DispatchCommand else "send",
             "epoch": str(identity.epoch), "target_id": str(identity.target_id),
             "incarnation": str(identity.incarnation), "expected_revision": str(identity.expected_revision),
             "not_after": identity.not_after.isoformat(timespec="microseconds").replace("+00:00", "Z"),
             "params": params}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()
