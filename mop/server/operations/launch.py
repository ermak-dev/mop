"""Создание папета (#643): admission общего журнала и SQL-композиция цели.

Внешних эффектов admission не делает, кроме вызовов credential-сервиса от
имени того же человека (reserve/provision гранта): воркер токена человека не
держит. Регистрация job, шаги узла и активация гранта -- шаги Operation.
"""
from datetime import timedelta
import hashlib
import json
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from django.db import IntegrityError
from django.utils import timezone

from mop.common.credentials import Credential
from mop.server.access.context import ActorKind
from mop.server.access.services import Denied, current_actor
from . import models as m
from .commands import CreateCommand
from .requests import resolve_retry, RequestRefused, Retry
from .lifecycle import State
from .puppet import RestartHandler
from .scheduler import RegisterProof, RegisterTicket
from .service import Refused, _independent, _binding, _actor, _snapshot, _event, _move, _ACTIVE

STAGES = ('scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant')
REGISTER = frozenset({'conditional_register_job', 'inspect_register_job'})
# Грант и назначение живут не дольше предела гранта; продление -- отдельная операция.
LIFETIME = timedelta(days=30)
# Пространство имён детерминированных target/incarnation: повтор того же
# запроса находит те же агента и грант, а не заводит новые.
_IDENTITY = UUID('5a0d6c52-1f2e-4c8a-9b7d-6e3f4a2b1c90')


class LaunchTargets(Protocol):
    """SQL-порт назначений от composition: операции не владеют execution-моделями."""
    def epoch(self, node_id: UUID) -> UUID | None:
        """Эпоха, заявленная executor узла; неизвестна -- None."""
        ...

    def assign(self, actor, *, target_id, incarnation_id, project_id, node_id, receipt, expires_at,
               epoch, revision, image):
        """В текущей транзакции: назначение с именем, инкарнация, пины запуска.
        -> назначение после активации (id, revision, generation, role, bundle_digest, grant_id)."""
        ...


class LaunchSpecs(Protocol):
    """Порт рендера спеки тела от composition (#643): доверенные факты, не wire."""
    def render(self, target, *, epoch, restore_generation, operation_id, step_id, not_after,
               definition_id, scheduler_id) -> dict:
        """-> {'Job': ...} по узлу, его привязке к планировщику, пинам инкарнации
        и пину одобренного определения; адресная Meta -- addressed_metadata."""
        ...


def digest(command):
    intent = command.intent
    value = {'action': 'create', 'epoch': str(intent.epoch), 'project_id': str(intent.project_id),
             'node_id': str(command.node_id), 'definition_id': str(command.definition_id),
             'not_after': intent.not_after.isoformat()}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def image_pin(image_id):
    """Пин записи одобренного образа узла, не content-digest слоёв: образ
    идентифицирует ImageRevision, а наблюдение узла подтверждает её наличие."""
    return hashlib.sha256(('image-revision:' + str(image_id)).encode()).hexdigest()


def identities(actor, request_id):
    base = f'{actor.user_id}:{request_id}'
    return uuid5(_IDENTITY, base + ':target'), uuid5(_IDENTITY, base + ':incarnation')


