"""Создание папета (#643): admission общего журнала и SQL-композиция цели.

Внешних эффектов admission не делает, кроме вызовов credential-сервиса от
имени того же человека (reserve/provision гранта): воркер токена человека не
держит. Регистрация job, шаги узла и активация гранта -- шаги Operation.
"""
from datetime import timedelta
import hashlib
import json
from typing import Protocol
from uuid import UUID, uuid5

from django.db import IntegrityError
from django.utils import timezone

from mop.common.credentials import Credential
from mop.server.access.context import ActorKind
from mop.server.access.services import Denied, current_actor
from . import models as m
from .commands import CreateCommand
from .requests import resolve_retry, RequestRefused, Retry
from .service import Refused, _independent, _binding, _snapshot, _event, _ACTIVE

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
        self.grants.register_agent(actor, project_id=intent.project_id, target_id=target_id,
                                   definition_id=command.definition_id)
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
