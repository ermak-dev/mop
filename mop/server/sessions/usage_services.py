"""Короткие ORM transactions и один bounded async logical queue consumer.

Никакая lease expiry не освобождает reading/unknown. Commit не подтверждает
полноту volatile upstream. Для remote exclusion нужен UsageBoundary #669.
"""
import asyncio
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
import re
from uuid import UUID, uuid4
from django.db import transaction
from django.utils import timezone
from mop.server.access.services import Access
from . import usage_models as m
from .proxy_usage import DecodedBatch, CodecError, validate_event
from .usage_protocol import (PROFILE, COUNTERS, MAX_COUNT, ReadCapability, ReadPermit,
                             Quiescence, CommitResult, UsageBoundary)


class Unavailable(RuntimeError):
    """Без сырых upstream exception/ответов и credentials."""


class Held(ValueError):
    """Старое destructive read не доказано quiescent."""


def _autocommit():
    connection = transaction.get_connection()
    if connection.in_atomic_block or not connection.get_autocommit():
        raise Unavailable('usage admission requires no surrounding transaction')


def _count(value):
    if type(value) is not int or not 1 <= value <= MAX_COUNT:
        raise ValueError('usage batch count must be within 1..100')
    return value


def _digest(batch):
    encoded = json.dumps(asdict(batch), default=str, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


class UsageStore:
    """Внутренний consumer API, не публичные mutations/CRUD источника."""
    def create_source(self, *, remote_id, boundary_id, profile, installed_version):
        if (type(remote_id) is not UUID or type(boundary_id) is not UUID
                or profile != PROFILE or installed_version != 'v8.0.16'):
            raise Unavailable('usage requires the validated tagged profile and external ownership boundary')
        # remote_id задаёт доверенная external identity, не endpoint alias caller.
        return m.UsageSource.objects.create(remote_id=remote_id, boundary_id=boundary_id,
            profile=profile, installed_version=installed_version).id

    def begin(self, capability, *, owner_id, count=100):
        _autocommit()
        _count(count)
        if type(capability) is not ReadCapability or type(owner_id) is not UUID:
            raise Unavailable('addressed external usage capability required')
        now = timezone.now()
        if (not isinstance(capability.valid_until, timezone.datetime)
                or timezone.is_naive(capability.valid_until)
                or not now < capability.valid_until <= now + timedelta(seconds=60)):
            raise Unavailable('usage capability expired or exceeds validity bound')
        with transaction.atomic():
            row = m.UsageSource.objects.select_for_update().get(pk=capability.source_id)
            if (not row.enabled or row.remote_id != capability.remote_id
                    or row.boundary_id != capability.boundary_id or row.profile != capability.profile
                    or row.installed_version != capability.installed_version
                    or row.profile != PROFILE or row.installed_version != 'v8.0.16'):
                raise Unavailable('usage profile or external ownership binding changed')
            if m.UsageReadIntent.objects.filter(source=row, state__in=('reading', 'unknown')).exists():
                raise Held('unresolved destructive read holds usage source; expiry is not quiescence')
            intent = m.UsageReadIntent.objects.create(source=row, owner_id=owner_id,
                boundary_id=row.boundary_id, count=count)
            permit = ReadPermit(row.id, intent.id, owner_id, row.boundary_id, count)
        # Объект возвращается только после настоящего COMMIT.
        return permit

    def _intent(self, permit):
        if type(permit) is not ReadPermit:
            raise Held('typed addressed read permit required')
        source = m.UsageSource.objects.select_for_update().get(pk=permit.source_id)
        intent = m.UsageReadIntent.objects.select_for_update().get(pk=permit.intent_id, source=source)
        if (intent.owner_id != permit.owner_id or intent.boundary_id != permit.boundary_id
                or source.boundary_id != permit.boundary_id or intent.count != permit.count):
            raise Held('usage read binding changed')
        return intent

    def unknown(self, permit, *, code):
        if code not in ('transport_unknown', 'invalid_batch', 'consumer_cancelled'):
            raise ValueError('unknown usage failure code')
        with transaction.atomic():
            intent = self._intent(permit)
            if intent.state in ('committed', 'gap'):
                return
            intent.state, intent.gap_code = 'unknown', code
            intent.save(update_fields=('state', 'gap_code'))

    def reconcile(self, permit, proof):
        """Только trusted addressed external quiescence, не boolean из REST."""
        _autocommit()
        now = timezone.now()
        if (type(proof) is not Quiescence or proof.source_id != permit.source_id
                or proof.intent_id != permit.intent_id or proof.boundary_id != permit.boundary_id
                or not isinstance(proof.observed_at, timezone.datetime) or timezone.is_naive(proof.observed_at)
                or not now-timedelta(seconds=30) <= proof.observed_at <= now):
            raise Held('current addressed external quiescence required')
        with transaction.atomic():
            intent = self._intent(permit)
            if intent.state not in ('reading', 'unknown') or proof.observed_at < intent.started_at:
                raise Held('read is not unresolved or proof predates it')
            intent.state, intent.gap_code, intent.finished_at = 'gap', 'read_outcome_unknown', now
            intent.save(update_fields=('state', 'gap_code', 'finished_at'))

    def _validate(self, permit, batch):
        if (type(batch) is not DecodedBatch or batch.source_complete is not False
                or type(batch.events) is not tuple or type(batch.rejected) is not int
                or batch.rejected < 0 or len(batch.events)+batch.rejected > permit.count):
            raise Unavailable('invalid bounded usage batch')
        for event in batch.events:
            try:
                validate_event(event, source_id=permit.source_id)
            except CodecError:
                raise Unavailable('invalid addressed usage event') from None

    def finish(self, permit, batch):
        self._validate(permit, batch)
        fingerprint = _digest(batch)
        with transaction.atomic():
            intent = self._intent(permit)
            if intent.state == 'committed':
                if intent.batch_fingerprint != fingerprint:
                    raise Held('read intent already committed a different batch')
                return CommitResult(intent.accepted, intent.duplicates, intent.conflicts, intent.rejected)
            if intent.state not in ('reading', 'unknown'):
                raise Held('late usage batch cannot replace reconciled evidence')
            accepted = duplicates = conflicts = 0
            for event in batch.events:
                previous = m.UsageEvent.objects.filter(source_id=permit.source_id, event_id=event.event_id).first()
                if previous:
                    if previous.fingerprint == event.fingerprint:
                        duplicates += 1
                    else:
                        conflicts += 1
                    continue
                values = {name: getattr(event, name) for name in ('event_id', 'fingerprint',
                    'occurred_at', 'provider', 'model', 'alias', 'failed', 'quality')}
                m.UsageEvent.objects.create(source_id=permit.source_id, intent=intent,
                    key_id=event.key_id or '', counters=dict(event.counters), **values)
                self._aggregate(event)
                accepted += 1
            intent.state = 'committed'
            intent.gap_code = 'identity_conflict' if conflicts else 'invalid_records' if batch.rejected else ''
            intent.batch_fingerprint, intent.finished_at = fingerprint, timezone.now()
            intent.accepted, intent.duplicates, intent.conflicts, intent.rejected = accepted, duplicates, conflicts, batch.rejected
            intent.save(update_fields=('state', 'gap_code', 'batch_fingerprint', 'finished_at',
                                       'accepted', 'duplicates', 'conflicts', 'rejected'))
            return CommitResult(accepted, duplicates, conflicts, batch.rejected)

    def _aggregate(self, event):
        # Source row lock сериализует все её aggregate writes, в том числе новые buckets.
        row, _ = m.UsageAggregate.objects.get_or_create(source_id=event.source_id,
            day=event.occurred_at.date(), key_id=event.key_id or '', provider=event.provider,
            model=event.model, alias=event.alias)
        row.requests += 1
        row.failed += int(event.failed)
        row.partial += int(event.quality != 'complete')
        for name, value in event.counters:
            if value is not None:
                row.totals[name] = row.totals.get(name, 0) + value
                row.known[name] = row.known.get(name, 0) + 1
        row.save(update_fields=('requests', 'failed', 'partial', 'totals', 'known'))


class UnboundBoundary:
    async def acquire(self, source_id):
        raise Unavailable('exclusive proxy usage boundary and actual version validation are not bound')


class UsageCollector:
    """Одна пачка в памяти и один intent; backpressure не создаёт вторую очередь."""
    def __init__(self, *, runtime, source_id, boundary: UsageBoundary | None = None, count=100):
        self.runtime, self.source_id = runtime, source_id
        self.boundary = boundary if boundary is not None else UnboundBoundary()
        self.store, self.owner_id, self.count = UsageStore(), uuid4(), _count(count)
        self._busy = asyncio.Lock()

    async def once(self):
        if self._busy.locked():
            raise Held('usage collector already owns an in-flight read')
        async with self._busy:
            # acquire не читает очередь; внешний trusted boundary проверяет remote exclusion.
            try:
                capability = await self.runtime.effect(self.boundary.acquire, self.source_id)
            except Unavailable:
                raise
            except Exception:
                raise Unavailable('proxy usage boundary validation failed') from None
            if type(capability) is not ReadCapability or capability.source_id != self.source_id:
                raise Unavailable('usage boundary returned a different source capability')
            permit = await self.runtime.database(self.store.begin, capability, owner_id=self.owner_id, count=self.count)
            try:
                batch = await self.runtime.effect(self.boundary.pop, permit)
            except asyncio.CancelledError:
                # Runtime продолжает владеть effect; source удерживается и после cancellation.
                await self.runtime.database(self.store.unknown, permit, code='consumer_cancelled')
                raise
            except Exception:
                await self.runtime.database(self.store.unknown, permit, code='transport_unknown')
                return {'state': 'unknown', 'intent_id': str(permit.intent_id), 'source_complete': False}
            try:
                result = await self.runtime.database(self.store.finish, permit, batch)
            except Unavailable:
                await self.runtime.database(self.store.unknown, permit, code='invalid_batch')
                return {'state': 'unknown', 'intent_id': str(permit.intent_id), 'source_complete': False}
            return {'state': 'committed', 'intent_id': str(permit.intent_id),
                    'source_complete': False, **asdict(result)}

    async def run(self, stop, *, interval=5):
        if type(interval) not in (int, float) or not 1 <= interval <= 60:
            raise ValueError('usage polling interval must be within 1..60 seconds')
        while not stop.is_set():
            result = await self.once()
            if result['state'] == 'unknown':
                return result
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass


class UsageQueries:
    """Публичный материализованный DTO #626/#642; unknown не нули, totals не billing."""
    def read(self, actor, *, start, end, limit=100, after=''):
        Access().authorize(actor, 'execution.view_usage')
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except (TypeError, ValueError):
            raise ValueError('usage period requires ISO dates') from None
        if not 0 < (last-first).days <= 366 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('usage query exceeds date or page bounds')
        if type(after) is not str or after and not re.fullmatch('[0-9a-f]{32}', after):
            raise ValueError('invalid usage page cursor')
        rows = m.UsageAggregate.objects.filter(day__gte=first, day__lt=last)
        if after:
            rows = rows.filter(id__gt=UUID(after))
        rows = list(rows.order_by('id')[:limit+1])
        items = []
        for row in rows[:limit]:
            items.append({'id': str(row.id), 'source_id': str(row.source_id), 'date': row.day.isoformat(),
                'key_id': row.key_id or None, 'provider': row.provider, 'model': row.model, 'alias': row.alias,
                'requests': row.requests, 'failed_requests': row.failed, 'partial_events': row.partial,
                'counters': {name: {'value': str(row.totals[name]) if name in row.totals else None,
                    'known_events': row.known.get(name, 0), 'unknown_events': row.requests-row.known.get(name, 0)}
                    for name in COUNTERS}})
        # Число источников тоже ограничено; API не выдаёт их credentials/config.
        sources = list(m.UsageSource.objects.order_by('id')[:101])
        if len(sources) > 100:
            raise Unavailable('usage source query exceeds safety bound')
        states = []
        for source in sources:
            intents = m.UsageReadIntent.objects.filter(source=source)
            latest = intents.order_by('-started_at').first()
            states.append({'id': str(source.id), 'profile': source.profile,
                'installed_version': source.installed_version, 'source_complete': False,
                'unresolved_reads': intents.filter(state__in=('reading', 'unknown')).count(),
                'gap_reads': intents.exclude(gap_code='').count(),
                'last_read_at': latest.started_at.isoformat() if latest else None,
                'status': latest.state if latest else 'not_observed'})
        Access().authorize(actor, 'execution.view_usage')
        return {'schema_version': 1, 'source': 'cliproxy', 'source_complete': False,
            'period': {'start': first.isoformat(), 'end': last.isoformat(), 'timezone': 'UTC', 'end_exclusive': True},
            'attribution': {'user': 'unavailable', 'project': 'unavailable', 'puppet': 'unavailable'},
            'sources': states, 'items': items, 'next': rows[limit-1].id.hex if len(rows) > limit else None}
