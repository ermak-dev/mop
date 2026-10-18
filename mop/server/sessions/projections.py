"""ORM queries: один SQL snapshot фактов при READ COMMITTED, без Collector/cache.

Authoritative Assignment/Catalog и correlated observations/receipts/sequence
материализуются одним statement. atomic сам по себе здесь ничего не доказывает.
Scope/epoch проверяются до и после; смена возвращает reset, не смешанную страницу.
"""
from datetime import timedelta
import json
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import models, transaction
from django.db.models import OuterRef, Subquery, Exists, F, Value, Q
from django.db.models.functions import JSONObject, Cast, Coalesce
from django.utils import timezone
from django.utils.crypto import salted_hmac
from mop.server.access.services import Denied, NotVisible
from mop.server.access.readscope import read_scope, ScopeTooWide
from .models import Assignment, IdentityApplication, ReadinessReceipt
from .projection_models import (ProjectionClock, ProjectionRuntime, SourceObservation, SourceProducer,
                                ProjectSequence, ProjectionEvent, ReadSnapshot)
from .projection_policy import KINDS, observation, project_state, safe_payload
from .projection_writes import ProjectionWriter, SourceConflict


class SnapshotReset(ValueError):
    """Нужен новый snapshot и reauthorization; пустая страница не заменяет reset."""


class SnapshotCapacity(ValueError):
    """Производный снимок превысил разрешённые границы."""


def _event_cursor(value):
    cursor = signing.dumps(value, salt='mop.read.events.v1', compress=True)
    if len(cursor) > 4096:
        raise SnapshotCapacity('snapshot scope vector exceeds wire limit; select a project')
    return cursor


def _text(value):
    return Cast(value, output_field=models.CharField())


def _observation_query(kind):
    return SourceObservation.objects.filter(target_id=OuterRef('pk'), kind=kind).annotate(item=JSONObject(
        payload=F('payload'), incarnation=_text('incarnation_id'), producer_epoch=_text('producer_epoch'),
        controller_run=_text('controller_run'), sequence=_text('sequence'),
        observed_at=F('observed_at'), received_at=F('received_at'), status=F('status'), reason=F('reason'))).values('item')[:1]


def _producer(kind):
    return Subquery(SourceProducer.objects.filter(target_id=OuterRef('pk'), kind=kind,
        incarnation_id=OuterRef('current_incarnation_id'),
        executor_epoch=OuterRef('current_incarnation__executor_epoch')).values('epoch')[:1])


def _receipt(model):
    return Exists(model.objects.filter(attempt__assignment_id=OuterRef('pk'),
        attempt__incarnation_id=OuterRef('current_incarnation_id'), attempt__generation=OuterRef('generation'),
        attempt__role=OuterRef('role'), attempt__bundle_digest=OuterRef('bundle_digest'),
        attempt__grant_id=OuterRef('grant_id'), attempt__state='succeeded'))


def _query(project_ids):
    # Ни prefetch, ни поздних lookup из projector: весь DTO и watermark видят
    # один и тот же PostgreSQL statement snapshot, включая конкурентный COMMIT.
    return Assignment.objects.filter(project_id__in=project_ids, project__retired_at__isnull=True).annotate(
        source_state=Subquery(_observation_query('state')),
        source_clone=Subquery(_observation_query('clone')),
        source_size=Subquery(_observation_query('size')),
        producer_state=_producer('state'), producer_clone=_producer('clone'), producer_size=_producer('size'),
        clock_revision=Coalesce(Subquery(ProjectionClock.objects.filter(target_id=OuterRef('pk')).values('revision')[:1]),
                                Value(0), output_field=models.BigIntegerField()),
        deleted=Coalesce(Subquery(ProjectionClock.objects.filter(target_id=OuterRef('pk')).values('retired')[:1]), Value(False)),
        project_position=Coalesce(Subquery(ProjectSequence.objects.filter(project_id=OuterRef('project_id')).values('position')[:1]),
                                  Value(0), output_field=models.BigIntegerField()),
        controller_run=Subquery(ProjectionRuntime.objects.filter(pk=1).values('run_id')[:1]),
        controller_epoch=Subquery(ProjectionRuntime.objects.filter(pk=1).values('epoch')[:1]),
        identity_applied=_receipt(IdentityApplication), ready=_receipt(ReadinessReceipt),
    ).order_by('pk').values('id','revision','generation','role','owner_id','bundle_digest','expires_at',
        'project_id','project__name','project__revision','node_id','node__name','node__revision','node__retired_at',
        'node__desired_eligible','node__applied_revision','node__applied_at','node__observed_eligible','node__observed_at',
        'current_incarnation_id','current_incarnation__executor_epoch','current_incarnation__accepting',
        'clock_revision','deleted','project_position','controller_run','controller_epoch',
        'source_state','source_clone','source_size','producer_state','producer_clone','producer_size','identity_applied','ready')


