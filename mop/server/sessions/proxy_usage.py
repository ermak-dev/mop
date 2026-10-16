"""Allowlist codec очереди CLIProxyAPI v8.0.16; без ORM, HTTP и attribution guesses.

Профиль подтверждён публичным source tag, не текущим deployed экземпляром.
Raw api_key/source/headers/failure body остаются только во входном буфере.
Normalized parent totals не складываются повторно с дочерними категориями.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from uuid import UUID

MAX_BATCH_BYTES = 2 * 1024 * 1024
MAX_BATCH_RECORDS = 500
_MAX_COUNTER = 2 ** 63 - 1
_FIELDS = (
    ('total', (), 'total_tokens'),
    ('unclassified', (), 'unclassified_tokens'),
    ('input', ('input',), 'total_tokens'),
    ('input_uncached', ('input',), 'uncached_tokens'),
    ('cache_read', ('input',), 'cache_read_tokens'),
    ('cache_write', ('input',), 'cache_write_tokens'),
    ('output', ('output',), 'total_tokens'),
    ('output_non_reasoning', ('output',), 'non_reasoning_tokens'),
    ('reasoning', ('output',), 'reasoning_tokens'),
)


class CodecError(ValueError):
    """Без сырых records, credential и exception context JSON parser."""


@dataclass(frozen=True)
class UsageEvent:
    source_id: UUID
    event_id: str
    fingerprint: str
    occurred_at: datetime
    key_id: str | None
    provider: str
    model: str
    alias: str
    failed: bool
    quality: str
    counters: tuple
    profile: str = 'cliproxy-v8.0.16-accounting-2'


@dataclass(frozen=True)
class DecodedBatch:
    events: tuple[UsageEvent, ...]
    rejected: int
    source_complete: bool = False


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate field')
        value[key] = item
    return value


def _nonfinite(value):
    raise ValueError('nonfinite JSON')


def _text(value, maximum, *, private=''):
    if (type(value) is not str or not 0 < len(value) <= maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or (private and private in value)):
        raise ValueError('invalid public label')
    return value


def _mac(key, domain, value):
    return hmac.digest(key, domain + b'\x00' + value, 'sha256').hex()


def _counter(value):
    if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
        raise ValueError('invalid token counter')
    return value


def _partition(values, parent, children):
    parts = tuple(values[name] for name in children)
    if values[parent] is not None and all(part is not None for part in parts):
        if sum(parts) != values[parent]:
            raise ValueError('inconsistent token partition')


def _record(record, source_id, key_salt):
    if type(record) is not dict or type(record.get('accounting_version')) is not int or record['accounting_version'] != 2:
        raise ValueError('unsupported accounting profile')
    identifier = _text(record.get('execution_id'), 128)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', identifier):
        raise ValueError('invalid execution identifier')
    raw_key = record.get('api_key')
    if type(raw_key) is not str or len(raw_key) > 8192 or any(ord(c) < 32 for c in raw_key):
        raise ValueError('invalid key identity')
    key_id = 'key_' + _mac(key_salt, b'mop.usage.client-key.v1', source_id.bytes + raw_key.encode()) if raw_key else None
    stamp = record.get('timestamp')
    if type(stamp) is not str or len(stamp) > 64:
        raise ValueError('invalid usage timestamp')
    at = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('usage timestamp requires timezone')
    at = at.astimezone(timezone.utc)
    breakdown = record.get('token_breakdown')
    if (type(breakdown) is not dict or type(breakdown.get('schema_version')) is not int
            or breakdown['schema_version'] != 2
            or breakdown.get('quality') not in ('complete', 'unclassified', 'inconsistent')):
        raise ValueError('normalized token breakdown required')
    values = {}
    for name, path, field in _FIELDS:
        section = breakdown
        for part in path:
            section = section.get(part, {})
            if type(section) is not dict:
                raise ValueError('invalid token section')
        values[name] = _counter(section[field]) if field in section else None
    _partition(values, 'input', ('input_uncached', 'cache_read', 'cache_write'))
    _partition(values, 'output', ('output_non_reasoning', 'reasoning'))
    _partition(values, 'total', ('input', 'output', 'unclassified'))
    if breakdown['quality'] == 'complete' and values['unclassified'] not in (None, 0):
        raise ValueError('complete partition contains unclassified tokens')
    quality = 'partial' if any(value is None for value in values.values()) else breakdown['quality']
    provider = _text(record.get('provider'), 128, private=raw_key)
    model = _text(record.get('model'), 256, private=raw_key)
    alias = _text(record.get('alias') or model, 256, private=raw_key)
    failed = record.get('failed')
    if type(failed) is not bool:
        raise ValueError('invalid failure classification')
    event_id = hashlib.sha256(b'mop.usage.execution.v1\x00' + source_id.bytes + identifier.encode()).hexdigest()
    counters = tuple(values.items())
    canonical = json.dumps([str(source_id), event_id, at.isoformat(), key_id, provider, model,
        alias, failed, quality, counters], ensure_ascii=True, separators=(',', ':')).encode()
    fingerprint = hashlib.sha256(canonical).hexdigest()
    return UsageEvent(source_id, event_id, fingerprint, at, key_id, provider, model, alias,
                      failed, quality, counters)


def decode_batch(body, *, source_id, key_salt):
    if (type(body) is not bytes or len(body) > MAX_BATCH_BYTES or type(source_id) is not UUID
            or type(key_salt) is not bytes or not 32 <= len(key_salt) <= 65536):
        raise CodecError('invalid usage batch input')
    parsed = None
    try:
        parsed = json.loads(body, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        pass
    if type(parsed) is not list or len(parsed) > MAX_BATCH_RECORDS:
        raise CodecError('invalid usage batch envelope')
    events, rejected = [], 0
    for item in parsed:
        try:
            event = _record(item, source_id, key_salt)
        except (ValueError, TypeError, OverflowError, KeyError):
            rejected += 1
        else:
            events.append(event)
    return DecodedBatch(tuple(events), rejected)
