"""Конечный read-only AuditEvent query: whitelist, scoped SQL и keyset, не очередь."""
from dataclasses import dataclass
from datetime import datetime, timezone as utc
from uuid import UUID

from django.core import signing
from django.db.models import Exists, OuterRef, Q

from ..access.readscope import read_scope, ScopeTooWide
from ..catalog.models import NodeProject
from .models import AuditEvent, Operation


class AuditReset(ValueError):
    """Истёкший/чужой cursor или изменившаяся authority требует новой первой страницы."""


class AuditCapacity(ValueError):
    """Нельзя молча обрезать scope и выдать неполный успешный журнал."""


@dataclass(frozen=True)
class AuditTarget:
    kind: str
    id: str
    incarnation: str | None

    def wire(self):
        return {'kind':self.kind, 'id':self.id, 'incarnation':self.incarnation}


@dataclass(frozen=True)
class AuditItem:
    id: str
    operation_id: str
    revision: str
    created_at: str
    action: str
    state: str
    code: str
    reason: str
    target: AuditTarget
    node_id: str
    project_ids: tuple[str, ...]

    def wire(self):
        return {'id':self.id, 'operation_id':self.operation_id, 'revision':self.revision,
            'created_at':self.created_at, 'action':self.action, 'state':self.state,
            'code':self.code, 'reason':self.reason, 'target':self.target.wire(),
            'node_id':self.node_id, 'project_ids':list(self.project_ids)}


@dataclass(frozen=True)
class AuditPage:
    epoch: UUID
    results: tuple[AuditItem, ...]
    next_cursor: str | None

    def wire(self):
        return {'schema_version':1, 'epoch':str(self.epoch), 'results':[row.wire() for row in self.results],
                'next_cursor':self.next_cursor, 'coverage':['operation_audit']}


_SALT = 'mop.operations.audit.v1'
_FIELDS = ('id','operation_id','revision','created_at','state','code','reason','operation__action',
           'operation__target_kind','operation__target_id','operation__incarnation_id','operation__node_id')


def _time(value):
    return value.astimezone(utc.utc).isoformat().replace('+00:00', 'Z')


def _position(cursor, *, epoch, proof, limit):
    if type(cursor) is not str or not 1 <= len(cursor) <= 4096:
        raise AuditReset('invalid audit cursor; restart pagination')
    try:
        value = signing.loads(cursor, salt=_SALT, max_age=300)
        if (type(value) is not dict or set(value) != {'epoch','proof','limit','at','id'}
                or value['epoch'] != str(epoch) or value['proof'] != proof
                or type(value['limit']) is not int or value['limit'] != limit
                or type(value['id']) is not str or not value['id'].isascii()
                or not value['id'].isdecimal() or value['id'].startswith('0') or len(value['id']) > 19):
            raise ValueError()
        at = datetime.fromisoformat(value['at'])
        if at.utcoffset() is None or _time(at) != value['at']:
            raise ValueError()
        return at, int(value['id'])
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        raise AuditReset('audit cursor expired or authority changed; restart pagination') from None


def _query(scope):
    # Полный M2M scope до LIMIT. Нельзя отрезать чужие project_ids из DTO:
    # такой audit может содержать общий reason, поэтому вся строка закрыта.
    links = Operation.projects.through.objects.filter(operation_id=OuterRef('operation_id'))
    denied = links.exclude(project_id__in=scope.project_ids)
    rows = AuditEvent.objects.filter(Exists(links), ~Exists(denied), operation__node__retired_at=None)
    if scope.project_id is not None:
        rows = rows.filter(operation__projects=scope.project_id)
    # Для puppet проект — его собственный, не все соседи на общем узле.
    # Node command, напротив, затрагивает каждый текущий проект узла.
    foreign_node_projects = NodeProject.objects.filter(node_id=OuterRef('operation__node_id'),
        project__retired_at=None).exclude(project_id__in=scope.project_ids)
    rows = rows.exclude(Q(operation__target_kind='node') & ~Q(operation__action='build') & Exists(foreign_node_projects))
    if scope.node_id is not None:
        rows = rows.filter(operation__node_id=scope.node_id).exclude(operation__action='build')
        # У build node_id -- storage anchor, не доказательство single-node scope.
        # Его node-service disclosure запрещён до публичного полного build-scope port.
    if scope.target_id is not None:
        rows = rows.filter(operation__target_kind='puppet', operation__target_id=scope.target_id,
                           operation__incarnation_id=scope.incarnation)
    return rows.order_by('-created_at','-id')