def _row(raw, now, epoch):
    incarnation = str(raw['current_incarnation_id']) if raw['current_incarnation_id'] else None
    run = str(raw['controller_run']) if raw['controller_run'] and raw['controller_epoch'] == epoch else None
    accepting = bool(raw['current_incarnation__accepting']) and raw['node__retired_at'] is None and not raw['deleted']
    facts = {kind:observation(raw['source_'+kind], kind=kind, incarnation=incarnation,
        producer_epoch=str(raw['producer_'+kind]) if raw['producer_'+kind] else None,
        run_id=run, accepting=accepting, now=now, ttl=30 if kind != 'size' else 300) for kind in KINDS}
    session, clone = facts['state'], facts['clone']
    freshness = session['freshness'] if session['reason'] != 'not_observed' else 'stale'
    state = project_state(session['value'], clone['value'] if clone['freshness'] == 'fresh' else None, freshness)
    return {'kind':'puppet', 'id':str(raw['id']), 'incarnation':incarnation,
        'revision':str(raw['revision'] + raw['project__revision'] + raw['node__revision'] + raw['clock_revision']),
        'deleted':raw['deleted'], 'name':None, 'aliases':[], 'project_id':str(raw['project_id']),
        'project_name':raw['project__name'], 'node_id':str(raw['node_id']), 'node_name':raw['node__name'],
        'desired':{'revision':str(raw['revision']), 'role':raw['role'], 'bundle_digest':raw['bundle_digest'], 'generation':str(raw['generation']),
                   'owner_id':str(raw['owner_id']) if raw['owner_id'] else None,
                   'expires_at':raw['expires_at'].isoformat()},
        'applied':{'identity':raw['identity_applied'], 'ready':raw['ready']},
        'observed':facts, 'state':state,
        'node':{'desired':{'eligible':raw['node__desired_eligible']},
                'applied':{'applied_revision':str(raw['node__applied_revision']) if raw['node__applied_revision'] else None,
                           'at':raw['node__applied_at'].isoformat() if raw['node__applied_at'] else None},
                'observed':{'eligible':raw['node__observed_eligible'],
                            'at':raw['node__observed_at'].isoformat() if raw['node__observed_at'] else None,
                            'freshness':'unverified', 'reason':'source_version_unavailable'}},
        'usage':{'source':'proxy', 'freshness':'unavailable', 'reason':'usage_query_not_bound'}}


