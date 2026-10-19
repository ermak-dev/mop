"""Wire values собственного клиентского endpoint; никакой ambient authority."""
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from uuid import UUID


@dataclass(frozen=True)
class TrustedEndpointContext:
    """Результат trusted gateway binding, не декодер actor/owner из JSON.

    User/token не различает разговоры. Holder/attribution передают endpoint_id и
    session_id отдельно от human owner; lease/nonce — transport fence, не login.
    Каждый use case проверяет credential, ownership и generation заново.
    """
    user_id: int
    credential_id: UUID
    session_id: UUID
    endpoint_id: UUID
    gateway_id: UUID
    generation: int
    nonce: UUID = field(repr=False)


@dataclass(frozen=True)
class EndpointPresence:
    id: UUID
    session_id: UUID
    address: str
    project_id: UUID
    external_session_id: str
    display_name: str
    expires_at: datetime
    state: str
    live: bool = False


@dataclass(frozen=True, repr=False)
class DeliveryFrame:
    id: UUID
    message_id: UUID
    external_session_id: str
    text: str
    priority: str
    notify_when_idle: bool
    reply_address: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    id: UUID
    message_id: UUID
    operation_request_id: UUID | None
    state: str
    idle: bool


@dataclass(frozen=True)
class ApplicationAcceptance:
    outcome: str


@dataclass(frozen=True, repr=False)
class SealedPayload:
    data: bytes
    key_id: str
    nonce: bytes


INBOX_CONTROL_BYTES = 8192
INBOX_DELIVERY_BYTES = 401408
_INBOX_CLIENT = {
    'peer_ready': ('external_session_id',),
    'bound_received': ('connection_nonce',),
    'prewrite': ('connection_nonce', 'delivery_id', 'message_id'),
    'application_acceptance': ('connection_nonce', 'delivery_id', 'message_id', 'outcome'),
    'idle_observed': ('connection_nonce', 'delivery_id', 'message_id'),
    'peer_ready_again': ('connection_nonce', 'probe_id', 'external_session_id'),
}
_INBOX_SERVER = {
    'bound': ('endpoint_id', 'external_session_id', 'epoch', 'generation', 'connection_nonce', 'lease_seconds'),
    'delivery': ('connection_nonce', 'frame'),
    'write_allowed': ('connection_nonce', 'delivery_id', 'message_id'),
    'receipt': ('connection_nonce', 'result'),
    'probe': ('connection_nonce', 'probe_id'),
}


def _inbox_uuid(value):
    if type(value) is not str:
        raise ValueError('canonical inbox UUID required')
    try:
        valid = str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('canonical inbox UUID required')


def _inbox_text(value, maximum):
    if (type(value) is not str or not value.strip() or '\x00' in value
            or len(value.encode('utf-8')) > maximum):
        raise ValueError('invalid bounded inbox text')


def _inbox_exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('exact inbox fields required')


def inbox_validate(value, *, direction=None):
    """Конечный общий wire-контракт; ни actor, ни RPC selector не принимается."""
    if type(value) is not dict or type(value.get('type')) is not str:
        raise ValueError('inbox object required')
    kinds = (_INBOX_CLIENT if direction == 'client' else _INBOX_SERVER if direction == 'server'
             else {**_INBOX_CLIENT, **_INBOX_SERVER} if direction is None else {})
    kind = value['type']
    if kind not in kinds:
        raise ValueError('invalid inbox message direction or type')
    _inbox_exact(value, ('schema_version', 'type', *kinds[kind]))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('inbox schema version must be 1')
    for name in ('connection_nonce', 'endpoint_id', 'epoch', 'delivery_id', 'message_id', 'probe_id'):
        if name in value:
            _inbox_uuid(value[name])
    if 'external_session_id' in value:
        _inbox_text(value['external_session_id'], 256)
    if 'generation' in value and (type(value['generation']) is not str
            or re.fullmatch(r'[1-9][0-9]{0,19}', value['generation']) is None):
        raise ValueError('positive canonical inbox generation required')
    if 'lease_seconds' in value and (type(value['lease_seconds']) is not int or value['lease_seconds'] != 15):
        raise ValueError('inbox lease must be 15 seconds')
    if 'outcome' in value and (type(value['outcome']) is not str
            or value['outcome'] not in ('accepted', 'unknown', 'unavailable')):
        raise ValueError('invalid inbox application evidence')
    if 'delivery_id' in value and value['delivery_id'] == value['message_id']:
        raise ValueError('distinct inbox delivery and message IDs required')
    if kind == 'delivery':
        frame = value['frame']
        _inbox_exact(frame, ('id', 'message_id', 'external_session_id', 'text', 'priority', 'notify_when_idle', 'reply_address'))
        for name in ('id', 'message_id'):
            _inbox_uuid(frame[name])
        if frame['id'] == frame['message_id']:
            raise ValueError('distinct inbox delivery and message IDs required')
        _inbox_text(frame['external_session_id'], 256)
        _inbox_text(frame['text'], 65536)
        if (type(frame['priority']) is not str or frame['priority'] not in ('now', 'next')
                or type(frame['notify_when_idle']) is not bool):
            raise ValueError('invalid inbox delivery options')
        address = frame['reply_address']
        if address is not None:
            if type(address) is not str or not address.startswith('private:'):
                raise ValueError('private inbox address required')
            _inbox_uuid(address[8:])
    if kind == 'receipt':
        result = value['result']
        _inbox_exact(result, ('id', 'message_id', 'operation_request_id', 'state', 'idle'))
        for name in ('id', 'message_id'):
            _inbox_uuid(result[name])
        if result['id'] == result['message_id']:
            raise ValueError('distinct inbox delivery and message IDs required')
        if result['operation_request_id'] is not None:
            _inbox_uuid(result['operation_request_id'])
        if (type(result['state']) is not str or result['state'] not in
                ('queued', 'attempting', 'accepted', 'unknown', 'unavailable')
                or type(result['idle']) is not bool or result['idle'] and result['state'] != 'accepted'):
            raise ValueError('invalid inbox domain receipt')
    return value


def inbox_encode(value):
    inbox_validate(value)
    wire = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    maximum = INBOX_DELIVERY_BYTES if value['type'] == 'delivery' else INBOX_CONTROL_BYTES
    if len(wire.encode('utf-8')) > maximum:
        raise ValueError('inbox frame byte bound exceeded')
    return wire


def inbox_decode(wire, *, direction):
    if type(wire) is not str or len(wire.encode('utf-8')) > (INBOX_CONTROL_BYTES if direction == 'client' else INBOX_DELIVERY_BYTES):
        raise ValueError('bounded UTF-8 inbox JSON required')
    # Глубина проверяется до json.loads: даже 8 KiB вложенных массивов не
    # должны запускать рекурсию декодера. Строки и escapes не считаются скобками.
    depth = 0
    quoted = escaped = False
    for char in wire:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > 4:
                raise ValueError('inbox JSON nesting exceeded')
        elif char in ']}':
            depth -= 1
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate inbox JSON key')
            result[key] = item
        return result
    def nonfinite(_):
        raise ValueError('nonfinite inbox JSON value')
    try:
        value = json.loads(wire, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (RecursionError, UnicodeError):
        raise ValueError('invalid inbox JSON') from None
    inbox_validate(value, direction=direction)
    if value['type'] != 'delivery' and len(wire.encode('utf-8')) > INBOX_CONTROL_BYTES:
        raise ValueError('inbox control byte bound exceeded')
    return value
