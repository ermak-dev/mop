"""Framework-free значения #641; identity источника не зависит от Access/Sessions.

Время внешнего CLI не является версией. sequence принадлежит одному producer
текущей runtime-сессии; сменить producer может только authoritative lifecycle.
"""
from dataclasses import dataclass
import json
import re
from typing import Protocol
from uuid import UUID

EVENTS = frozenset(('SessionStart', 'UserPromptSubmit', 'Stop', 'StopFailure', 'PostModelSwitch', 'Statusline'))
MAX_RECORD = 4096
MAX_BATCH = 64


def text(value, limit=128):
    return (type(value) is str and 0 < len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def positive(value):
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError('positive bounded integer required')
    return value


def uuid(value):
    if type(value) is not str:
        raise ValueError('canonical UUID required')
    result = UUID(value)
    if str(result) != value:
        raise ValueError('canonical UUID required')
    return result


def counter(value):
    if type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise ValueError('positive decimal counter required')
    return positive(int(value))


def fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError('unknown or missing telemetry fields')


@dataclass(frozen=True)
class CurrentSession:
    session_id: str
    producer_epoch: UUID

    def __post_init__(self):
        if not text(self.session_id) or type(self.producer_epoch) is not UUID:
            raise ValueError('authoritative session identity required')


@dataclass(frozen=True)
class SessionScope:
    epoch: UUID
    target_id: UUID
    incarnation_id: UUID
    node_id: UUID
    executor_epoch: UUID
    session_id: str
    producer_epoch: UUID

    def __post_init__(self):
        if not text(self.session_id) or any(type(getattr(self, key)) is not UUID
                for key in self.__dataclass_fields__ if key != 'session_id'):
            raise ValueError('typed session scope required')

    def wire(self):
        return {key: str(getattr(self, key)) for key in self.__dataclass_fields__}

    @classmethod
    def parse(cls, value):
        fields(value, cls.__dataclass_fields__)
        return cls(**{key: value[key] if key == 'session_id' else uuid(value[key]) for key in value})


@dataclass(frozen=True)
class SourceRef:
    """Неизменяемый инициатор конкретного хода, не нынешний владелец clone.

    None целиком означает unknown. Известная ссылка содержит principal, user
    (если есть), endpoint/session и operation/message correlation. Resolver
    создаёт её из trusted delivery/auth, никогда из hook JSON.
    """
    user_id: int | None
    principal_id: UUID
    endpoint_id: UUID | None
    session_id: UUID | None
    operation_id: UUID | None
    message_id: UUID | None

    def __post_init__(self):
        if self.user_id is not None:
            positive(self.user_id)
        if (type(self.principal_id) is not UUID
                or any(value is not None and type(value) is not UUID for value in
                       (self.endpoint_id, self.session_id, self.operation_id, self.message_id))
                or self.endpoint_id is None and self.session_id is None
                or self.operation_id is None and self.message_id is None):
            raise ValueError('immutable source identity and correlation required')


@dataclass(frozen=True)
class SourceAttributedTurn:
    turn_id: UUID
    initiator: SourceRef

    def __post_init__(self):
        if type(self.turn_id) is not UUID or type(self.initiator) is not SourceRef:
            raise ValueError('typed attributed turn required')


class SessionAuthority(Protocol):
    def current(self, binding) -> CurrentSession | None:
        """SQL-only trusted currentSession projection внутри caller atomic."""
        ...


class TurnAttribution(Protocol):
    def resolve(self, binding, session_id: str, turn_id: UUID) -> SourceAttributedTurn | None: ...


class ProcessedTurnBinding(Protocol):
    def bind_processed_turn(self, scope: SessionScope, turn_id: UUID) -> None:
        """Trusted processing adapter, не очередь доставки и не hook JSON."""
        ...


class PrivateHistoryReader(Protocol):
    def read(self, scope: SessionScope, cursor: str | None) -> dict: ...


@dataclass(frozen=True)
class Observation:
    scope: SessionScope
    sequence: int
    event: str
    turn_id: UUID | None
    model: str | None
    error: str | None
    causal_switch: int | None
    dropped: int

    def __post_init__(self):
        positive(self.sequence)
        if (type(self.scope) is not SessionScope or type(self.event) is not str or self.event not in EVENTS
                or self.turn_id is not None and type(self.turn_id) is not UUID
                or self.model is not None and not text(self.model)
                or self.error is not None and not text(self.error, 64)
                or type(self.dropped) is not int or not 0 <= self.dropped < 2**63
                or self.event in ('Statusline', 'PostModelSwitch') and self.model is None
                or self.event == 'StopFailure' and self.error is None
                or self.event != 'StopFailure' and self.error is not None):
            raise ValueError('invalid finite telemetry record')
        if self.causal_switch is not None:
            positive(self.causal_switch)
            if self.event != 'Statusline' or self.causal_switch >= self.sequence:
                raise ValueError('prior causal switch required')

    def wire(self):
        return dict(schema_version=1, scope=self.scope.wire(), sequence=str(self.sequence),
            event=self.event, turn_id=str(self.turn_id) if self.turn_id else None,
            model=self.model, error=self.error,
            causal_switch=str(self.causal_switch) if self.causal_switch else None, dropped=str(self.dropped))

    @classmethod
    def parse(cls, value):
        fields(value, ('schema_version', *cls.__dataclass_fields__))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('unsupported telemetry schema')
        dropped = value['dropped']
        if type(dropped) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,18}', dropped) or int(dropped) >= 2**63:
            raise ValueError('bounded drop counter required')
        return cls(SessionScope.parse(value['scope']), counter(value['sequence']), value['event'],
            uuid(value['turn_id']) if value['turn_id'] is not None else None, value['model'], value['error'],
            counter(value['causal_switch']) if value['causal_switch'] is not None else None, int(dropped))


@dataclass(frozen=True)
class Ack:
    producer_epoch: UUID
    sequence: int

    def __post_init__(self):
        positive(self.sequence)
        if type(self.producer_epoch) is not UUID:
            raise ValueError('producer epoch required')

    def wire(self):
        return dict(schema_version=1, producer_epoch=str(self.producer_epoch), sequence=str(self.sequence))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