class CreatePuppets:
    action = 'create'
    permissions = ('operations.view_operation', 'execution.create_puppet', 'execution.assign_work')

    def __init__(self, operations, *, grants, targets=None, issuer=None, scheduler=None, namespace='default'):
        self.ops, self.grants, self.issuer, self.scheduler = operations, grants, issuer, scheduler
        self.targets, self.namespace = targets, namespace

    def _ports(self):
        if (self.issuer is None or self.scheduler is None or self.targets is None
                or type(self.scheduler.scheduler_id) is not UUID
                or not REGISTER <= self.scheduler.capabilities):
            raise Refused('controller_unavailable', safe_reason='create puppet bindings are unavailable')

    def _authorize(self, actor, project_id):
        if actor.kind is not ActorKind.HUMAN:
            raise Denied('create requires a human owner')
        for action in self.permissions:
            self.ops.access.authorize(actor, action, project_ids=(project_id,))

    def _pins(self, actor, command):
        """SQL-only: узел проекта, эпоха executor, одобренный bootstrap, готовый образ."""
        from mop.server.catalog.models import NodeImage, NodeProject
        from mop.server.preparation.models import BootstrapApproval
        project_id, node_id = command.intent.project_id, command.node_id
        if not NodeProject.objects.filter(node_id=node_id, project_id=project_id).exists():
            raise Denied('node is not in the project')
        if not m.ScheduledNode.objects.filter(node_id=node_id, scheduler_id=self.scheduler.scheduler_id).exists():
            raise Refused('controller_unavailable', safe_reason='node is not enrolled with the scheduler')
        epoch = self.targets.epoch(node_id)
        if epoch is None:
            raise Refused('controller_unavailable', safe_reason='node executor has not declared its epoch')
        approval = (BootstrapApproval.objects.select_related('revision')
                    .filter(revision__project_id=project_id, human_id=actor.human_owner_id,
                            approvalrevocation__isnull=True)
                    .order_by('-revision__number').first())
        if approval is None:
            raise Refused('revision_conflict', safe_reason='no approved bootstrap revision for the project')
        image = NodeImage.objects.filter(node_id=node_id, project_id=project_id, available=True).first()
        if image is None or image.observed_image_id is None or image.observed_image_id != image.desired_image_id:
            raise Refused('revision_conflict', safe_reason='the node image of the project is not ready')
        return epoch, approval.revision, image_pin(image.observed_image_id)

    def _admit(self, actor, command, installation):
        """Общие проверки admission; повторяются в финальной транзакции."""
        self._ports()
        self._authorize(actor, command.intent.project_id)
        if not installation.effects_enabled:
            raise Refused('effects_disabled')
        now = timezone.now()
        if command.intent.not_after <= now:
            raise Refused('deadline_expired')
        if command.intent.not_after > now + timedelta(minutes=5):
            raise Refused('deadline_horizon')
        return self._pins(actor, command)

    def _replay(self, actor, command, installation):
        intent = command.intent
        old = m.Operation.objects.filter(actor_id=actor.user_id, request_id=intent.request_id).first()
        try:
            retry = resolve_retry(expected_epoch=intent.epoch, current_epoch=installation.epoch,
                submitted_digest=digest(command), stored_digest=old.digest if old else None,
                result_available=not old.result_pruned if old else True)
        except RequestRefused as error:
            raise Refused(error.code) from None
        if retry is Retry.EXPIRED:
            raise Refused('result_expired')
        return old if retry is Retry.REPLAY else None

    def _agent_credential(self, agent_id, project_id):
        """Субъект делегирования -- агент со своим credential (#688), выданным
        вне lifecycle гранта: без него активация отказала бы всегда. Каждый шаг
        идемпотентен, повтор запроса ничего не меняет."""
        from mop.server.access import models as a, services as auth
        auth.bind_local_identity(agent_id)
        source = a.LoginIdentity.objects.get(user_id=agent_id).source
        a.ProjectMembership.objects.get_or_create(user_id=agent_id, project_id=project_id, source=source)
        auth.issue_token(agent_id)

    def submit(self, actor, command, *, credential):
        if type(command) is not CreateCommand:
            raise ValueError('typed create command required')
        if type(credential) is not Credential:
            raise Denied('current human credential required')
        _independent()
        intent = command.intent
        # 1. Без записей: replay, привязки, права, пины -- отказ ничего не оставляет.
        with self.ops._transaction():
            installation = self.ops._installation(intent.epoch)
            current_actor(actor, grants=self.ops.grants)
            old = self._replay(actor, command, installation)
            if old is not None:
                return self.ops._disclose(actor, old)
            self._admit(actor, command, installation)
        target_id, incarnation_id = identities(actor, intent.request_id)
        expires_at = timezone.now() + LIFETIME
        # 2. Агент и грант -- идемпотентны по target/request; вне SQL, от имени человека.
        profile = self.grants.register_agent(actor, project_id=intent.project_id, target_id=target_id,
                                             definition_id=command.definition_id)
        self._agent_credential(profile.agent_id, intent.project_id)
        receipt = self.issuer.reserve_grant(credential, project_id=intent.project_id,
            definition_id=command.definition_id, target_id=target_id, incarnation=incarnation_id,
            request_id=intent.request_id, expires_at=expires_at)
        if (receipt.target_id, receipt.incarnation, receipt.project_id) != (target_id, incarnation_id, intent.project_id):
            raise Denied('credential service returned a foreign grant')
        if receipt.credential_version_id is None:
            receipt = self.issuer.provision_grant(credential, receipt.id, expected_revision=receipt.revision)
        if receipt.credential_version_id is None or receipt.state != 'reserved':
            raise Refused('revision_conflict', safe_reason='grant is not provisioned')
        # 3. Одна транзакция: назначение, инкарнация, пины, Operation и шаги.
        try:
            return self._compose(actor, command, target_id, incarnation_id, receipt, expires_at)
        except IntegrityError:
            # Параллельный повтор того же запроса успел первым: его результат и есть ответ.
            with self.ops._transaction():
                installation = self.ops._installation(intent.epoch)
                old = self._replay(actor, command, installation)
                if old is None:
                    raise
                return self.ops._disclose(actor, old)

    def _compose(self, actor, command, target_id, incarnation_id, receipt, expires_at):
        from mop.server.catalog.models import Node, Project
        intent = command.intent
        with self.ops._transaction():
            installation = self.ops._installation(intent.epoch)
            current_actor(actor, grants=self.ops.grants)
            old = self._replay(actor, command, installation)
            if old is not None:
                return self.ops._disclose(actor, old)
            project = Project.objects.select_for_update().get(pk=intent.project_id)
            Node.objects.select_for_update().get(pk=command.node_id)
            epoch, revision, image = self._admit(actor, command, installation)
            active = m.Operation.objects.filter(epoch=installation.epoch, state__in=_ACTIVE)
            if (active.count() >= self.ops.queue_limit
                    or active.filter(projects=project.pk).count() >= self.ops.project_limit):
                raise Refused('admission_limit')
            row = self.targets.assign(actor, target_id=target_id, incarnation_id=incarnation_id,
                project_id=project.pk, node_id=command.node_id, receipt=receipt, expires_at=expires_at,
                epoch=epoch, revision=revision, image=image)
            operation = m.Operation.objects.create(actor_id=actor.user_id, actor_binding=_binding(actor),
                request_id=intent.request_id, digest=digest(command), epoch=intent.epoch,
                restore_generation=installation.generation, action=self.action, node_id=command.node_id,
                target_kind='puppet', target_id=row.id, incarnation_id=incarnation_id,
                expected_revision=row.revision, eligible=None, not_after=intent.not_after)
            operation.projects.set((project.pk,))
            m.RestartBinding.objects.create(operation=operation, project_id=project.pk, executor_epoch=epoch,
                owner_id=actor.user_id, generation=row.generation, role=row.role,
                bundle_digest=row.bundle_digest, grant_id=row.grant_id)
            m.CreateBinding.objects.create(operation=operation, definition_id=command.definition_id,
                scheduler_id=self.scheduler.scheduler_id, job_id=str(row.id), namespace=self.namespace)
            for ordinal, name in enumerate(STAGES, 1):
                m.Step.objects.create(operation=operation, ordinal=ordinal, name=name)
            _event(operation)
            return _snapshot(operation)


