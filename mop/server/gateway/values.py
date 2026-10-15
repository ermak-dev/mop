"""Материализованные значения; credential никогда не входит в публичный repr."""
from dataclasses import dataclass, field
import hashlib
from uuid import UUID


@dataclass(frozen=True)
class Credential:
    version_id: UUID
    value: str = field(repr=False)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.value.encode()).hexdigest()


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Claim:
    state: str
    id: UUID | None = None
    revision: int = 0
    fingerprint: str = field(default='', repr=False)


def refusal(status, code):
    return Reply(status, code.encode(), (('Content-Type', 'text/plain; charset=utf-8'),))
