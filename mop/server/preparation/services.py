"""Preparation use cases: короткий sync ORM, provider/redactor строго вне SQL.

RuntimeGate — локальный текущий auth/assignment/intent/fencing query, не I/O
adapter. Он обязан проверить полный pinned proof и assigned machine principal.
Ни grant actor, ни operation id, ни RunBinding из JSON сами не дают authority.
Worker, Ansible, node delivery, reconciliation и публичные endpoints здесь нет.
"""
import base64
from datetime import timedelta
from functools import wraps
import hashlib
import json
import re
from typing import Protocol
import uuid

from django.db import DatabaseError, transaction
from django.utils import timezone

from ..access.context import ActorContext, PrincipalKind
from ..access.delegation import ApprovedPolicy, PolicyBinding
from ..catalog.models import Project
from ..secrets.services import (RetainedSecrets, RetentionScope, outside_transaction,
                                Conflict as SecretConflict, Refused as SecretRefused)
from . import models as m
from .values import (RunBinding, RevisionInfo, RevisionContent, ApprovalInfo,
                     RunInfo, StepInfo, Readback)


class Conflict(ValueError):
    """CAS/identity или неизвестный эффект требуют readback, не повтор I/O."""


class StorageUnavailable(ValueError):
    """Без SQL parameters, ciphertext и raw exception context драйвера."""


def storage_boundary(command):
    @wraps(command)
    def call(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except DatabaseError:
            pass
        raise StorageUnavailable('bootstrap storage is unavailable')
    return call


class Authorizer(Protocol):
    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()): ...
    def authorize_owner(self, owner_id, action, *, project_ids=()): ...


class RuntimeGate(Protocol):
    def check(self, actor, binding: RunBinding, action: str) -> None: ...


class LogRedactor(Protocol):
    def sanitize(self, raw: bytes) -> str: ...


class Deny:
    def authorize(self, actor, action, **refs):
        raise PermissionError('preparation authorizer is not bound')

    def authorize_owner(self, owner_id, action, **refs):
        raise PermissionError('preparation owner authorizer is not bound')

    def check(self, actor, binding, action):
        raise PermissionError('bootstrap runtime gate is not bound')

    def sanitize(self, raw):
        raise PermissionError('bootstrap log redactor is not bound')


def revision_info(row):
    return RevisionInfo(*(getattr(row, name) for name in RevisionInfo.__dataclass_fields__))


def approval_info(row):
    return ApprovalInfo(row.id, row.revision_id, row.human_id, row.created_at)


def binding_of(row):
    revision = row.approval.revision
    return RunBinding(row.id, row.approval_id, revision.id, revision.project_id,
                      row.target_id, row.incarnation, revision.digest)


def run_info(row):
    return RunInfo(binding_of(row), row.version, row.state, row.reason, row.failures, row.failure_limit,
                   row.deadline, row.started_at, row.finished_at)


def step_info(row):
    return StepInfo(*(getattr(row, name) for name in StepInfo.__dataclass_fields__))


def exact_uuid(*values):
    if any(type(value) is not uuid.UUID for value in values):
        raise ValueError('complete UUID identity required')