class AuditQueries:
    """Epoch -- обязательный live SQL port root, без cached ControllerRun."""
    def __init__(self, *, epoch, grants=None):
        if not callable(epoch):
            raise ValueError('current installation authority reader is required')
        self.epoch, self.grants = epoch, grants

    def _epoch(self):
        value = self.epoch()
        if type(value) is not UUID:
            raise AuditCapacity('installation read authority is not bound')
        return value

    def _scope(self, actor, project_id):
        try:
            return read_scope(actor, 'operations.view_audit', project_id=project_id, grants=self.grants)
        except ScopeTooWide as error:
            raise AuditCapacity(str(error)) from None

    def read(self, actor, *, project_id=None, limit=100, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be within 1..100')
        epoch, scope = self._epoch(), self._scope(actor, project_id)
        rows = _query(scope)
        if cursor is not None:
            at, pk = _position(cursor, epoch=epoch, proof=scope.proof, limit=limit)
            rows = rows.filter(Q(created_at__lt=at) | Q(created_at=at, pk__lt=pk))
        materialized = list(rows.values(*_FIELDS)[:limit+1])
        # M2M IDs принадлежат pinned Operation, не текущим NodeProject. Второй
        # SELECT не делает их authority: основной SELECT уже выполнил anti-join,
        # а смена этих pins требует reset, никогда post-LIMIT filtering.
        projects = {}
        for operation_id, value in Operation.projects.through.objects.filter(
                operation_id__in={row['operation_id'] for row in materialized}).values_list('operation_id','project_id'):
            projects.setdefault(operation_id, []).append(value)
        values = []
        for row in materialized[:limit]:
            scope_ids = tuple(sorted(projects.get(row['operation_id'], ()), key=str))
            if not scope_ids or not set(scope_ids) <= set(scope.project_ids):
                raise AuditReset('operation scope changed; restart pagination')
            action, kind = row['operation__action'], row['operation__target_kind']
            target_id = row['operation__target_id'] if kind == 'puppet' else row['operation__node_id']
            if action == 'build':
                if len(scope_ids) != 1:
                    raise AuditCapacity('build audit project scope is unavailable')
                kind, target_id = 'project', scope_ids[0]
            values.append(AuditItem(str(row['id']), str(row['operation_id']), str(row['revision']),
                _time(row['created_at']), action, row['state'], row['code'], row['reason'],
                AuditTarget(kind, str(target_id), str(row['operation__incarnation_id']) if row['operation__incarnation_id'] else None),
                str(row['operation__node_id']), tuple(str(value) for value in scope_ids)))
        # Project membership proof не содержит соседей каждого узла. Повторяем
        # тот же SQL predicate для конечного набора (включая lookahead): смена
        # NodeProject/retirement/pins после SELECT отказывает всей странице,
        # а не прячет строку после LIMIT и не выдаёт stale node-wide reason.
        selected = {row['id'] for row in materialized}
        if selected and _query(scope).filter(pk__in=selected).count() != len(selected):
            raise AuditReset('audit resource scope changed during read; restart pagination')
        if scope != self._scope(actor, project_id) or epoch != self._epoch():
            raise AuditReset('audit authority changed during read; restart pagination')
        following = None
        if len(materialized) > limit:
            last = values[-1]
            following = signing.dumps({'epoch':str(epoch), 'proof':scope.proof, 'limit':limit,
                'at':last.created_at, 'id':last.id}, salt=_SALT, compress=True)
        return AuditPage(epoch, tuple(values), following)
