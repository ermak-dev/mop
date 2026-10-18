"""Конечный send/dispatch handler; private bytes живут только до попытки/срока.

MessageBinding — typed input существующего Operation, не самостоятельная очередь.
AEAD I/O вне транзакции; SQL gates повторяются после него и перед выдачей bytes.
"""
from datetime import timedelta
from uuid import uuid4

from django.utils import timezone

from mop.common.endpoint import TrustedEndpointContext
from mop.server.access.services import Denied, current_actor
from . import models as m
from .commands import SendCommand, DispatchCommand, InvalidCommand
from .puppet import RestartTarget
from .requests import resolve_retry, RequestRefused, Retry
from .service import Refused, _independent, _binding, _actor, _snapshot, _event, _move
from .lifecycle import State


class MessageHandler:
    stages = ('apply_identity', 'send_message')

    def __init__(self, operations, *, cipher=None, support=None, recipients=None, endpoints=None):
        self.ops, self.cipher, self.support = operations, cipher, support
        self.recipients, self.endpoints = recipients, endpoints

    def effect_lease(self, work):
        """Прежняя 30s lease send/dispatch; initial-create budgets сюда не переходят."""
        return 30

    def _holder(self, actor, holder, project_id):
        if holder is None:
            return
        if type(holder) is not TrustedEndpointContext or self.endpoints is None:
            raise Denied('trusted current holder endpoint required')
        value = self.endpoints.holder_context(actor, holder.endpoint_id, holder.nonce, project_id=project_id)
        if value != holder:
            raise Denied('holder endpoint binding changed')

    def _authorize(self, actor, target, row=None, *, dispatch=None):
        dispatch = row.action == 'dispatch' if row is not None else dispatch
        # Вид Actor прав не даёт (#689): send/dispatch решает общий авторизатор,
        # делегирование -- пересечением цепочки; сервис права не получит.
        action = 'execution.dispatch_task' if dispatch else 'execution.send_message'
        self.ops.access.authorize(actor, action, project_ids=(target.project_id,))
        if dispatch and actor.human_owner_id != target.owner_id:
            raise Denied('dispatch belongs to another human assignment')
        if row is not None:
            pin = row.messagebinding
            if pin.holder_endpoint is not None:
                if self.endpoints is None:
                    raise Refused('controller_unavailable', safe_reason='holder endpoint authority is unavailable')
                self.endpoints.holder_context(actor, pin.holder_endpoint, pin.holder_connection,
                    project_id=target.project_id)

    def require(self, target, row):
        if self.recipients is None or self.support is None:
            raise Refused('controller_unavailable', safe_reason='physical message binding is unavailable')
        self.recipients.authorize(target, dispatch=row.action == 'dispatch')
        self.support.require(target)

    def _admission(self, actor, command, holder, digest):
        intent = command.intent
        installation = self.ops._installation(intent.epoch)
        current_actor(actor, grants=self.ops.grants)
        target = self.ops.targets.locked(intent.target_id) if self.ops.targets is not None else None
        old = m.Operation.objects.filter(actor_id=actor.user_id, request_id=intent.request_id).first()
        try:
            retry = resolve_retry(expected_epoch=intent.epoch, current_epoch=installation.epoch,
                submitted_digest=digest, stored_digest=old.digest if old else None,
                result_available=not old.result_pruned if old else True)
        except RequestRefused as error:
            raise Refused(error.code) from None
        if retry is Retry.EXPIRED:
            raise Refused('result_expired')
        if retry is Retry.REPLAY:
            return self.ops._disclose(actor, old), None, installation
        if type(target) is not RestartTarget:
            raise Denied('current assignment is unavailable')
        self.ops.view_own_puppet_operation(actor, (target.project_id,))
        dispatch = type(command) is DispatchCommand
        self._authorize(actor, target, dispatch=dispatch)
        self._holder(actor, holder, target.project_id)
        if command.notify_when_idle and holder is None:
            raise Denied('idle notification requires a trusted holder endpoint')
        if self.recipients is None or self.support is None or self.cipher is None:
            raise Refused('controller_unavailable', safe_reason='protected message delivery is unavailable')
        self.recipients.authorize(target, dispatch=dispatch)
        self.support.require(target)
        if not installation.effects_enabled:
            raise Refused('effects_disabled')
        now = timezone.now()
        if intent.not_after <= now:
            raise Refused('deadline_expired')
        if intent.not_after > now + timedelta(minutes=5):
            raise Refused('deadline_horizon')
        if intent.expected_revision != target.revision or intent.incarnation != target.incarnation_id:
            raise Refused('revision_conflict')
        self.ops._admission(installation, (target.project_id,), actor=actor, request_id=intent.request_id)
        return None, target, installation

    def submit(self, actor, command, *, holder=None):
        if type(command) not in (SendCommand, DispatchCommand):
            raise InvalidCommand(field='body')
        # Node application input имеет меньшую строгую границу, чем общий JSON body.
        if len(command.message.encode('utf-8')) > 16384 or '\x00' in command.message:
            raise InvalidCommand(field='message')
        if holder is not None and type(holder) is not TrustedEndpointContext:
            raise Denied('trusted holder endpoint required')
        _independent()
        if self.cipher is None:
            raise Refused('controller_unavailable', safe_reason='protected message cipher is unavailable')
        digest = self.cipher.digest(command, holder.endpoint_id if holder else None)
        with self.ops._transaction():
            replay, _, _ = self._admission(actor, command, holder, digest)
            if replay is not None:
                return replay
        operation_id, message_id = uuid4(), uuid4()
        # Ни plaintext, ни canonical intent не сохраняются; AEAD связан с exact IDs.
        sealed = self.cipher.seal(operation_id, message_id, command.intent.target_id, command.message.encode('utf-8'))
        with self.ops._transaction():
            replay, target, installation = self._admission(actor, command, holder, digest)
            if replay is not None:
                return replay
            intent = command.intent
            row = m.Operation.objects.create(id=operation_id, actor_id=actor.user_id, actor_binding=_binding(actor),
                request_id=intent.request_id, digest=digest, epoch=intent.epoch, restore_generation=installation.generation,
                action='dispatch' if type(command) is DispatchCommand else 'send', node_id=target.node_id,
                target_kind='puppet', target_id=target.target_id, incarnation_id=target.incarnation_id,
                expected_revision=target.revision, eligible=None, not_after=intent.not_after)
            row.projects.set((target.project_id,))
            m.RestartBinding.objects.create(operation=row, project_id=target.project_id, executor_epoch=target.executor_epoch,
                owner_id=target.owner_id, generation=target.generation, role=target.role,
                bundle_digest=target.bundle_digest, grant_id=target.grant_id)
            m.MessageBinding.objects.create(operation=row, message_id=message_id, ciphertext=sealed,
                priority=command.priority.value, wait_seconds=command.wait_seconds, notify_when_idle=command.notify_when_idle,
                holder_endpoint=holder.endpoint_id if holder else None,
                holder_connection=holder.nonce if holder else None)
            for ordinal, stage in enumerate(self.stages, 1):
                m.Step.objects.create(operation=row, ordinal=ordinal, name=stage)
            _event(row)
            result = _snapshot(row)
        return result

    def input(self, work):
        """Caller уже выполнил machine gate; после decrypt проверяем всё ещё раз."""
        _independent()
        row = m.MessageBinding.objects.select_related('operation').get(operation_id=work.operation_id)
        if not row.ciphertext or self.cipher is None:
            raise Refused('controller_unavailable', safe_reason='protected message input is unavailable')
        text = self.cipher.open(work.operation_id, row.message_id, work.target_id, bytes(row.ciphertext))
        with self.ops._transaction():
            # guard -> Assignment -> Operation: тот же порядок, что machine claim.
            self.ops._installation(work.epoch)
            self.ops.targets.locked(work.target_id)
            if self.ops.current_machine(work.operation_id, work.step_id) != work:
                raise Refused('claim_conflict')
            pin = m.MessageBinding.objects.get(operation_id=work.operation_id)
            if not pin.ciphertext:
                raise Refused('operation_conflict')
            reply = self.endpoints.holder_address(_actor(pin.operation), pin.holder_endpoint, pin.holder_connection,
                project_id=pin.operation.restartbinding.project_id) if pin.holder_endpoint else None
            return dict(message=text.decode('utf-8'), message_id=str(pin.message_id),
                operation_request_id=str(pin.operation.request_id), priority=pin.priority,
                notify_when_idle=pin.notify_when_idle, reply_address=reply,
                holder_endpoint=str(pin.holder_endpoint) if pin.holder_endpoint else None)

    def expire_payloads(self, *, limit=50):
        """TTL cleanup без объявления ещё живого эффекта остановленным."""
        _independent()
        ids = tuple(m.MessageBinding.objects.filter(operation__not_after__lte=timezone.now()).exclude(ciphertext=b'')
            .order_by('operation_id').values_list('operation_id', flat=True)[:limit])
        for identity in ids:
            with self.ops._transaction():
                self.ops._installation()
                row = m.Operation.objects.select_for_update().get(pk=identity)
                m.MessageBinding.objects.filter(operation=row).update(ciphertext=b'')
                if row.state == State.ACCEPTED:
                    _move(row, State.REFUSED, 'deadline_expired', expected_revision=row.revision)
        return len(ids)