class PoolQueries:
    """Общий service для DRF/Admin. Память/файлы/старый web не источник данных."""
    def __init__(self, *, epoch, grants=None, max_rows=5000, max_bytes=2*1024*1024):
        if not callable(epoch):
            raise ValueError('current installation authority reader is required')
        if not 1 <= max_rows <= 5000 or not 1024 <= max_bytes <= 2*1024*1024:
            raise ValueError('invalid snapshot bounds')
        self.epoch, self.grants = epoch, grants
        self.max_rows, self.max_bytes = max_rows, max_bytes

    def _epoch(self):
        value = self.epoch()
        if type(value) is not UUID:
            raise SourceConflict('installation read authority is not bound')
        return value

    def _scope(self, actor, project_id=None):
        try:
            return read_scope(actor, 'catalog.view_pool', project_id=project_id,
                              grants=self.grants, max_projects=5000)
        except ScopeTooWide:
            raise SnapshotCapacity('snapshot project bound exceeded; select a project') from None

    @staticmethod
    def _rows(authority):
        projects = (authority.project_id,) if authority.project_id else authority.project_ids
        rows = _query(projects)
        if authority.node_id is not None:
            rows = rows.filter(node_id=authority.node_id)
        if authority.target_id is not None:
            rows = rows.filter(pk=authority.target_id, current_incarnation_id=authority.incarnation)
        return rows

    def _collect(self, actor, project_id=None, target_id=None):
        epoch, authority = self._epoch(), self._scope(actor, project_id)
        rows = self._rows(authority)
        if target_id is not None:
            rows = rows.filter(pk=target_id)
        materialized = list(rows[:self.max_rows+1])
        if len(materialized) > self.max_rows:
            raise SnapshotCapacity('snapshot target bound exceeded; select a project')
        if epoch != self._epoch() or authority != self._scope(actor, project_id):
            raise SnapshotReset('authority changed while materializing snapshot')
        now = timezone.now()
        values = [_row(row, now, epoch) for row in materialized]
        if len(json.dumps(values, separators=(',',':')).encode()) > self.max_bytes:
            raise SnapshotCapacity('snapshot byte bound exceeded; select a project')
        projects = (authority.project_id,) if authority.project_id else authority.project_ids
        positions = {str(project):'0' for project in projects}
        for row in materialized:
            positions[str(row['project_id'])] = str(row['project_position'])
        runs = {row['controller_run'] for row in materialized}
        run_id = next(iter(runs)) if runs else ProjectionRuntime.objects.filter(pk=1).values_list('run_id', flat=True).first()
        return epoch, authority, values, positions, run_id

    def visible_ids(self, actor):
        """Материализованный scoped список для framework Admin adapter."""
        _, _, rows, _, _ = self._collect(actor)
        return tuple(UUID(row['id']) for row in rows if not row['deleted'])

    def detail(self, actor, target_id):
        epoch, _, rows, _, _ = self._collect(actor, target_id=target_id)
        if not rows:
            raise NotVisible('resource not found')
        return {'schema_version':1, 'epoch':str(epoch), 'puppet':rows[0]}

    def _binding(self, actor, authority):
        return salted_hmac('mop.read.binding.v1', str(actor.credential_id)+':'+str(actor.user_id),
                           algorithm='sha256').hexdigest(), authority.proof

    def snapshot(self, actor, *, project_id=None, page_size=100, cursor=None):
        if type(page_size) is not int or not 1 <= page_size <= 500:
            raise ValueError('page_size must be within 1..500')
        try:
            authority = self._scope(actor, project_id)
        except Denied as error:
            if cursor is not None:
                raise SnapshotReset('snapshot authority changed: '+str(error)) from error
            raise
        binding, scope = self._binding(actor, authority)
        epoch = self._epoch()
        if cursor is not None:
            if type(cursor) is not str or len(cursor) > 4096:
                raise SnapshotReset('invalid snapshot cursor')
            try:
                page = signing.loads(cursor, salt='mop.read.page.v1', max_age=300)
                saved = ReadSnapshot.objects.filter(pk=UUID(page['id']), user_id=actor.user_id,
                    binding=binding, scope=scope, epoch=epoch, expires_at__gt=timezone.now()).first()
                offset = page['offset']
                if saved is None or type(offset) is not int or not 0 <= offset < len(saved.rows) or page['size'] != page_size:
                    raise SnapshotReset('snapshot scope or cursor changed')
                current_run = ProjectionRuntime.objects.filter(pk=1).values_list('run_id', flat=True).first()
                if saved.run_id != current_run:
                    raise SnapshotReset('controller run changed')
            except (signing.BadSignature, KeyError, ValueError, TypeError):
                raise SnapshotReset('snapshot scope or cursor changed') from None
        else:
            epoch, authority, rows, positions, run_id = self._collect(actor, project_id)
            binding, scope = self._binding(actor, authority)
            event_cursor = _event_cursor({'epoch':str(epoch), 'binding':binding, 'scope':scope,
                                          'positions':positions})
            # Одна страница вообще не пишет в БД. Только многостраничный GET
            # резервирует bounded производный snapshot, не меняя business state.
            saved = ReadSnapshot(user_id=actor.user_id, binding=binding, scope=scope,
                epoch=epoch, run_id=run_id, cursor=event_cursor,
                rows=[row for row in rows if not row['deleted']], expires_at=timezone.now()+timedelta(seconds=300))
            if len(saved.rows) > page_size:
                # Lock User ограничивает concurrent allocations одного credential.
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=actor.user_id)
                    if ReadSnapshot.objects.filter(user_id=actor.user_id, expires_at__gt=timezone.now()).count() >= 8:
                        raise SnapshotCapacity('active snapshot limit reached')
                    saved.save(force_insert=True)
            offset = 0
        if authority != self._scope(actor, project_id) or epoch != self._epoch():
            raise SnapshotReset('snapshot authority changed')
        following = None
        if offset + page_size < len(saved.rows):
            following = signing.dumps({'id':str(saved.id), 'offset':offset+page_size, 'size':page_size},
                                      salt='mop.read.page.v1', compress=True)
        return {'schema_version':1, 'epoch':str(epoch), 'snapshot_id':str(saved.id), 'cursor':saved.cursor,
            'next_cursor':following, 'results':saved.rows[offset:offset+page_size],
            'coverage':{'catalog':True, 'assignments':True, 'observations':True,
                        'operation_publication':False, 'pending_operations':False,
                        'endpoints':False, 'telemetry':False, 'usage':False},
            'handshake_complete':False}

    def events_after(self, actor, cursor, *, project_id=None, limit=100):
        """Durable invalidations этого publication owner, не обещание всех Ops events."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('event limit must be within 1..500')
        epoch, authority = self._epoch(), self._scope(actor, project_id)
        binding, scope = self._binding(actor, authority)
        projects = (authority.project_id,) if authority.project_id else authority.project_ids
        if type(cursor) is not str or len(cursor) > 4096:
            raise SnapshotReset('invalid event cursor')
        try:
            data = signing.loads(cursor, salt='mop.read.events.v1', max_age=86400)
            positions = data['positions']
            if (data['epoch'] != str(epoch) or data['binding'] != binding or data['scope'] != scope
                    or type(positions) is not dict or set(positions) != {str(value) for value in projects}
                    or any(type(value) is not str or not value.isascii() or not value.isdecimal()
                           or len(value) > 19 for value in positions.values())):
                raise SnapshotReset('event cursor scope changed')
        except (signing.BadSignature, ValueError, TypeError, KeyError):
            raise SnapshotReset('event cursor scope changed') from None
        predicate = Q(pk__in=[])
        for project in projects:
            predicate |= Q(project_id=project, position__gt=int(positions[str(project)]))
        events = ProjectionEvent.objects.filter(predicate, epoch=epoch)
        if authority.node_id is not None:
            events = events.filter(target_id__in=Assignment.objects.filter(node_id=authority.node_id).values('pk'))
        if authority.target_id is not None:
            events = events.filter(target_id=authority.target_id, incarnation_id=authority.incarnation)
        rows = list(events.order_by('project_id','position').values(
            'id','project_id','position','target_id','incarnation_id','revision','deleted')[:limit])
        if authority != self._scope(actor, project_id) or epoch != self._epoch():
            raise SnapshotReset('event authority changed')
        result = []
        for row in rows:
            positions[str(row['project_id'])] = str(row['position'])
            result.append({'schema_version':1, 'event_id':str(row['id']), 'epoch':str(epoch),
                'project_id':str(row['project_id']), 'type':'puppet.deleted' if row['deleted'] else 'puppet.changed',
                'entity':{'kind':'puppet', 'id':str(row['target_id']),
                          'incarnation':str(row['incarnation_id']) if row['incarnation_id'] else None,
                          'revision':str(row['revision']), 'deleted':row['deleted']}})
        data['positions'] = positions
        following = _event_cursor(data)
        return {'schema_version':1, 'epoch':str(epoch), 'cursor':following, 'results':result,
                'coverage':['observations','explicit_owner_publication']}

    @staticmethod
    def prune_snapshots(*, before, limit=100):
        """Явное обслуживание производного cache, не побочный эффект GET."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('bounded snapshot maintenance required')
        ids = tuple(ReadSnapshot.objects.filter(expires_at__lte=before).order_by('expires_at').values_list('pk', flat=True)[:limit])
        return ReadSnapshot.objects.filter(pk__in=ids).delete()[0]
