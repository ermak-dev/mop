"""Чистая конечная политика CredentialRotation, не исполнитель и не workflow DSL.

Каждый возвращённый снимок нужно сохранить conditional update по старой
revision вместе с аудитом/outbox в короткой транзакции. Только после COMMIT
намерения (pending) допустим внешний вызов. Потерянный COMMIT сначала читают
по идентичности операции. CAS здесь проверяет снимок, не заменяет CAS в БД
и unique-ограничение конкурирующих ротаций одного секрета.

required — замороженные серверные привязки получателей, включая incarnation.
Приложение привязывает ротацию к old/new SecretVersion; проверяет полномочия,
назначение, версию, claim/попытку и incarnation каждого receipt до вызова
политики, в той же транзакции, где активирует. Строки/множества здесь — уже
проверенные факты, не токены полномочий и не пользовательские JSON-поля.

Outcome.REJECTED означает подтверждённый отказ БЕЗ эффекта, не HTTP 500.
APPLIED означает завершённый, привязанный к намерению эффект. Timeout, crash,
частичная доставка и потерянный ответ — UNKNOWN. После crash любое pending
сначала переводят в reconciling, даже если вызов, возможно, ещё не начался.
Observation.ABSENT разрешён лишь при доказанном отсутствии ВСЕГО эффекта и
остановленном старом исполнителе. Observation.PARTIAL при доказанном покое
сохраняет известные effects/receipts и открывает только явную компенсацию.
Частичное применение не даёт права повтора исходного эффекта.

Компенсация конкретна: восстановить старый credential, затем отдельно отозвать
новый. Восстановление включает безопасную адресную выдачу старой версии и её
подтверждение внешним адаптером. Это новые fallible effects, не возврат старой
revision, не откат SQL и не повторная активация отозванного grant. При отказе
сохраняются обе категории причин и PARTIAL; автоматического universal undo нет.
"""
from dataclasses import dataclass, field, replace
from enum import Enum


class RotationError(ValueError):
    """Безопасный отказ политики без значений credentials и сырых ошибок."""


class RevisionConflict(RotationError):
    """Снимок изменился: перечитать, а не повторить эффект."""


class Stage(Enum):
    STAGED = "staged"
    DELIVERING = "delivering"
    ACTIVE = "active"
    RETIRING = "retiring"
    RETIRED = "retired"
    RECONCILING = "reconciling"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    RESTORING_OLD = "restoring_old"
    OLD_RESTORED = "old_restored"
    REVOKING_NEW = "revoking_new"
    COMPENSATED = "compensated"
    PARTIAL = "partial"


class Effect(Enum):
    DELIVER = "deliver"
    RETIRE_OLD = "retire_old"
    RESTORE_OLD = "restore_old"
    REVOKE_NEW = "revoke_new"


class Outcome(Enum):
    APPLIED = "applied"
    REJECTED = "rejected_without_effect"
    UNKNOWN = "unknown"


class FailureReason(Enum):
    """Безопасная категория известного отказа/частичного эффекта, не raw error."""

    REFUSED = "refused"
    PRECONDITION = "precondition_failed"
    UNAVAILABLE = "unavailable_before_effect"
    PARTIAL = "partially_applied"


class Observation(Enum):
    APPLIED = "applied"
    ABSENT = "absent"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


_EFFECT_STAGE = {
    Effect.DELIVER: Stage.DELIVERING,
    Effect.RETIRE_OLD: Stage.RETIRING,
    Effect.RESTORE_OLD: Stage.RESTORING_OLD,
    Effect.REVOKE_NEW: Stage.REVOKING_NEW,
}
_SUCCESS_STAGE = {
    Effect.DELIVER: Stage.DELIVERING,
    Effect.RETIRE_OLD: Stage.RETIRED,
    Effect.RESTORE_OLD: Stage.OLD_RESTORED,
    Effect.REVOKE_NEW: Stage.COMPENSATED,
}
_COMPENSATIONS = frozenset({Effect.RESTORE_OLD, Effect.REVOKE_NEW})


def _recipients(value, *, required=False):
    if not isinstance(value, (set, frozenset)):
        raise RotationError("invalid rotation recipient set")
    copied = frozenset(value)
    if required and not copied:
        raise RotationError("rotation requires addressed recipients")
    if any(type(item) is not str or not item for item in copied):
        raise RotationError("invalid rotation recipient binding")
    return copied


