"""Wire values собственного клиентского endpoint; никакой ambient authority."""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class TrustedEndpointContext:
    """Результат trusted gateway binding, не декодер actor/owner из JSON.

    User/token не различает разговоры. Holder/attribution передают endpoint_id и
    session_id отдельно от human owner; lease/nonce — transport fence, не login.
    Каждый use case проверяет credential, ownership и generation заново.
    """
    user_id: int
    credential_id: UUID
    session_id: UUID
    endpoint_id: UUID
    gateway_id: UUID
    generation: int
    nonce: UUID = field(repr=False)


@dataclass(frozen=True)
class EndpointPresence:
    id: UUID
    session_id: UUID
    address: str
    project_id: UUID
    external_session_id: str
    display_name: str
    expires_at: datetime
    state: str
    live: bool = False


@dataclass(frozen=True, repr=False)
class DeliveryFrame:
    id: UUID
    message_id: UUID
    external_session_id: str
    text: str
    priority: str
    notify_when_idle: bool
    reply_address: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    id: UUID
    message_id: UUID
    operation_request_id: UUID | None
    state: str
    idle: bool


@dataclass(frozen=True)
class ApplicationAcceptance:
    outcome: str


@dataclass(frozen=True, repr=False)
class SealedPayload:
    data: bytes
    key_id: str
    nonce: bytes
