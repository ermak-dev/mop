"""Чистые правила переходов операции; конкурентную запись охраняет ORM CAS."""
from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


_NEXT = {
    State.ACCEPTED: frozenset({State.RUNNING, State.REFUSED, State.EXPIRED, State.CANCELLED}),
    State.RUNNING: frozenset({State.SUCCEEDED, State.REFUSED, State.FAILED,
                             State.PARTIAL, State.RECONCILING}),
    State.RECONCILING: frozenset({State.SUCCEEDED, State.FAILED, State.PARTIAL, State.UNKNOWN}),
    State.UNKNOWN: frozenset({State.RECONCILING}),
}
_SETTLED = frozenset({State.SUCCEEDED, State.REFUSED, State.FAILED,
                      State.PARTIAL, State.EXPIRED, State.CANCELLED})


@dataclass(frozen=True)
class Transition:
    state: State
    revision: int


def _state(value):
    try:
        return State(value)
    except (TypeError, ValueError):
        raise ValueError("invalid operation state") from None


def _revision(value):
    if type(value) is not int or value < 1:
        raise ValueError("operation revision must be a positive integer")
    return value


def transition(before, after, *, revision, expected_revision):
    before, after = _state(before), _state(after)
    revision, expected_revision = _revision(revision), _revision(expected_revision)
    if revision != expected_revision:
        raise ValueError("operation revision changed")
    if after not in _NEXT.get(before, ()):
        raise ValueError(f"invalid operation transition: {before.value} -> {after.value}")
    return Transition(after, revision + 1)


def may_release_resources(state, *, effect_quiescent):
    state = _state(state)
    if type(effect_quiescent) is not bool:
        raise ValueError("effect_quiescent must be a boolean")
    return state in _SETTLED and effect_quiescent