@dataclass(frozen=True, slots=True, repr=False)
class Rotation:
    """Immutable снимок политики; хранение, auth и внешние порты остаются снаружи.

    original_failure/compensation_failure обозначают этапы отказа,
    original_reason/compensation_reason сохраняют обе конечные категории причин.
    Не принимать exception/message с plaintext в эти поля. Если приложению
    нужны подробности, оно отдельно сохраняет безопасную диагностику адаптера
    вместе со снимком, но не секрет и не исходное исключение.
    """

    required: frozenset[str] = field(repr=False)
    state: Stage = Stage.STAGED
    revision: int = 0
    receipts: frozenset[str] = field(default=frozenset(), repr=False)
    pending: Effect | None = None
    retryable: Effect | None = None
    original_failure: Effect | None = None
    compensation_failure: Effect | None = None
    original_reason: FailureReason | None = None
    compensation_reason: FailureReason | None = None

    def __post_init__(self):
        object.__setattr__(self, "required", _recipients(self.required, required=True))
        object.__setattr__(self, "receipts", _recipients(self.receipts))
        if type(self.state) is not Stage or type(self.revision) is not int or self.revision < 0:
            raise RotationError("invalid rotation snapshot")
        for value in (self.pending, self.retryable, self.original_failure, self.compensation_failure):
            if value is not None and type(value) is not Effect:
                raise RotationError("invalid rotation effect category")
        if not self.receipts <= self.required:
            raise RotationError("rotation receipt binding mismatch")
        if self.pending is not None and self.retryable is not None:
            raise RotationError("rotation effect is already pending")
        effect = self.pending or self.retryable
        if self.state is Stage.RECONCILING:
            if self.pending is None:
                raise RotationError("reconciliation requires pending intent")
        elif effect is not None and self.state is not _EFFECT_STAGE[effect]:
            raise RotationError("rotation stage does not match effect")
        for failure, reason in ((self.original_failure, self.original_reason),
                                (self.compensation_failure, self.compensation_reason)):
            if ((failure is None) != (reason is None)
                    or (reason is not None and type(reason) is not FailureReason)):
                raise RotationError("rotation failure requires a safe reason category")
        self._validate_facts(effect)

    def _validate_facts(self, effect):
        # Восстановленный ORM-снимок тоже не должен открывать небезопасный переход.
        if self.original_failure is not None and self.original_failure not in {Effect.DELIVER, Effect.RETIRE_OLD}:
            raise RotationError("invalid original rotation failure")
        if self.compensation_failure is not None and self.compensation_failure not in _COMPENSATIONS:
            raise RotationError("invalid compensation failure")
        compensating = self.state in {
            Stage.RESTORING_OLD, Stage.OLD_RESTORED, Stage.REVOKING_NEW,
            Stage.COMPENSATED, Stage.PARTIAL,
        } or (self.state is Stage.RECONCILING and effect in _COMPENSATIONS)
        needs_failure = compensating or self.state in {Stage.FAILED, Stage.RECOVERY_REQUIRED}
        if needs_failure != (self.original_failure is not None):
            raise RotationError("rotation failure history does not match stage")
        if self.state is Stage.RECOVERY_REQUIRED and self.original_reason is not FailureReason.PARTIAL:
            raise RotationError("partial recovery requires confirmed partial application")
        if (self.original_reason is FailureReason.PARTIAL
                and not (compensating or self.state is Stage.RECOVERY_REQUIRED)):
            raise RotationError("partial application requires explicit recovery")
        if (self.state is Stage.PARTIAL) != (self.compensation_failure is not None):
            raise RotationError("compensation failure history does not match stage")
        if self.state in {Stage.RESTORING_OLD, Stage.REVOKING_NEW} and effect is None:
            raise RotationError("compensation stage requires intent or proven absence")
        if self.state is Stage.RETIRING and effect is None:
            raise RotationError("retirement requires intent or proven absence")
        confirmed_delivery = (
            self.state in {Stage.ACTIVE, Stage.RETIRING, Stage.RETIRED}
            or (self.state is Stage.DELIVERING and effect is None)
            or (self.state is Stage.RECONCILING and effect is Effect.RETIRE_OLD)
            or self.original_failure is Effect.RETIRE_OLD
        )
        partial_delivery = (self.original_failure is Effect.DELIVER
                            and self.original_reason is FailureReason.PARTIAL)
        expected_receipts = self.required if confirmed_delivery else frozenset()
        if not partial_delivery and self.receipts != expected_receipts:
            raise RotationError("delivery evidence does not match rotation stage")

    def __repr__(self):
        return f"Rotation(state={self.state.value!r}, revision={self.revision})"

    @classmethod
    def stage(cls, required: frozenset[str]) -> "Rotation":
        """Новая ротация с непустым замороженным адресным набором."""
        return cls(required=required)

    def _expected(self, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise RotationError("invalid expected rotation revision")
        if expected_revision != self.revision:
            raise RevisionConflict("rotation revision changed; refresh before deciding")

    def _change(self, **changes):
        return replace(self, revision=self.revision + 1, **changes)

    def _begin(self, expected_revision, sources, effect):
        self._expected(expected_revision)
        if self.state not in sources or self.pending is not None or self.retryable is not None:
            raise RotationError("rotation effect cannot start in this stage")
        return self._change(state=_EFFECT_STAGE[effect], pending=effect)

    def begin_delivery(self, *, expected_revision: int) -> "Rotation":
        """Сохранить намерение до адресной доставки новой версии."""
        return self._begin(expected_revision, {Stage.STAGED}, Effect.DELIVER)

    def activate(self, *, expected_revision: int) -> "Rotation":
        """Активировать только после полного проверенного адресного подтверждения."""
        self._expected(expected_revision)
        if (self.state is not Stage.DELIVERING or self.pending is not None
                or self.retryable is not None or self.receipts != self.required):
            raise RotationError("activation requires all addressed delivery receipts")
        return self._change(state=Stage.ACTIVE)

    def begin_retirement(self, *, expected_revision: int) -> "Rotation":
        """Старый ключ отзывается только после активации, не по таймауту отправки."""
        return self._begin(expected_revision, {Stage.ACTIVE}, Effect.RETIRE_OLD)

    def begin_compensation(self, *, expected_revision: int) -> "Rotation":
        """Из известного отказа/частичного эффекта начать отдельное восстановление."""
        return self._begin(expected_revision, {Stage.FAILED, Stage.RECOVERY_REQUIRED}, Effect.RESTORE_OLD)

    def begin_new_revocation(self, *, expected_revision: int) -> "Rotation":
        """Отдельное намерение: новый credential не отзывается раньше восстановления."""
        return self._begin(expected_revision, {Stage.OLD_RESTORED}, Effect.REVOKE_NEW)

    def retry_effect(self, *, expected_revision: int) -> "Rotation":
        """Повтор допустим только после readback ABSENT с доказанным покоем."""
        self._expected(expected_revision)
        if self.retryable is None or self.pending is not None or self.state is Stage.RECONCILING:
            raise RotationError("rotation retry requires proven absence and quiescence")
        return self._change(pending=self.retryable, retryable=None)

    def _applied(self, receipts):
        effect = self.pending
        if effect is Effect.DELIVER:
            if receipts != self.required:
                raise RotationError("delivery confirmation requires exact addressed receipts")
        elif receipts:
            raise RotationError("unexpected delivery receipts for rotation effect")
        return self._change(state=_SUCCESS_STAGE[effect], pending=None,
                            receipts=receipts if effect is Effect.DELIVER else self.receipts)

    def record(self, *, expected_revision: int, outcome: Outcome,
               receipts: frozenset[str] = frozenset(), reason: FailureReason | None = None) -> "Rotation":
        """Подтвердить сохранённое намерение; crash/timeout оставляют его для сверки."""
        self._expected(expected_revision)
        if type(outcome) is not Outcome:
            raise RotationError("invalid rotation effect outcome")
        if reason is not None and (type(reason) is not FailureReason or outcome is not Outcome.REJECTED
                                   or reason is FailureReason.PARTIAL):
            raise RotationError("safe failure reason is only valid for a confirmed rejection without effect")
        if self.pending is None or self.state is Stage.RECONCILING:
            raise RotationError("rotation has no recordable effect; reconcile pending uncertainty")
        receipts = _recipients(receipts)
        if outcome is Outcome.APPLIED:
            return self._applied(receipts)
        if receipts:
            raise RotationError("unconfirmed effect cannot carry delivery confirmation")
        if outcome is Outcome.UNKNOWN:
            return self._change(state=Stage.RECONCILING)
        reason = reason or FailureReason.REFUSED
        if self.pending in _COMPENSATIONS:
            return self._change(state=Stage.PARTIAL, compensation_failure=self.pending,
                                compensation_reason=reason, pending=None)
        return self._change(state=Stage.FAILED, original_failure=self.pending,
                            original_reason=reason, pending=None)

    def reconcile(self, *, expected_revision: int, outcome: Observation,
                  receipts: frozenset[str] = frozenset(), quiescent: bool = False) -> "Rotation":
        """Сверить внешний факт; отсутствие всего эффекта и покой открывают отдельный retry."""
        self._expected(expected_revision)
        if self.state is not Stage.RECONCILING or self.pending is None:
            raise RotationError("rotation is not awaiting reconciliation")
        if type(outcome) is not Observation or type(quiescent) is not bool:
            raise RotationError("invalid rotation observation")
        receipts = _recipients(receipts)
        if outcome is Observation.APPLIED:
            return self._applied(receipts)
        if outcome is Observation.PARTIAL:
            if not quiescent:
                raise RotationError("partial recovery requires proven executor quiescence")
            if self.pending is not Effect.DELIVER and receipts:
                raise RotationError("unexpected delivery receipts for rotation effect")
            if self.pending in _COMPENSATIONS:
                return self._change(state=Stage.PARTIAL, compensation_failure=self.pending,
                                    compensation_reason=FailureReason.PARTIAL, pending=None)
            return self._change(state=Stage.RECOVERY_REQUIRED, original_failure=self.pending,
                                original_reason=FailureReason.PARTIAL, pending=None,
                                receipts=receipts if self.pending is Effect.DELIVER else self.receipts)
        if receipts:
            raise RotationError("uncertain or absent effect cannot carry delivery confirmation")
        if outcome is Observation.UNKNOWN:
            return self._change()
        if not quiescent:
            raise RotationError("absence requires proven executor quiescence")
        return self._change(state=_EFFECT_STAGE[self.pending], retryable=self.pending, pending=None)
