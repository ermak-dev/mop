"""Типизированный реестр отказов границы #691: один источник code -> status
-> retryable для глобального views.exception_handler.

Записи конечны и названы типом домена, а не строкой из исключения: это не
универсальный JSON-RPC error framework. Приватных per-view мапперов
(endpointviews/machineviews/usageviews/projectionviews) больше нет.
Незарегистрированный тип или подменённый код Refused записи не получают:
verdict возвращает None, DRF делает re-raise, и клиент видит generic 500
Django, а причина остаётся в серверном логе.
"""
from dataclasses import dataclass
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError
from mop.server.access.services import Denied, NotVisible
from mop.server.operations.service import Refused, _MESSAGES
from mop.server.sessions import endpoints as delivery
from mop.server.sessions import services as execution
from mop.server.sessions import usage_services as usage
from mop.server.sessions.projection_writes import SourceConflict
from mop.server.sessions.projections import SnapshotCapacity, SnapshotReset
from .authbackend import AuthenticationUnavailable


@dataclass(frozen=True)
class Entry:
    """Строка реестра: что граница отвечает на зарегистрированный отказ.

    message=None -- клиенту уходит str(error): доменный отказ уже несёт
    безопасную причину. Строка -- фиксированный текст вместо причины,
    которую выпускать нельзя (драйвер БД, имя отсутствующего объекта).
    reset -- верхнеуровневый признак {'reset': {'required': true}}.
    """
    status: int
    code: str
    retryable: bool
    retry_after: str | None = None
    message: str | None = None
    reset: bool = False


class ReadAuthorityUnavailable(RuntimeError):
    """Уже безопасный отказ foundation, не raw exception внешнего adapter."""


class ReadRefusal(Exception):
    """Отказ read-семейства вместе с эпохой, прочитанной до отказа.

    Эпоха не выдумывается: None означает, что отказала сама authority, и
    конверт поле epoch опускает, как прежний маппер projectionviews.
    """

    def __init__(self, error, epoch):
        self.error = error
        self.epoch = epoch
        super().__init__(str(error))


def _refused(code):
    # Статус кода живёт в домене (Refused.status); граница добавляет только
    # retryable и Retry-After, второй копии формулы статуса нет.
    status = Refused(code).status
    return Entry(status, code, status in (429, 503), '1' if status == 429 else None)


# Ключ -- код домена операций: запись ищется по error.code, а не берётся из
# error.status, поэтому подменённый код не находит записи и уходит в 500.
REFUSED = {code: _refused(code) for code in _MESSAGES}

# Доменные отказы delivery/execution/usage/read: одинаковы на любом входе.
# Типы не наследуют друг друга (проверка реестра), порядок вердикт не меняет.
TYPED = (
    (delivery.Capacity, Entry(429, 'delivery_capacity', True, retry_after='5')),
    (delivery.Unavailable, Entry(503, 'delivery_unavailable', True)),
    (delivery.Conflict, Entry(409, 'delivery_conflict', False)),
    (execution.Unavailable, Entry(503, 'authority_unavailable', True)),
    (execution.Conflict, Entry(409, 'execution_conflict', False)),
    (usage.Unavailable, Entry(503, 'usage_unavailable', True)),
    (ReadAuthorityUnavailable, Entry(503, 'authority_unavailable', True)),
    (SnapshotReset, Entry(410, 'reset_required', False, reset=True)),
    (SnapshotCapacity, Entry(503, 'unavailable', True)),
    (SourceConflict, Entry(409, 'incarnation_conflict', False)),
)

# Доступ: NotVisible -- подкласс Denied, поэтому стоит раньше него.
ACCESS = (
    (AuthenticationUnavailable, Entry(503, 'authority_unavailable', True)),
    (NotVisible, Entry(404, 'not_found', False, message='resource not found')),
    (ObjectDoesNotExist, Entry(404, 'not_found', False, message='resource not found')),
    (Denied, Entry(403, 'access_denied', False)),
)

# Read-страницы исторически отвечают на отказ доступа своими кодами и с
# причиной; byte-identity обязана их сохранить, поэтому это override, а не
# второй словарь доступа.
READ_OVERRIDES = (
    (NotVisible, Entry(404, 'not_found', False)),
    (Denied, Entry(403, 'permission_denied', False)),
)

# Сырой ValueError конечного парсера (exact/uid/query/protocol) -- клиентский
# ввод только там, где view объявил value_error_is_input.
INPUT = Entry(400, 'validation_error', False)

# Отказ DB, включая auth lookup, не становится anonymous/empty success.
# Неизвестный исход COMMIT не retryable: повтор мог бы применить эффект дважды.
DATABASE_UNAVAILABLE = Entry(503, 'database_unavailable', True,
                             message='controller database unavailable')
DATABASE_OUTCOME_UNKNOWN = Entry(503, 'database_outcome_unknown', False,
                                 message='database commit outcome unknown; reconcile before retry')

# Коды framework-исключений DRF по статусу; прочие -- request_refused.
FRAMEWORK_CODES = {400: 'validation_error', 401: 'authentication_required', 403: 'permission_denied',
                   404: 'not_found', 405: 'method_not_allowed', 429: 'throttled'}


def verdict(error, *, value_error_is_input=False):
    """Запись реестра для error или None (fail-closed -> generic 500)."""
    if isinstance(error, DatabaseError):
        unknown = getattr(error.__cause__, 'outcome_unknown', False)
        return DATABASE_OUTCOME_UNKNOWN if unknown else DATABASE_UNAVAILABLE
    if isinstance(error, Refused):
        # Refused -- ValueError: без этой ветки input-view смягчил бы
        # подменённый код до 400.
        return REFUSED.get(error.code)
    for kind, entry in TYPED + ACCESS:
        if isinstance(error, kind):
            return entry
    if value_error_is_input and isinstance(error, ValueError):
        return INPUT
    return None


def read_verdict(refusal):
    """Запись для отказа внутри ReadRefusal: read-override, затем общий реестр."""
    for kind, entry in READ_OVERRIDES:
        if isinstance(refusal.error, kind):
            return entry
    return verdict(refusal.error, value_error_is_input=True)