def require_reserved_grant(target):
    """Грант новой цели всё ещё выдан ей: reserved, с retained credential, та
    же инкарнация. Авторизация исполнения до готовности была бы ложным отказом:
    token агента приходит после неё."""
    from mop.server.access.models import Grant
    grant = Grant.objects.select_related('intent').filter(pk=target.grant_id).first()
    if (grant is None or grant.state != 'reserved' or grant.credential_version_id is None
            or (grant.intent.target_id, grant.intent.incarnation) != (target.target_id, target.incarnation_id)):
        raise Denied('reserved grant of the new target is no longer current')


class CreateMachine(RestartHandler):
    """Узловые шаги create (#643): apply_identity и confirm_ready общего протокола.

    Принимается только create-admission'ом, не машинным submit; стадии те же,
    что у Operation create, -- последний узловой шаг не терминален, дальше
    серверная activate_grant."""
    stages = STAGES
    action = 'create'
    permission = 'execution.create_puppet'

    def __init__(self, operations, *, support=None):
        super().__init__(operations)
        self.support = support

    def _valid(self, command):
        raise ValueError('create is admitted by its own command, not by the machine handler')

    def require(self, target, row=None):
        if self.support is None:
            raise Refused('controller_unavailable', safe_reason='launch binding is unavailable')
        self.support.require(target)
        require_reserved_grant(target)


