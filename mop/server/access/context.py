"""Framework-free ссылки на identity; permissions всегда читаются заново."""
from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class PrincipalKind(str, Enum):
    HUMAN = 'human'
    SERVICE = 'service'
    GRANT = 'grant'


@dataclass(frozen=True)
class ActorContext:
    user_id: int
    principal_id: UUID
    kind: PrincipalKind
    credential_id: UUID
    human_owner_id: int | None
    node_id: UUID | None
    grant_id: UUID | None = None
