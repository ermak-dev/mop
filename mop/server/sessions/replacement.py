"""#625: SQL-only transitions замены; физические proofs поставляет Root.

Нет Operations ORM, scheduler/provider calls или прав из переданного JSON.
Обычное current() не видит retired source; исключение ограничено recycle wipe.
"""
from dataclasses import asdict
import hashlib
import json
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from mop.server.catalog.models import NodeProject
from . import models as m
from .protocol import AuthorityV1, ReplacementAuthority, WorkV1
from .services import Conflict, Unavailable, unbound_installation, unclaimed


class UnboundReplacement:
    def require(self, **locator):
        raise Unavailable('replacement lifecycle authority is not bound')

    def stopped(self, work):
        raise Unavailable('stopped-source wipe authority is not bound')

    def authorize(self, work):
        raise Unavailable('stopped-source wipe authority is not bound')


def binding_digest(proof):
    """Одинаковые immutable pins для stop/wipe/cutover, разные stage receipts."""
    values = asdict(proof)
    for field in ('phase', 'step_id', 'token', 'receipt_id', 'modify_index'):
        values.pop(field)
    return hashlib.sha256(json.dumps(values, default=str, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


class ReplacementExecution:
    def __init__(self, assignments, *, authority=None, installation=None):
        self.assignments = assignments
        self.port = authority if authority is not None else UnboundReplacement()
        self.installation = installation if installation is not None else unbound_installation

    def _guard(self):
        if not transaction.get_connection().in_atomic_block:
            raise Unavailable('replacement lifecycle requires caller transaction')
        value = self.installation()
        if type(value) is not AuthorityV1:
            raise Unavailable('typed replacement installation authority required')
        if not value.effects_enabled:
            raise Unavailable('installation effects are disabled')
        return value

    def _proof(self, *, operation_id, step_id, token, phase):
        state = self._guard()
        if any(type(value) is not UUID for value in (operation_id, step_id, token)):
            raise ValueError('typed replacement operation locator required')
        proof = self.port.require(operation_id=operation_id, step_id=step_id, token=token, phase=phase)
        self._validate(proof, state)
        if (proof.operation_id, proof.step_id, proof.token, proof.phase) != (operation_id, step_id, token, phase):
            raise Conflict('replacement authority returned another operation stage')
        return proof

    def _validate(self, proof, state):
        if type(proof) is not ReplacementAuthority:
            raise Unavailable('typed replacement lifecycle proof required')
        if proof.epoch != state.epoch:
            raise Conflict('replacement installation epoch changed')
        offset = 1 if proof.action == 'update' else 2
        if (proof.destination_incarnation_id == proof.source.incarnation_id
                or proof.destination_grant_id == proof.source.grant_id
                or proof.destination_generation != proof.source.generation+offset
                or proof.destination_revision != proof.source.revision+offset
                or proof.expires_at > proof.source_expires_at):
            raise Conflict('distinct pinned replacement destination required')

    def _locked(self, proof):
        source = proof.source
        row = m.Assignment.objects.select_for_update().filter(pk=source.target_id).first()
        if row is None:
            raise Conflict('replacement source assignment is unavailable')
        # Свой fence обязателен; только свой исключается из конфликтов.
        if self.assignments.target_fence(row.pk, excluding_operation=None) is not True:
            raise Conflict('replacement operation must hold target')
        unclaimed(self.assignments.target_fence, row.pk, excluding_operation=proof.operation_id)
        if (row.project_id != source.project_id or row.node_id != source.node_id
                or row.owner_id != source.owner_id or row.deleted_at is not None
                or not row.owner.is_active or row.role != source.role or row.bundle_digest != source.bundle_digest
                or not NodeProject.objects.filter(node_id=row.node_id, project_id=row.project_id).exists()):
            raise Conflict('replacement source owner, project, node or policy changed')
        incarnation = m.ExecutionIncarnation.objects.select_for_update().filter(pk=source.incarnation_id).first()
        if (incarnation is None or incarnation.assignment_id != row.pk or incarnation.node_id != row.node_id
                or incarnation.executor_epoch != source.executor_epoch or incarnation.replaced_by_epoch is not None):
            raise Conflict('replacement source executor changed')
        return row, incarnation

    def _source(self, row, incarnation, proof, *, stopped=False):
        source = proof.source
        offset = int(stopped)
        if (row.current_incarnation_id != source.incarnation_id or row.grant_id != source.grant_id
                or row.expires_at != proof.source_expires_at
                or row.revision != source.revision+offset or row.generation != source.generation+offset
                or incarnation.accepting is stopped):
            raise Conflict('replacement source incarnation or counters changed')
        if proof.phase != 'stop' and (row.expires_at <= timezone.now() or proof.expires_at <= timezone.now()):
            raise Conflict('replacement assignment expired')
        if m.AssignmentAttempt.objects.filter(assignment=row, state__in=('claimed', 'unknown')).exists():
            raise Conflict('unresolved external effect holds replacement source')

    def _previous(self, proof):
        previous = m.ReplacementReceipt.objects.filter(operation_id=proof.operation_id, phase=proof.phase).first()
        if previous is None and m.ReplacementReceipt.objects.filter(receipt_id=proof.receipt_id).exists():
            raise Conflict('replacement receipt identity already committed')
        return previous

    def _same(self, previous, proof):
        if (previous.assignment_id != proof.source.target_id
                or previous.source_incarnation_id != proof.source.incarnation_id
                or previous.binding_digest != binding_digest(proof)
                or (previous.step_id, previous.token, previous.receipt_id, previous.modify_index) !=
                   (proof.step_id, proof.token, proof.receipt_id, proof.modify_index)):
            raise Conflict('replacement receipt or immutable destination pins changed')

    def _record(self, proof, row):
        m.ReplacementReceipt.objects.create(operation_id=proof.operation_id, phase=proof.phase,
            step_id=proof.step_id, token=proof.token, receipt_id=proof.receipt_id,
            binding_digest=binding_digest(proof), assignment=row,
            source_incarnation_id=proof.source.incarnation_id, resulting_revision=row.revision,
            resulting_generation=row.generation, modify_index=proof.modify_index)

    def _stopped(self, row, incarnation, proof, *, active_attempt=False):
        previous = m.ReplacementReceipt.objects.filter(operation_id=proof.operation_id, phase='stop').first()
        if (proof.action != 'recycle' or previous is None or previous.binding_digest != binding_digest(proof)
                or previous.assignment_id != row.pk or previous.source_incarnation_id != incarnation.pk):
            raise Conflict('exact confirmed recycle stop required')
        if proof.phase == 'wipe' and (previous.receipt_id != proof.receipt_id or previous.modify_index != proof.modify_index):
            raise Conflict('stopped-source wipe requires original successful stop receipt')
        if proof.phase == 'cutover' and proof.modify_index <= previous.modify_index:
            raise Conflict('replacement registration must follow confirmed stop index')
        if active_attempt:
            # Именно свой claim допускается для addressed input/result; чужой
            # unresolved effect остаётся fence. _source ниже не видит его.
            own = dict(operation_id=proof.operation_id, step_id=proof.step_id, action='wipe_clone')
            if m.AssignmentAttempt.objects.filter(assignment=row, state__in=('claimed', 'unknown')).exclude(**own).exists():
                raise Conflict('another unresolved effect holds stopped source')
            self._source_counters(row, incarnation, proof)
        else:
            self._source(row, incarnation, proof, stopped=True)
        return previous

    def _source_counters(self, row, incarnation, proof):
        """Тот же CAS для уже claimed wipe, без снятия собственного attempt fence."""
        source = proof.source
        if (row.current_incarnation_id != source.incarnation_id or row.grant_id != source.grant_id
                or row.expires_at != proof.source_expires_at
                or row.revision != source.revision+1 or row.generation != source.generation+1
                or incarnation.accepting or row.expires_at <= timezone.now() or proof.expires_at <= timezone.now()):
            raise Conflict('stopped source admission changed')

    def stop(self, *, operation_id, step_id, token):
        proof = self._proof(operation_id=operation_id, step_id=step_id, token=token, phase='stop')
        row, incarnation = self._locked(proof)
        previous = self._previous(proof)
        if previous:
            self._same(previous, proof)
            self._source(row, incarnation, proof, stopped=True)
            return row.revision
        self._source(row, incarnation, proof)
        incarnation.accepting = False
        incarnation.revision += 1
        incarnation.save(update_fields=('accepting', 'revision'))
        row.revision += 1
        row.generation += 1
        row.save(update_fields=('revision', 'generation'))
        self._record(proof, row)
        return row.revision

    def cutover(self, *, operation_id, step_id, token):
        proof = self._proof(operation_id=operation_id, step_id=step_id, token=token, phase='cutover')
        row, incarnation = self._locked(proof)
        previous = self._previous(proof)
        if previous:
            self._same(previous, proof)
            if (row.current_incarnation_id != proof.destination_incarnation_id
                    or row.grant_id != proof.destination_grant_id or row.revision != previous.resulting_revision
                    or row.generation != previous.resulting_generation or row.expires_at != proof.expires_at):
                raise Conflict('historical replacement receipt cannot rebind current assignment')
            return self.assignments.current(project_id=row.project_id, target_id=row.pk)
        if proof.action == 'recycle':
            self._stopped(row, incarnation, proof)
        else:
            self._source(row, incarnation, proof)
        if m.ExecutionIncarnation.objects.filter(pk=proof.destination_incarnation_id).exists():
            raise Conflict('replacement destination incarnation already exists')
        destination = m.ExecutionIncarnation.objects.create(id=proof.destination_incarnation_id,
            assignment=row, node_id=row.node_id, executor_epoch=proof.source.executor_epoch)
        m.IncarnationLaunch.objects.create(incarnation=destination, **asdict(proof.launch))
        incarnation.accepting = False
        incarnation.revision += 1
        incarnation.save(update_fields=('accepting', 'revision'))
        row.current_incarnation = destination
        row.grant_id, row.expires_at = proof.destination_grant_id, proof.expires_at
        row.revision, row.generation = proof.destination_revision, proof.destination_generation
        row.save(update_fields=('current_incarnation', 'grant_id', 'expires_at', 'revision', 'generation'))
        self._record(proof, row)
        return self.assignments.current(project_id=row.project_id, target_id=row.pk)

    def authorize(self, work):
        """Только MachineExecution: old grant проверяет специальный Root port.

        Generic GrantAuthority правильно отказывает stopped current, и не
        получает здесь исключения для send/restart/обычного wipe.
        """
        state = self._guard()
        if type(work) is not WorkV1 or work.action != 'wipe_clone':
            raise Conflict('only exact stopped-source recycle wipe is allowed')
        proof = self.port.stopped(work)
        self._validate(proof, state)
        source = proof.source
        if (proof.phase != 'wipe' or (work.operation_id, work.step_id, work.epoch, work.target_id,
                work.incarnation_id, work.node_id, work.executor_epoch, work.owner_id, work.role,
                work.bundle_digest, work.grant_id, work.revision, work.generation) !=
                (proof.operation_id, proof.step_id, proof.epoch, source.target_id, source.incarnation_id,
                 source.node_id, source.executor_epoch, source.owner_id, source.role, source.bundle_digest,
                 source.grant_id, source.revision+1, source.generation+1) or work.not_after <= timezone.now()):
            raise Conflict('exact current stopped-source recycle wipe required')
        row, incarnation = self._locked(proof)
        self._stopped(row, incarnation, proof, active_attempt=True)