class LaunchJobs:
    """Полоса scheduler_register (#643): одна условная запись спеки тела.

    Как stop: claim -> preflight -> последняя SQL-проверка -> эффект -> запись
    результата; потерянный ответ -- unknown, сверка только чтением. Успех
    записывает enrollment ScheduledPuppet: stop/delete узнают этот job.
    """
    step = 'scheduler_register'

    def __init__(self, operations, *, scheduler=None, targets=None, specs=None):
        self.ops, self.scheduler, self.targets, self.specs = operations, scheduler, targets, specs

    def _ports(self):
        if (self.scheduler is None or self.targets is None or self.specs is None
                or type(self.scheduler.scheduler_id) is not UUID or not REGISTER <= self.scheduler.capabilities):
            raise Refused('controller_unavailable', safe_reason='conditional register scheduler is unavailable')

    def _target(self, row):
        from .puppet import RestartTarget
        pin = row.restartbinding
        return RestartTarget(row.target_id, pin.project_id, row.node_id, row.incarnation_id, pin.executor_epoch,
            pin.owner_id, pin.role, pin.bundle_digest, pin.grant_id, pin.generation, row.expected_revision)

    def _locked(self, operation_id):
        # Порядок блокировок machine execution: restore -> Assignment -> Operation -> Step.
        installation = self.ops._installation()
        target_id = m.Operation.objects.values_list('target_id', flat=True).get(pk=operation_id, action='create')
        current = self.targets.locked(target_id)
        row = m.Operation.objects.select_for_update().get(pk=operation_id, action='create')
        step = m.Step.objects.select_for_update().get(operation=row, name=self.step)
        if row.epoch != installation.epoch or row.restore_generation != installation.generation:
            raise Refused('epoch_conflict')
        return installation, current, row, step

    def _validate(self, installation, current, row):
        if not installation.effects_enabled:
            raise Refused('effects_disabled')
        if row.not_after <= timezone.now():
            raise Refused('deadline_expired')
        target = self._target(row)
        if current != target:
            raise Refused('revision_conflict')
        actor = _actor(row)
        self.ops.access.authorize(actor, 'execution.create_puppet', project_ids=(target.project_id,))
        if actor.human_owner_id != target.owner_id:
            raise Denied('create belongs to another human assignment')
        require_reserved_grant(target)
        binding = row.createbinding
        if (binding.scheduler_id != self.scheduler.scheduler_id or not m.ScheduledNode.objects.filter(
                node_id=row.node_id, scheduler_id=binding.scheduler_id).exists()):
            raise Refused('controller_unavailable', safe_reason='node is not enrolled with the scheduler')
        return target

    def _ticket(self, installation, row, step, target):
        binding = row.createbinding
        job = self.specs.render(target, epoch=row.epoch, restore_generation=installation.generation,
            operation_id=row.id, step_id=m.Step.objects.get(operation=row, name='apply_identity').id,
            not_after=row.not_after, definition_id=binding.definition_id, scheduler_id=binding.scheduler_id)['Job']
        return RegisterTicket(row.epoch, target, binding.scheduler_id, binding.job_id, binding.namespace,
            installation.generation, 0, job, row.id, step.token, row.not_after)

    def claim(self, operation_id):
        _independent()
        self._ports()
        with self.ops._transaction():
            installation, current, row, step = self._locked(operation_id)
            if row.state != 'accepted' or step.token is not None:
                return None
            try:
                target = self._validate(installation, current, row)
            except (Denied, Refused) as error:
                _move(row, State.REFUSED, 'execution_refused', expected_revision=row.revision, safe_reason=str(error))
                return None
            keys = sorted(('node:'+str(row.node_id), 'project:'+str(target.project_id),
                           'puppet:'+str(row.target_id), 'target:'+str(row.target_id)))
            for key in keys:
                m.Resource.objects.get_or_create(key=key)
            tuple(m.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
            if m.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None).exists():
                return None
            if m.Operation.objects.filter(state__in=('running', 'reconciling', 'unknown'),
                                          resourceclaim__released_at=None).distinct().count() >= self.ops.execution_limit:
                return None
            step.token = step.attempt_id = uuid4()
            step.started_at = timezone.now()
            step.lease_until = min(row.not_after, timezone.now() + timedelta(seconds=30))
            step.save(update_fields=('token', 'attempt_id', 'started_at', 'lease_until'))
            for key in keys:
                m.ResourceClaim.objects.create(resource_id=key, operation=row, step=step, token=step.token)
            _move(row, State.RUNNING, 'effect_claimed', expected_revision=row.revision)
            return self._ticket(installation, row, step, target)

    def prepare(self, ticket):
        _independent()
        with self.ops._transaction():
            installation, current, row, step = self._locked(ticket.operation_id)
            if step.token != ticket.token or row.state != 'running' or step.dispatched_at is not None:
                return False
            try:
                self._validate(installation, current, row)
            except (Denied, Refused) as error:
                _move(row, State.REFUSED, 'execution_refused', expected_revision=row.revision, safe_reason=str(error))
                m.ResourceClaim.objects.filter(operation=row, released_at=None).update(released_at=timezone.now())
                return False
            step.dispatched_at = timezone.now()
            step.save(update_fields=('dispatched_at',))
            return True

    def record(self, ticket, proof):
        _independent()
        if type(proof) is not RegisterProof:
            raise ValueError('typed registration proof required')
        with self.ops._transaction():
            _, _, row, step = self._locked(ticket.operation_id)
            if step.token != ticket.token:
                raise Refused('claim_conflict')
            if step.quiescent_at is not None:
                if step.outcome == proof.outcome and step.evidence == proof.evidence:
                    return _snapshot(row)
                raise Refused('claim_conflict')
            if row.state not in ('running', 'unknown', 'reconciling'):
                raise Refused('operation_conflict')
            if proof.outcome == 'unknown':
                step.outcome, step.evidence = proof.outcome, proof.evidence
                step.save(update_fields=('outcome', 'evidence'))
                if row.state == 'running':
                    _move(row, State.RECONCILING, 'effect_uncertain', expected_revision=row.revision)
                    _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)
                return _snapshot(row)
            if row.state == 'unknown':
                _move(row, State.RECONCILING, 'late_confirmation', expected_revision=row.revision)
            applied = proof.outcome == 'succeeded'
            step.outcome, step.evidence, step.quiescent_at = proof.outcome, proof.evidence, timezone.now()
            step.save(update_fields=('outcome', 'evidence', 'quiescent_at'))
            m.EffectReceipt.objects.create(step=step, token=ticket.token, epoch=row.epoch, node_id=row.node_id,
                source_revision=row.expected_revision, decision_revision=row.revision, applied=applied)
            if not applied:
                _move(row, State.FAILED, 'conditional_refusal', expected_revision=row.revision)
                m.ResourceClaim.objects.filter(operation=row, released_at=None).update(released_at=step.quiescent_at)
                return _snapshot(row)
            binding = row.createbinding
            binding.applied_index = proof.modify_index
            binding.save(update_fields=('applied_index',))
            # Тот же enrollment, по которому stop/delete узнают свой job.
            m.ScheduledPuppet.objects.create(target_id=row.target_id, incarnation_id=row.incarnation_id,
                epoch=row.epoch, restore_generation=ticket.restore_generation, scheduler_id=binding.scheduler_id,
                job_id=binding.job_id, namespace=binding.namespace, modify_index=proof.modify_index)
            if row.state == 'reconciling':
                # Сверка частичного workflow не выдаёт оставшиеся effects автоматически.
                _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)
                return _snapshot(row)
            # Цель остаётся за операцией: дальше её шаги исполняет узел.
            m.ResourceClaim.objects.filter(operation=row, step=step, released_at=None).update(released_at=step.quiescent_at)
            before = row.revision
            row.revision += 1
            row.code, row.reason = 'step_confirmed', 'named stage receipt committed'
            if not m.Operation.objects.filter(pk=row.id, revision=before, state=State.RUNNING).update(
                    revision=row.revision, code=row.code, reason=row.reason):
                raise Refused('revision_conflict')
            _event(row)
            return _snapshot(row)

    def run(self, operation_id):
        """Blocking worker entry; Root вызывает в Runtime.blocking, не HTTP handler."""
        ticket = self.claim(operation_id)
        if ticket is None:
            return None
        if not self.prepare(ticket):
            return None
        return self.record(ticket, self.scheduler.register_job(ticket))

    def reconcile(self, actor, operation_id):
        """Только актуальное recovery право; сверка чтением, без новой записи."""
        _independent()
        self._ports()
        with self.ops._transaction():
            installation, _, row, step = self._locked(operation_id)
            self.ops.access.authorize(actor, 'operations.recover_operation',
                                      project_ids=tuple(row.projects.values_list('pk', flat=True)))
            if row.state not in ('running', 'unknown', 'reconciling') or step.token is None:
                raise Refused('operation_conflict')
            ticket = self._ticket(installation, row, step, self._target(row))
        return self.record(ticket, self.scheduler.inspect_register(ticket))


