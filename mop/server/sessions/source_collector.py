"""Bounded async опрос через внедрённые ports; ORM остаётся в database callback."""
import asyncio
import math
from typing import Protocol
from .projection_policy import KINDS


class SourceRefusal(ValueError):
    """Trusted adapter помечает уже безопасную библиотечную причину явно."""


class Source(Protocol):
    async def read(self, kind, ticket): ...


class ObservationSink(Protocol):
    def observed(self, kind, ticket, value): ...
    def failed(self, kind, ticket, reason): ...


class SourceCollector:
    """Отдельная lane каждого источника, без бесконечной очереди/polling из GET.

    Ticket фиксирует node/project/incarnation/producer/controller run до I/O.
    Sink повторно проверяет эти bindings и source sequence внутри короткой SQL
    функции; failure не может присвоить новому факту выдуманный producer sequence.
    Lifecycle запуска/отмены принадлежит composition root.
    """
    def __init__(self, *, source: Source, sink: ObservationSink, database,
                 concurrency=4, timeout=5):
        if (type(concurrency) is not int or not 1 <= concurrency <= 8 or type(timeout) not in (int,float)
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise ValueError('bounded source concurrency and deadline required')
        self.source, self.sink, self.database = source, sink, database
        self.concurrency, self.timeout = concurrency, timeout
        self._active = set()

    async def collect(self, kind, tickets):
        if kind not in KINDS or type(tickets) is not tuple or len(tickets) > 500:
            raise ValueError('bounded named source batch required')
        if kind in self._active:
            raise RuntimeError('source lane already has an active batch')
        self._active.add(kind)
        pending = iter(tickets)
        async def worker():
            for ticket in pending:
                reason = None
                try:
                    async with asyncio.timeout(self.timeout):
                        value = await self.source.read(kind, ticket)
                except SourceRefusal:
                    raise
                except TimeoutError:
                    reason = 'source_timeout'
                except Exception:
                    # Только raw exceptions внешнего adapter; SQL/use-case errors
                    # ниже не обрезаются и доходят до владельца с причиной.
                    reason = 'source_unavailable'
                if reason is None:
                    await self.database(self.sink.observed, kind, ticket, value)
                else:
                    await self.database(self.sink.failed, kind, ticket, reason)
        tasks = [asyncio.create_task(worker()) for _ in range(min(self.concurrency, len(tickets)))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # Не прячем безопасный отказ SQL/adapter в безликом ExceptionGroup.
            # Все соседние задачи всё равно отменяются и дренируются владельцем.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._active.remove(kind)
