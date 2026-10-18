"""Типизированные immutable границы Preparation; без ORM и внешнего I/O."""
from dataclasses import dataclass, field
from datetime import datetime
import re
from uuid import UUID

from mop.common.bootstrap_envelope import RevisionContent


@dataclass(frozen=True)
class RunBinding:
    run_id: UUID
    approval_id: UUID
    revision_id: UUID
    project_id: UUID
    target_id: UUID
    incarnation: UUID
    digest: str

    def __post_init__(self):
        if (any(type(getattr(self, key)) is not UUID for key in (
                'run_id', 'approval_id', 'revision_id', 'project_id', 'target_id', 'incarnation'))
                or not isinstance(self.digest, str) or not re.fullmatch('[0-9a-f]{64}', self.digest)):
            raise ValueError('complete bootstrap run binding required')


@dataclass(frozen=True)
class RevisionInfo:
    id: UUID
    project_id: UUID
    owner_id: int
    number: int
    content_version_id: UUID
    digest: str
    task_size: int
    vars_size: int


@dataclass(frozen=True)
class ApprovalInfo:
    id: UUID
    revision_id: UUID
    human_id: int
    created_at: datetime


@dataclass(frozen=True)
class RunInfo:
    binding: RunBinding
    version: int
    state: str
    reason: str
    failures: int
    failure_limit: int
    deadline: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def id(self):
        return self.binding.run_id


@dataclass(frozen=True)
class StepInfo:
    id: UUID
    ordinal: int
    name: str
    state: str
    attempt: int
    failures: int
    log: str = field(repr=False)
    progress: int
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class Readback:
    confirmed: bool
    run: RunInfo | None
