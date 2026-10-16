"""Только ID сохранённой immutable policy; shell/branch/actor из JSON запрещены."""
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID
from datetime import datetime
from ..operations.commands import InvalidCommand, _shape, _uuid, _deadline, _revision, _require


def validate_source(branch, commit):
    if (type(branch) is not str or not 0 < len(branch) <= 256 or branch.startswith(('-', '/', 'refs/'))
            or branch.endswith(('/', '.')) or '..' in branch or '@{' in branch or '//' in branch
            or any(ord(c) < 33 or ord(c) == 127 or c in '~^:?*[\\\\' for c in branch)
            or any(part.startswith('.') or part.endswith('.lock') for part in branch.split('/'))
            or type(commit) is not str or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', commit)):
        raise ValueError('verified immutable image branch and commit required')


@dataclass(frozen=True)
class BuildCommand:
    epoch: UUID
    request_id: UUID
    project_id: UUID
    policy_id: UUID
    expected_revision: int
    not_after: datetime
    mode: str
    target_nodes: tuple[UUID, ...]
    force_safety: bool

    def __post_init__(self):
        if (any(type(v) is not UUID for v in (self.epoch, self.request_id, self.project_id, self.policy_id))
                or type(self.expected_revision) is not int or self.expected_revision < 1
                or type(self.force_safety) is not bool or self.mode not in ('missing', 'update', 'rebuild')
                or type(self.target_nodes) is not tuple or not 1 <= len(self.target_nodes) <= 64
                or any(type(v) is not UUID for v in self.target_nodes)
                or len(set(self.target_nodes)) != len(self.target_nodes)
                or not isinstance(self.not_after, datetime) or self.not_after.utcoffset() is None):
            raise ValueError('finite pinned build command required')

    def digest(self):
        value = dict(epoch=str(self.epoch), project_id=str(self.project_id), policy_id=str(self.policy_id),
            expected_revision=self.expected_revision, not_after=self.not_after.isoformat(), mode=self.mode,
            target_nodes=sorted(str(pk) for pk in self.target_nodes), force_safety=self.force_safety)
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def parse(project_id, body):
    fields = {'schema_version', 'epoch', 'request_id', 'expected_revision', 'not_after',
              'policy_id', 'mode', 'target_nodes', 'force_safety'}
    _shape(body, fields, fields, 'command')
    _require(type(body['schema_version']) is int and body['schema_version'] == 1, 'schema_version')
    _require(type(body['target_nodes']) is list, 'target_nodes')
    try:
        return BuildCommand(_uuid(body['epoch'], 'epoch'), _uuid(body['request_id'], 'request_id'),
            _uuid(str(project_id), 'project_id'), _uuid(body['policy_id'], 'policy_id'),
            _revision(body['expected_revision']), _deadline(body['not_after']), body['mode'],
            tuple(sorted((_uuid(pk, 'target_nodes') for pk in body['target_nodes']), key=str)), body['force_safety'])
    except ValueError:
        raise InvalidCommand(field='command') from None
