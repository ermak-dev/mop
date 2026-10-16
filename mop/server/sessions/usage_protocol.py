"""Узкий addressed consumer port #669, не management/decrypt oracle.

ReadCapability приходит только от trusted server composition. Её издатель обязан
проверить actual version/schema и владеть ВНЕШНЕЙ границей эксклюзивного чтения:
queue и broadcast stream недоступны панели, другой установке и иным читателям.
UUID из локальной БД или операторский boolean этого не доказывают. В v8.0.16
нет remote lease/ack; без независимой access boundary acquire обязан отказать.

acquire/pop проверяют текущий installation epoch/restore guard и consumer identity
через owner APIs #669; authority после ожидания/дешифрования проверяется повторно.
Это не перенос root key или права на произвольный URL в обычный worker.
pop выполняется ровно один раз на permit, без HTTP retries/redirects, вне SQL.
Credential runtime держит management key и key-label HMAC salt, ограничивает
TLS response bytes/deadline и возвращает только DecodedBatch, не raw queue JSON.
quiescence должна адресовать старый intent после остановки ВСЕХ его возможных
HTTP попыток. Timeout/expiry/отсутствие процесса сами по себе не доказательство.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID
from .proxy_usage import DecodedBatch

PROFILE = 'cliproxy-v8.0.16-accounting-2'
MAX_COUNT = 100
from mop.common.usage_contract import COUNTERS  # noqa: F401


@dataclass(frozen=True)
class ReadCapability:
    source_id: UUID
    remote_id: UUID
    boundary_id: UUID
    profile: str
    installed_version: str
    valid_until: datetime


@dataclass(frozen=True)
class ReadPermit:
    source_id: UUID
    intent_id: UUID
    owner_id: UUID
    boundary_id: UUID
    count: int


@dataclass(frozen=True)
class Quiescence:
    source_id: UUID
    intent_id: UUID
    boundary_id: UUID
    observed_at: datetime


@dataclass(frozen=True)
class CommitResult:
    accepted: int
    duplicates: int
    conflicts: int
    rejected: int


class UsageBoundary(Protocol):
    async def acquire(self, source_id: UUID) -> ReadCapability: ...
    async def pop(self, permit: ReadPermit) -> DecodedBatch: ...
    async def quiescence(self, permit: ReadPermit) -> Quiescence: ...