def bounded_content(tasks, variables, provenance, commit, refs):
    if type(tasks) is not bytes or len(tasks) > 262144 or type(variables) is not bytes or len(variables) > 131072:
        raise ValueError('bootstrap tasks or vars exceed byte bounds')
    if (type(provenance) is not str or not 1 <= len(provenance) <= 1024
            or any(ord(c) < 32 for c in provenance) or re.search(r'://[^/]*@', provenance)
            or type(commit) is not str or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', commit)):
        raise ValueError('bounded provenance and exact commit required')
    if type(refs) is not tuple or len(refs) > 32:
        raise ValueError('bounded unique secret references required')
    exact_uuid(*refs)
    if len(set(refs)) != len(refs):
        raise ValueError('bounded unique secret references required')
    return RevisionContent(tasks, variables, provenance, commit, tuple(sorted(refs, key=str)))


def encode(content):
    return json.dumps({'format': 1, 'tasks': base64.b64encode(content.tasks).decode(),
        'variables': base64.b64encode(content.variables).decode(), 'provenance': content.provenance,
        'commit': content.commit, 'secret_refs': [str(value) for value in content.secret_refs]},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


class _RevisionAuthority:
    def __init__(self, api):
        self.api = api

    def check(self, actor, scope):
        if scope.purpose != 'policy':
            raise PermissionError('unsupported bootstrap retention purpose')
        self.api._human(actor, scope.project_id, owner_id=scope.owner_id)
        row = m.BootstrapRevision.objects.filter(pk=scope.subject_id).first()
        if row and (row.project_id, row.owner_id) != (scope.project_id, scope.owner_id):
            raise PermissionError('bootstrap retention scope mismatch')


class _PayloadAuthority:
    def __init__(self, api, binding):
        self.api, self.binding = api, binding

    def check(self, actor, scope):
        row = self.api._bound(actor, self.binding, 'payload')
        if scope != self.api._scope(row.approval.revision):
            raise PermissionError('bootstrap payload scope mismatch')


class Preparation:
    def __init__(self, *, provider, authorizer: Authorizer | None = None,
                 gate: RuntimeGate | None = None, redactor: LogRedactor | None = None):
        self.provider = provider
        self.authorizer = authorizer if authorizer is not None else Deny()
        self.gate = gate if gate is not None else Deny()
        self.redactor = redactor if redactor is not None else Deny()
        self.secrets = RetainedSecrets(provider=provider, authority=_RevisionAuthority(self))

    def _human(self, actor, project_id, *, owner_id=None):
        if type(actor) is not ActorContext or actor.kind is not PrincipalKind.HUMAN:
            raise PermissionError('authenticated human bootstrap authority required')
        self.authorizer.authorize(actor, 'preparation.approve_bootstrap', project_ids=(project_id,))
        if owner_id is not None and actor.user_id != owner_id:
            raise PermissionError('bootstrap belongs to another human owner')
        return actor.user_id

    def _scope(self, revision):
        return RetentionScope(revision.project_id, revision.owner_id, revision.id, 'policy')

    def _refs(self, revision):
        return tuple(m.BootstrapSecretReference.objects.filter(revision=revision).order_by('version_id').values_list('version_id', flat=True))

    def _references(self, revision):
        self.secrets.check_reference(self._scope(revision), revision.content_version_id)
        self.secrets.check_project_references(self._refs(revision), project_id=revision.project_id)

    def _approved(self, approval):
        revision = approval.revision
        if (approval.human_id != revision.owner_id
                or m.ApprovalRevocation.objects.filter(approval=approval).exists()):
            raise PermissionError('bootstrap approval is revoked or invalid')
        self.authorizer.authorize_owner(revision.owner_id, 'preparation.approve_bootstrap', project_ids=(revision.project_id,))
        self._references(revision)

    def _run_authority(self, actor, row, action):
        if type(actor) is not ActorContext:
            raise PermissionError('trusted bootstrap actor required')
        revision = row.approval.revision
        if actor.kind is PrincipalKind.HUMAN:
            self._human(actor, revision.project_id, owner_id=revision.owner_id)
        # Exact receipt — запись уже случившегося эффекта, не допуск к новому.
        # Gate всё равно обязан проверить текущего writer и exact assignment.
        # Отозванная policy ниже даёт refused, но не стирает наблюдённый outcome.
        if action != 'record':
            self._approved(row.approval)
        if self.gate.check(actor, binding_of(row), action) is not None:
            raise PermissionError('bootstrap gate must validate, not return caller booleans')

    def _bound(self, actor, binding, action, *, lock=False):
        if type(binding) is not RunBinding:
            raise ValueError('complete typed bootstrap binding required')
        rows = m.BootstrapRun.objects.select_for_update() if lock else m.BootstrapRun.objects
        row = rows.select_related('approval__revision').filter(pk=binding.run_id).first()
        if row is None or binding_of(row) != binding:
            raise Conflict('bootstrap run binding changed or is unavailable')
        self._run_authority(actor, row, action)
        return row

    def _locked(self, actor, binding, version, action):
        row = self._bound(actor, binding, action, lock=True)
        if type(version) is not int or version < 1 or row.version != version:
            raise Conflict('bootstrap run version changed')
        if row.deadline <= timezone.now():
            raise Conflict('bootstrap deadline expired; readback required')
        return row

    def _save(self, actor, row, action):
        row.version += 1
        row.save(update_fields=('version', 'state', 'reason', 'failures', 'started_at', 'finished_at'))
        self._run_authority(actor, row, action)
        return run_info(row)

    @storage_boundary
    def publish(self, actor, *, project_id, tasks, variables, provenance, commit, secret_refs=()):
        outside_transaction()
        exact_uuid(project_id)
        content = bounded_content(tasks, variables, provenance, commit, secret_refs)
        owner = self._human(actor, project_id)
        self.secrets.check_project_references(content.secret_refs, project_id=project_id)
        raw = encode(content)
        revision_id = uuid.uuid4()
        scope = RetentionScope(project_id, owner, revision_id, 'policy')
        prepared = self.secrets.prepare(actor, scope, raw)
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project_id)
            self._human(actor, project_id, owner_id=owner)
            self.secrets.check_project_references(content.secret_refs, project_id=project_id)
            previous = m.BootstrapRevision.objects.filter(project_id=project_id, owner_id=owner).order_by('-number').first()
            version_id = self.secrets.commit(actor, prepared)
            row = m.BootstrapRevision.objects.create(id=revision_id, project_id=project_id, owner_id=owner,
                number=previous.number + 1 if previous else 1, content_version_id=version_id,
                digest=hashlib.sha256(raw).hexdigest(), provenance=provenance, commit=commit,
                task_size=len(tasks), vars_size=len(variables))
            for ref in content.secret_refs:
                m.BootstrapSecretReference.objects.create(revision=row, version_id=ref)
            return revision_info(row)

    def _decode(self, row, raw):
        if hashlib.sha256(raw).hexdigest() != row.digest:
            raise PermissionError('bootstrap retained digest mismatch')
        try:
            data = json.loads(raw)
            if data['format'] != 1:
                raise ValueError()
            result = bounded_content(base64.b64decode(data['tasks'], validate=True),
                base64.b64decode(data['variables'], validate=True), data['provenance'], data['commit'],
                tuple(uuid.UUID(value) for value in data['secret_refs']))
        except (ValueError, KeyError, TypeError):
            raise PermissionError('bootstrap retained content is invalid') from None
        if (encode(result) != raw or result.provenance != row.provenance or result.commit != row.commit
                or len(result.tasks) != row.task_size or len(result.variables) != row.vars_size
                or result.secret_refs != self._refs(row)):
            raise PermissionError('bootstrap retained metadata mismatch')
        return result

    @storage_boundary
    def read_revision(self, actor, revision_id):
        outside_transaction()
        row = m.BootstrapRevision.objects.get(pk=revision_id)
        self._human(actor, row.project_id, owner_id=row.owner_id)
        value = self.secrets.read(actor, self._scope(row), row.content_version_id)
        result = self._decode(row, value)
        self._human(actor, row.project_id, owner_id=row.owner_id)
        return result

    @storage_boundary
    def approve(self, actor, revision_id):
        with transaction.atomic():
            row = m.BootstrapRevision.objects.select_for_update().get(pk=revision_id)
            self._human(actor, row.project_id, owner_id=row.owner_id)
            self._references(row)
            approval, _ = m.BootstrapApproval.objects.get_or_create(revision=row, defaults={'human_id': actor.user_id})
            self._approved(approval)
            return approval_info(approval)

    @storage_boundary
    def get_approval(self, actor, approval_id):
        row = m.BootstrapApproval.objects.select_related('revision').get(pk=approval_id)
        self._human(actor, row.revision.project_id, owner_id=row.human_id)
        self._approved(row)
        return approval_info(row)

    @storage_boundary
    def revoke(self, actor, approval_id):
        with transaction.atomic():
            approval = m.BootstrapApproval.objects.select_for_update().select_related('revision').get(pk=approval_id)
            self._human(actor, approval.revision.project_id, owner_id=approval.human_id)
            m.ApprovalRevocation.objects.get_or_create(approval=approval)

    @storage_boundary
    def create_run(self, actor, *, approval_id, target_id, incarnation, request_id, deadline, steps, failure_limit=3):
        exact_uuid(approval_id, target_id, incarnation, request_id)
        if (type(steps) is not tuple or not 1 <= len(steps) <= 64
                or any(type(name) is not str or not re.fullmatch('[a-zA-Z0-9_.-]{1,64}', name) for name in steps)
                or len(set(steps)) != len(steps)
                or type(failure_limit) is not int or not 1 <= failure_limit <= 10):
            raise ValueError('bounded concrete bootstrap steps and failure limit required')
        approval = m.BootstrapApproval.objects.select_related('revision').get(pk=approval_id)
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=approval.revision.project_id)
            row = m.BootstrapRun.objects.filter(target_id=target_id, incarnation=incarnation, request_id=request_id).first()
            if row:
                self._run_authority(actor, row, 'readback')
                names = tuple(row.steps.order_by('ordinal').values_list('name', flat=True))
                if (row.approval_id, row.deadline, row.failure_limit, names) != (approval_id, deadline, failure_limit, steps):
                    raise Conflict('bootstrap request already names another intent')
                return run_info(row)
            # Replay выше раскрывает прежний результат, но не продлевает допуск.
            now = timezone.now()
            if (not isinstance(deadline, timezone.datetime) or timezone.is_naive(deadline)
                    or not now < deadline <= now + timedelta(hours=1)):
                raise ValueError('bounded future bootstrap deadline required')
            prior = m.BootstrapRun.objects.filter(target_id=target_id, incarnation=incarnation).order_by('-created_at').first()
            if prior and prior.state in ('accepted', 'running', 'unknown'):
                raise Conflict('bootstrap target has an unresolved run')
            budget = m.BootstrapRun.objects.filter(target_id=target_id, incarnation=incarnation,
                approval_id=approval_id).order_by('-created_at').first()
            failures = 0
            if budget:
                if budget.state == 'gave_up' or budget.failure_limit != failure_limit:
                    raise Conflict('bootstrap failure budget requires a new approved revision or incarnation')
                failures = budget.failures
            row = m.BootstrapRun(approval=approval, target_id=target_id, incarnation=incarnation,
                request_id=request_id, deadline=deadline, failure_limit=failure_limit,
                failures=failures, step_count=len(steps))
            self._run_authority(actor, row, 'create')
            row.save(force_insert=True)
            for ordinal, name in enumerate(steps):
                m.BootstrapStep.objects.create(run=row, ordinal=ordinal, name=name)
            return run_info(row)

    @storage_boundary
    def get_run(self, actor, run_id):
        row = m.BootstrapRun.objects.select_related('approval__revision').get(pk=run_id)
        self._run_authority(actor, row, 'readback')
        return run_info(row)

    @storage_boundary
    def payload(self, actor, binding):
        outside_transaction()
        row = self._bound(actor, binding, 'payload')
        store = RetainedSecrets(provider=self.provider, authority=_PayloadAuthority(self, binding))
        revision = row.approval.revision
        value = store.read(actor, self._scope(revision), revision.content_version_id)
        content = self._decode(revision, value)
        self._bound(actor, binding, 'payload')
        return content

    def _step(self, row, ordinal):
        if type(ordinal) is not int or not 0 <= ordinal < 64:
            raise ValueError('invalid bootstrap step ordinal')
        step = row.steps.filter(ordinal=ordinal).first()
        if step is None:
            raise Conflict('bootstrap step is unavailable')
        return step

    @storage_boundary
    def begin_step(self, actor, binding, ordinal, *, expected_version):
        with transaction.atomic():
            row = self._locked(actor, binding, expected_version, 'claim')
            step = self._step(row, ordinal)
            if (row.state not in ('accepted', 'running') or step.state not in ('pending', 'failed')
                    or row.steps.filter(ordinal__lt=ordinal).exclude(state='succeeded').exists()
                    or row.steps.filter(state='running').exists() or row.failures >= row.failure_limit):
                raise Conflict('bootstrap step cannot be claimed or repeated')
            row.state = 'running'
            row.started_at = row.started_at or timezone.now()
            step.state, step.attempt, step.progress = 'running', step.attempt + 1, 0
            step.started_at, step.finished_at = timezone.now(), None
            step.save()
            return self._save(actor, row, 'claim')

    def _log(self, raw):
        if type(raw) is not bytes or len(raw) > 65536:
            raise ValueError('bounded bootstrap log bytes required')
        if not raw:
            return ''
        outside_transaction()
        value = None
        try:
            value = self.redactor.sanitize(raw)
        except PermissionError:
            # Незаданный port — безопасный адресный отказ, не raw adapter error.
            if isinstance(self.redactor, Deny):
                raise
        except Exception:
            pass
        if value is None:
            raise ValueError('bootstrap log sanitization failed')
        if type(value) is not str or len(value.encode()) > 131072:
            raise ValueError('invalid sanitized bootstrap log')
        value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
        return ''.join(c for c in value if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)

    def _append(self, step, value):
        step.log = (step.log + value).encode()[-4096:].decode('utf-8', errors='ignore')
        step.log_bytes = len(step.log.encode())

    @storage_boundary
    def progress(self, actor, binding, ordinal, *, percent, log=b'', expected_version):
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError('bounded bootstrap progress required')
        self._bound(actor, binding, 'progress')
        clean = self._log(log)
        with transaction.atomic():
            row = self._locked(actor, binding, expected_version, 'progress')
            step = self._step(row, ordinal)
            if row.state != 'running' or step.state != 'running' or percent < step.progress:
                raise Conflict('bootstrap progress is not current')
            step.progress = percent
            self._append(step, clean)
            step.save()
            return self._save(actor, row, 'progress')

    @storage_boundary
    def record_step(self, actor, binding, ordinal, *, outcome, expected_version, log=b''):
        if outcome not in ('succeeded', 'failed', 'unavailable', 'refused', 'unknown'):
            raise ValueError('invalid bootstrap result')
        self._bound(actor, binding, 'record')
        clean = self._log(log)
        with transaction.atomic():
            row = self._locked(actor, binding, expected_version, 'record')
            step = self._step(row, ordinal)
            if row.state != 'running' or step.state != 'running':
                raise Conflict('bootstrap step is not running')
            step.state = 'failed' if outcome == 'unavailable' else outcome
            step.finished_at = None if outcome == 'unknown' else timezone.now()
            if outcome == 'succeeded':
                step.progress = 100
            elif outcome == 'failed':
                step.failures += 1
                row.failures += 1
                if row.failures >= row.failure_limit:
                    row.state = 'gave_up'
            else:
                row.state = 'failed' if outcome == 'unavailable' else outcome
            try:
                self._approved(row.approval)
            except (PermissionError, SecretConflict, SecretRefused) as error:
                # Это безопасные библиотечные причины, не исключения adapter I/O.
                row.state = 'unknown' if outcome == 'unknown' else 'refused'
                row.reason = str(error)[:256]
            if row.state in ('failed', 'refused', 'gave_up'):
                row.finished_at = timezone.now()
            self._append(step, clean)
            step.save()
            return self._save(actor, row, 'record')

    def _complete(self, row):
        steps = tuple(row.steps.order_by('ordinal').values_list('ordinal', 'state'))
        return (tuple(ordinal for ordinal, state in steps) == tuple(range(row.step_count))
                and all(state == 'succeeded' for ordinal, state in steps))

    @storage_boundary
    def finish(self, actor, binding, *, expected_version):
        with transaction.atomic():
            row = self._locked(actor, binding, expected_version, 'finish')
            if row.state != 'running' or not self._complete(row):
                raise Conflict('incomplete bootstrap cannot confirm success')
            row.state, row.failures, row.finished_at = 'succeeded', 0, timezone.now()
            return self._save(actor, row, 'finish')

    @storage_boundary
    def mark_unknown(self, actor, binding, *, expected_version):
        with transaction.atomic():
            row = self._locked(actor, binding, expected_version, 'unknown')
            if row.state != 'running' or not row.steps.filter(state='running').exists():
                raise Conflict('no running bootstrap effect to mark unknown')
            row.steps.filter(state='running').update(state='unknown')
            row.state = 'unknown'
            return self._save(actor, row, 'unknown')

    @storage_boundary
    def readback(self, actor, binding):
        if type(binding) is not RunBinding:
            raise ValueError('complete typed bootstrap binding required')
        with transaction.atomic():
            candidate = m.BootstrapRun.objects.select_related('approval__revision').filter(pk=binding.run_id).first()
            if candidate is None or binding_of(candidate) != binding:
                # Не подтверждаем неизвестный/неполный/чужой binding и не выдаём его fields.
                if type(actor) is not ActorContext:
                    raise PermissionError('trusted bootstrap actor required')
                if actor.kind is PrincipalKind.HUMAN:
                    self._human(actor, binding.project_id)
                if self.gate.check(actor, binding, 'readback') is not None:
                    raise PermissionError('invalid bootstrap gate result')
                return Readback(False, None)
            row = self._bound(actor, binding, 'readback', lock=True)
            if row.deadline <= timezone.now() and row.state in ('accepted', 'running'):
                row.state = 'unknown' if row.state == 'running' else 'refused'
                row.steps.filter(state='running').update(state='unknown')
                self._save(actor, row, 'readback')
            confirmed = row.state == 'succeeded' and self._complete(row)
            self._run_authority(actor, row, 'readback')
            return Readback(confirmed, run_info(row))

    @storage_boundary
    def steps(self, actor, binding):
        row = self._bound(actor, binding, 'readback')
        result = tuple(step_info(step) for step in row.steps.order_by('ordinal'))
        self._bound(actor, binding, 'readback')
        return result

    def approval_query(self):
        return ApprovalQuery(self)


