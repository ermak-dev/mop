"""Конечный node eligibility command; wire не выбирает callback или adapter."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from .commands import InvalidCommand, _deadline, _revision, _uuid


@dataclass(frozen=True)
class NodeEligibility:
    epoch: UUID
    request_id: UUID
    node_id: UUID
    expected_revision: int
    not_after: datetime
    eligible: bool

    def __post_init__(self):
        if (any(type(value) is not UUID for value in (self.epoch, self.request_id, self.node_id))
                or type(self.expected_revision) is not int or not 1 <= self.expected_revision < 2 ** 63
                or type(self.not_after) is not datetime or self.not_after.tzinfo is not timezone.utc
                or type(self.eligible) is not bool):
            raise InvalidCommand()

    @property
    def action(self):
        return 'enable' if self.eligible else 'drain'

    def digest(self):
        value = {'schema_version': 1, 'epoch': str(self.epoch), 'node_id': str(self.node_id),
                 'action': self.action, 'expected_revision': str(self.expected_revision),
                 'not_after': self.not_after.isoformat(timespec='microseconds')}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def parse(node_id, body, *, eligible):
    expected = {'schema_version', 'epoch', 'request_id', 'expected_revision', 'not_after', 'params'}
    if type(body) is not dict or set(body) != expected or type(body['params']) is not dict or body['params']:
        raise InvalidCommand('unknown_fields')
    if type(body['schema_version']) is not int or body['schema_version'] != 1:
        raise InvalidCommand('unsupported_schema')
    return NodeEligibility(_uuid(body['epoch'], 'epoch'), _uuid(body['request_id'], 'request_id'),
        _uuid(node_id, 'target_id'), _revision(body['expected_revision']), _deadline(body['not_after']), eligible)
