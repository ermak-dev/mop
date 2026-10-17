"""Framework-free ссылки на identity; permissions всегда читаются заново."""
from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class ActorKind(str, Enum):
    """Vocabulary самого Actor (#683): вид классифицирует, прав не даёт.

    Делегирование — не вид: машины делегирования это service Actor с
    заполненным grant_id; резолвер делегирования выбирается по grant_id.
    """
    HUMAN = 'human'
    SERVICE = 'service'
    AGENT = 'agent'


@dataclass(frozen=True)
class ActorContext:
    user_id: int
    kind: ActorKind
    credential_id: UUID
    human_owner_id: int | None
    node_id: UUID | None
    grant_id: UUID | None = None
