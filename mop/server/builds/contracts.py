"""Материализованные port values; никаких ORM/Root объектов через I/O boundary."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID
import hashlib


@dataclass(frozen=True)
class BuildInput:
    operation_id: UUID
    project_id: UUID
    project_name: str
    origin: str
    policy_id: UUID
    manifest: bytes
    manifest_digest: str
    source_ref: str
    source_commit: str
    source_digest: str
    mode: str
    target_nodes: tuple[UUID, ...]
    force_safety: bool
    not_after: datetime | None = None


@dataclass(frozen=True)
class TargetSnapshot:
    target_id: UUID
    incarnation_id: UUID
    node_id: UUID
    revision: int
    generation: int
    source_spec: bytes
    free: bool | None
    observed_at: datetime

    def __post_init__(self):
        if (any(type(pk) is not UUID for pk in (self.target_id, self.incarnation_id, self.node_id))
                or type(self.revision) is not int or self.revision < 1 or type(self.generation) is not int or self.generation < 1
                or type(self.source_spec) is not bytes or not 0 < len(self.source_spec) <= 262144
                or self.free is not None and type(self.free) is not bool
                or type(self.observed_at) is not datetime or self.observed_at.utcoffset() is None):
            raise ValueError('complete addressed target snapshot required')

    @property
    def spec_digest(self):
        return hashlib.sha256(self.source_spec).hexdigest()


@dataclass(frozen=True)
class ImageAvailability:
    node_id: UUID
    source_digest: str
    available: bool
    observed_at: datetime
    external_reference: str


@dataclass(frozen=True)
class BuildClaim:
    operation_id: UUID
    step_id: UUID
    token: UUID
    epoch: UUID


@dataclass(frozen=True)
class StageTicket:
    id: UUID
    key: str
    stage: str
    claim: BuildClaim
    build: BuildInput
    node_id: UUID | None = None
    target_id: UUID | None = None
    incarnation_id: UUID | None = None
    target_revision: int | None = None
    target_generation: int | None = None
    source_spec: bytes = b''
    source_spec_digest: str = ''


@dataclass(frozen=True)
class StageResult:
    outcome: str
    quiescent: bool
    reason: str

    def __post_init__(self):
        if (self.outcome not in ('succeeded', 'failed', 'unknown') or type(self.quiescent) is not bool
                or self.outcome != 'unknown' and not self.quiescent or type(self.reason) is not str
                or not 0 < len(self.reason) <= 512):
            raise ValueError('bounded typed stage result required')


class Scheduler(Protocol):
    capabilities: frozenset[str]
    def plan(self, build: BuildInput) -> tuple[TargetSnapshot, ...]: ...
    def apply(self, intent: StageTicket) -> StageResult: ...
    def inspect(self, intent: StageTicket) -> StageResult: ...


class ImageBuilder(Protocol):
    capabilities: frozenset[str]
    def availability(self, build: BuildInput, node_id: UUID) -> ImageAvailability: ...
    def apply(self, intent: StageTicket) -> StageResult: ...
    def inspect(self, intent: StageTicket) -> StageResult: ...


class NodeExecutor(Protocol):
    capabilities: frozenset[str]
    def apply(self, intent: StageTicket) -> StageResult: ...
    def inspect(self, intent: StageTicket) -> StageResult: ...