class GrantActivations:
    """Полоса activate_grant (#643): активация гранта credential-сервисом.

    Зовётся только после успешных шагов узла: квитанцию готовности сервис
    проверяет сам. Исход вызова может потеряться -- тогда unknown, и сверка
    читает состояние гранта, не зовёт активацию повторно."""
    step = 'activate_grant'
    before = ('scheduler_register', 'apply_identity', 'confirm_ready')

    def __init__(self, operations, *, targets=None, activator=None):
        self.ops, self.targets, self.activator = operations, targets, activator

    def _ports(self):
        if self.targets is None or self.activator is None:
            raise Refused('controller_unavailable', safe_reason='grant activation is unavailable')

    def _locked(self, operation_id):
        installation = self.ops._installation()
        target_id = m.Operation.objects.values_list('target_id', flat=True).get(pk=operation_id, action='create')
        current = self.targets.locked(target_id)
        row = m.Operation.objects.select_for_update().get(pk=operation_id, action='create')
        step = m.Step.objects.select_for_update().get(operation=row, name=self.step)
        if row.epoch != installation.epoch or row.restore_generation != installation.generation:
            raise Refused('epoch_conflict')
        return installation, current, row, step

    def _ready(self, row):
        return not m.Step.objects.filter(operation=row, name__in=self.before).exclude(outcome='succeeded').exists()

    def claim(self, operation_id):
        _independent()
        self._ports()
        with self.ops._transaction():
            installation, current, row, step = self._locked(operation_id)
            if row.state != 'running' or step.token is not None or not self._ready(row):
                return None
            target = LaunchJobs._target(self, row)
            try:
                if row.not_after <= timezone.now():
                    raise Refused('deadline_expired')
                if not installation.effects_enabled:
                    raise Refused('effects_disabled')
                if current != target:
                    raise Refused('revision_conflict')
                actor = _actor(row)
                self.ops.access.authorize(actor, 'execution.create_puppet', project_ids=(target.project_id,))
                if actor.human_owner_id != target.owner_id:
                    raise Denied('create belongs to another human assignment')
                require_reserved_grant(target)
            except (Denied, Refused) as error:
                _move(row, State.FAILED, 'execution_refused', expected_revision=row.revision, safe_reason=str(error))
                m.ResourceClaim.objects.filter(operation=row, released_at=None).update(released_at=timezone.now())
                return None
            keys = sorted(('node:'+str(row.node_id), 'project:'+str(target.project_id),
                           'puppet:'+str(row.target_id), 'target:'+str(row.target_id)))
            for key in keys:
                m.Resource.objects.get_or_create(key=key)
            tuple(m.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
            held = m.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None)
            if held.exclude(operation=row).exists():
                return None
            step.token = step.attempt_id = uuid4()
            step.started_at = step.dispatched_at = timezone.now()
            step.lease_until = min(row.not_after, timezone.now() + timedelta(seconds=30))
            step.save(update_fields=('token', 'attempt_id', 'started_at', 'dispatched_at', 'lease_until'))
            # Шаги узла держат цель до терминала операции: их claims остаются,
            # заводятся только недостающие ключи.
            own = set(held.filter(operation=row).values_list('resource_id', flat=True))
            for key in keys:
                if key not in own:
                    m.ResourceClaim.objects.create(resource_id=key, operation=row, step=step, token=step.token)
            from mop.server.access.models import Grant
            return (row.id, step.token, target.grant_id, Grant.objects.values_list('revision', flat=True).get(
                pk=target.grant_id), row.not_after)

    def record(self, operation_id, token, outcome, evidence, reason=None):
        _independent()
        with self.ops._transaction():
            _, _, row, step = self._locked(operation_id)
            if step.token != token:
                raise Refused('claim_conflict')
            if step.quiescent_at is not None:
                if (step.outcome, step.evidence) == (outcome, evidence):
                    return _snapshot(row)
                raise Refused('claim_conflict')
            if row.state not in ('running', 'unknown', 'reconciling'):
                raise Refused('operation_conflict')
            if outcome == 'unknown':
                step.outcome, step.evidence = outcome, evidence
                step.save(update_fields=('outcome', 'evidence'))
                if row.state == 'running':
                    _move(row, State.RECONCILING, 'effect_uncertain', expected_revision=row.revision)
                    _move(row, State.UNKNOWN, 'outcome_unknown', expected_revision=row.revision)
                return _snapshot(row)
            if row.state == 'unknown':
                _move(row, State.RECONCILING, 'late_confirmation', expected_revision=row.revision)
            applied = outcome == 'succeeded'
            step.outcome, step.evidence, step.quiescent_at = outcome, evidence, timezone.now()
            step.save(update_fields=('outcome', 'evidence', 'quiescent_at'))
            m.EffectReceipt.objects.create(step=step, token=token, epoch=row.epoch, node_id=row.node_id,
                source_revision=row.expected_revision, decision_revision=row.revision, applied=applied)
            # Отказ сервиса доходит до читателя своей фразой, не глухим failed.
            _move(row, State.SUCCEEDED if applied else State.FAILED,
                  'effect_confirmed' if applied else 'effect_failed', expected_revision=row.revision,
                  **({'safe_reason': reason} if reason else {}))
            m.ResourceClaim.objects.filter(operation=row, released_at=None).update(released_at=step.quiescent_at)
            return _snapshot(row)

    def run(self, operation_id):
        """Blocking worker entry; вызов credential-сервиса вне SQL-транзакции."""
        ticket = self.claim(operation_id)
        if ticket is None:
            return None
        operation_id, token, grant_id, revision, not_after = ticket
        # #703: COMMIT claim занял wall time; перед новым эффектом intent
        # проверяется снова. Сверка старого результата срока не требует.
        if not_after <= timezone.now():
            return self.record(operation_id, token, 'failed', 'activation-refused', 'command deadline expired')
        try:
            receipt = self.activator.activate_grant(grant_id, expected_revision=revision)
            outcome = ('succeeded', 'grant-activated') if receipt.state == 'active' else (
                'failed', 'activation-refused', 'grant is ' + str(receipt.state) + ' after activation')
        except (Denied, PermissionError) as error:
            outcome = ('failed', 'activation-refused', str(error))
        except Exception:
            # Ответ мог потеряться после COMMIT активации; повторного вызова нет.
            outcome = ('unknown', 'activation-uncertain')
        return self.record(operation_id, token, *outcome)

    def reconcile(self, actor, operation_id):
        """Сверка чтением гранта: active -- успех, отозван/истёк -- отказ;
        reserved ничего не доказывает -- исход остаётся неизвестным."""
        _independent()
        self._ports()
        with self.ops._transaction():
            _, _, row, step = self._locked(operation_id)
            self.ops.access.authorize(actor, 'operations.recover_operation',
                                      project_ids=tuple(row.projects.values_list('pk', flat=True)))
            if row.state not in ('running', 'unknown', 'reconciling') or step.token is None:
                raise Refused('operation_conflict')
            from mop.server.access.models import Grant
            state = Grant.objects.values_list('state', flat=True).get(pk=row.restartbinding.grant_id)
            token = step.token
        if state == 'active':
            return self.record(operation_id, token, 'succeeded', 'grant-activated')
        if state in ('revoked', 'expired'):
            return self.record(operation_id, token, 'failed', 'activation-refused')
        return None
