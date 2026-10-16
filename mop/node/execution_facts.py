"""Async finite read peer для composition #639/#643, не новый RPC listener.

Authority port проверяет service credential/enrolled node и возвращает scoped
physical snapshot. Отсутствующий inventory/source — unavailable, не пустой узел.
Read port не получает Mutation Work и не касается terminal handles.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
from typing import Protocol
import time

from mop.node.execution_adapters import BoundTarget, ProcessRunner
from mop.node.http_execution import FACT_FIELDS, Refused, Unavailable, uuid


@dataclass(frozen=True)
class FactScope:
    node_id: str
    kind: str
    targets: tuple[BoundTarget, ...]
    complete: bool


class FactAuthority(Protocol):
    async def authorize(self, node_id: str, kind: str, limit: int) -> FactScope: ...


class SupplementalFacts(Protocol):
    # Источники authoritative usage (#656) и orphan inventory (maintenance).
    # Чисто чтение; не «0», когда нет данных, и не permit удалить по empty scheduler.
    async def usage(self, target: BoundTarget) -> dict: ...
    async def junk(self, target: BoundTarget) -> dict: ...


class LocalFacts:
    def __init__(self, runner: ProcessRunner, authority: FactAuthority | None,
                 supplemental: SupplementalFacts | None = None, *, concurrency=4, timeout=15):
        if type(concurrency) is not int or not 1 <= concurrency <= 8 or not .1 <= timeout <= 60:
            raise Refused('bounded facts configuration required')
        self.runner, self.authority, self.supplemental = runner, authority, supplemental
        self._slots = asyncio.Semaphore(concurrency)
        self.timeout = timeout
        self._sequence = 0

    async def read(self, *, node_id, kind, limit=100):
        uuid(node_id)
        if type(kind) is not str or kind not in FACT_FIELDS or type(limit) is not int or not 1 <= limit <= 100:
            raise Refused('bounded named facts required')
        if self.authority is None:
            raise Unavailable('fact authority and physical inventory unavailable')
        # Deadline включает semaphore и scoped authority lookup; никакого thread,
        # живущего после timeout с уже освобождённым local concurrency slot.
        async with asyncio.timeout(self.timeout):
            async with self._slots:
                scope = await self.authority.authorize(node_id, kind, limit)
                if (type(scope) is not FactScope or scope.node_id != node_id or scope.kind != kind
                        or type(scope.complete) is not bool or type(scope.targets) is not tuple
                        or len(scope.targets) > limit):
                    raise Refused('fact scope does not match request')
                self._sequence += 1
                items = []
                complete = scope.complete
                for target in scope.targets:
                    if type(target) is not BoundTarget or target.node_id != node_id:
                        raise Refused('foreign physical fact target')
                    observed = datetime.now(timezone.utc).isoformat()
                    try:
                        values = await self._values(kind, target)
                        self._validate(kind, values)
                        state = 'fresh'
                    except Unavailable:
                        values, state, complete = {}, 'unavailable', False
                    items.append(dict(target_id=uuid(target.target_id),
                        incarnation_id=uuid(target.incarnation_id), sequence=str(self._sequence),
                        observed_at=observed, received_at=datetime.now(timezone.utc).isoformat(),
                        state=state, values=values))
                return dict(schema_version=1, node_id=node_id, kind=kind,
                            complete=complete, items=items)

    @staticmethod
    def _validate(kind, values):
        if type(values) is not dict or set(values) != FACT_FIELDS[kind]:
            raise Unavailable('incomplete finite fact source')
        for value in values.values():
            if (type(value) not in (int, str, bool)
                    or type(value) is int and not 0 <= value < 2**63
                    or type(value) is str and (len(value) > 256 or any(ord(c) < 32 for c in value))):
                raise Unavailable('invalid finite fact source')

    async def _values(self, kind, target):
        git = ('git', '-C', str(target.clone))
        if kind == 'clone':
            status = await self.runner.run((*git, 'status', '--porcelain=v1', '--untracked-files=normal'))
            branch = (await self.runner.run((*git, 'symbolic-ref', '--quiet', '--short', 'HEAD'))).strip()
            lines = status.splitlines()
            return dict(clean=not lines, branch=branch,
                untracked=sum(line.startswith('??') for line in lines),
                modified=sum(not line.startswith('??') for line in lines))
        if kind == 'states':
            dead = (await self.runner.run(('tmux', '-S', str(target.tmux_socket),
                'display-message', '-p', '-t', target.pane, '#{pane_dead}'))).strip()
            if dead not in ('0','1'):
                raise Unavailable('bound pane state unavailable')
            status = await self.runner.run((*git, 'status', '--porcelain=v1', '--untracked-files=normal'))
            return dict(running=dead == '0', dirty=bool(status.strip()), session_count=int(dead == '0'))
        if kind == 'sizes':
            result = await self.runner.run(('du', '-sb', '--', str(target.clone)))
            try:
                return dict(bytes=int(result.split()[0]))
            except (ValueError, IndexError) as error:
                raise Unavailable('clone size unavailable') from error
        if kind == 'disk':
            result = await self.runner.run(('df', '-B1', '--output=size,avail', '--', str(target.clone)))
            try:
                total, free = result.splitlines()[-1].split()
                return dict(total_bytes=int(total), free_bytes=int(free))
            except (ValueError, IndexError) as error:
                raise Unavailable('disk facts unavailable') from error
        if kind == 'health':
            started = time.monotonic()
            await self.runner.run((*git, 'rev-parse', '--git-dir'))
            return dict(healthy=True, latency_ms=int((time.monotonic()-started)*1000))
        if self.supplemental is None:
            raise Unavailable('usage or maintenance source unavailable')
        if kind == 'usage':
            return await self.supplemental.usage(target)
        if kind == 'junk':
            return await self.supplemental.junk(target)
        raise Refused('unknown finite fact kind')
