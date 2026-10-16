"""Порядок сверки повтора; текущую авторизацию и чтение ключа выполняет сервис."""
import re
from enum import StrEnum
from uuid import UUID


class Retry(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    EXPIRED = "expired"


_MESSAGES = {
    "epoch_conflict": "installation epoch changed; refresh before submitting",
    "request_conflict": "request key is already bound to different intent",
    "invalid_epoch": "request epoch must be a UUID",
    "invalid_digest": "intent digest must be canonical SHA-256",
    "invalid_result": "result availability must be a boolean",
}


class RequestRefused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


def _epoch(value):
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise RequestRefused("invalid_epoch")


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RequestRefused("invalid_digest")
    return value


def resolve_retry(*, expected_epoch, current_epoch, submitted_digest,
                  stored_digest=None, result_available=True):
    if _epoch(expected_epoch) != _epoch(current_epoch):
        raise RequestRefused("epoch_conflict")
    submitted_digest = _digest(submitted_digest)
    if type(result_available) is not bool:
        raise RequestRefused("invalid_result")
    if stored_digest is None:
        return Retry.NEW
    if _digest(stored_digest) != submitted_digest:
        raise RequestRefused("request_conflict")
    return Retry.REPLAY if result_available else Retry.EXPIRED