class ApprovalQuery:
    """Совместимый с #635 SQL-only port; pin — только первое human intent.

    Child и retry уже несут proof из CreationIntent; validate никогда не
    перечитывает latest. Caller Grants проверяет actor/parent/spawn отдельно.
    """
    def __init__(self, api):
        self.api = api

    @storage_boundary
    def pin(self, binding):
        if type(binding) is not PolicyBinding:
            raise PermissionError('trusted policy binding required')
        row = m.BootstrapApproval.objects.select_related('revision').filter(
            human_id=binding.owner_id, revision__project_id=binding.project_id,
            approvalrevocation__isnull=True).order_by('-revision__number').first()
        if row is None:
            raise PermissionError('human-approved bootstrap revision is unavailable')
        proof = ApprovedPolicy(row.id, row.revision.content_version_id, row.revision.digest,
                               row.human_id, row.revision.project_id)
        self.validate(proof, binding)
        return proof

    @storage_boundary
    def validate(self, proof, binding):
        if type(proof) is not ApprovedPolicy or type(binding) is not PolicyBinding:
            raise PermissionError('exact approved bootstrap proof required')
        row = m.BootstrapApproval.objects.select_related('revision').filter(pk=proof.approval_id).first()
        if row is None or (proof.owner_id, proof.project_id) != (binding.owner_id, binding.project_id):
            raise PermissionError('approved bootstrap proof scope mismatch')
        expected = ApprovedPolicy(row.id, row.revision.content_version_id, row.revision.digest,
                                  row.human_id, row.revision.project_id)
        if proof != expected:
            raise PermissionError('approved bootstrap proof binding mismatch')
        self.api._approved(row)
