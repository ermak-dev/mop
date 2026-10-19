"""Канонические retained bootstrap bytes формата 1, без ORM и I/O (#751).

Digest относится ко всему envelope, а не к tasks||variables. Этот codec не
разбирает YAML и не даёт authority: вызывающий проверяет выбранный proof.
"""
import base64
from dataclasses import dataclass, field
import hashlib
import json
import re
from uuid import UUID

# 262144/131072 bytes, 1024 Unicode scalars (до 12 ASCII bytes каждый),
# 64 hex commit, 32 canonical UUID refs и неизменные JSON keys/separators.
MAX_ENVELOPE_BYTES = 537974


class BootstrapError(ValueError):
    """Только конечные безопасные причины, без private content в сообщении."""


@dataclass(frozen=True)
class RevisionContent:
    tasks: bytes = field(repr=False)
    variables: bytes = field(repr=False)
    provenance: str = field(repr=False)
    commit: str
    secret_refs: tuple[UUID, ...]


def bounded_content(tasks, variables, provenance, commit, refs):
    if type(tasks) is not bytes or len(tasks) > 262144 or type(variables) is not bytes or len(variables) > 131072:
        raise BootstrapError('bootstrap tasks or vars exceed byte bounds')
    if (type(provenance) is not str or not 1 <= len(provenance) <= 1024
            or any(ord(c) < 32 or 0xd800 <= ord(c) <= 0xdfff for c in provenance)
            or re.search(r'://[^/]*@', provenance)
            or type(commit) is not str or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', commit)):
        raise BootstrapError('bounded provenance and exact commit required')
    if (type(refs) is not tuple or len(refs) > 32 or any(type(value) is not UUID for value in refs)
            or len(set(refs)) != len(refs)):
        raise BootstrapError('bounded unique secret references required')
    return RevisionContent(tasks, variables, provenance, commit, tuple(sorted(refs, key=str)))


def encode(content):
    """Исходная retained сериализация: ни формат, ни checksum не меняются."""
    return json.dumps({'format': 1, 'tasks': base64.b64encode(content.tasks).decode(),
        'variables': base64.b64encode(content.variables).decode(), 'provenance': content.provenance,
        'commit': content.commit, 'secret_refs': [str(value) for value in content.secret_refs]},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _binary(value, maximum):
    if type(value) is not str or len(value) > 4 * ((maximum + 2) // 3):
        raise BootstrapError('bootstrap encoded bytes exceed bounds')
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raise BootstrapError('bootstrap byte encoding is invalid') from None
    if len(raw) > maximum or base64.b64encode(raw).decode('ascii') != value:
        raise BootstrapError('bootstrap canonical byte encoding required')
    return raw


def decode(raw, digest):
    """Проверить SHA256 exact raw, затем strict canonical schema; не YAML."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ENVELOPE_BYTES:
        raise BootstrapError('bootstrap retained envelope exceeds byte bounds')
    if (type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest)
            or hashlib.sha256(raw).hexdigest() != digest):
        raise BootstrapError('bootstrap retained digest mismatch')
    try:
        data = json.loads(raw)
        if (type(data) is not dict or set(data) != {'format', 'tasks', 'variables', 'provenance', 'commit', 'secret_refs'}
                or type(data['format']) is not int or data['format'] != 1
                or type(data['secret_refs']) is not list or len(data['secret_refs']) > 32
                or any(type(value) is not str for value in data['secret_refs'])):
            raise ValueError()
        content = bounded_content(_binary(data['tasks'], 262144), _binary(data['variables'], 131072),
            data['provenance'], data['commit'], tuple(UUID(value) for value in data['secret_refs']))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BootstrapError('bootstrap retained content is invalid') from None
    # Сравнение также отказывает duplicate JSON keys, порядок refs, escapes,
    # UUID aliases, UTF-16/32 и whitespace: одного parse/re-encode недостаточно.
    if encode(content) != raw:
        raise BootstrapError('bootstrap retained canonical bytes required')
    return content


def decode_base64(value, digest):
    """Единственное wire поле finite bootstrap_body; legacy fragments нет."""
    return decode(_binary(value, MAX_ENVELOPE_BYTES), digest)
