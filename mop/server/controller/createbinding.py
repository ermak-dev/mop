"""Точка сборки create (#643): порт credential-сервиса и планировщика.

Без связанного credential-сервиса и планировщика с условной регистрацией
create отказывает до записей: наличие права не заменяет привязку.
"""
from dataclasses import dataclass

from .machinebinding import AssignmentBindings, Targets
from .runtime import assignments_service, machine_execution_service, operations_service
from mop.server.access.grants import Grants
from mop.server.operations.launch import CreatePuppets, LaunchJobs


class LaunchTargets:
    """SQL-порт create поверх execution: назначение, инкарнация, имя, пины.

    Живёт в composition: операции не владеют execution-моделями и правилом
    имён драйвера; fence цели -- тот же общий, что у остальных назначений."""
    def epoch(self, node_id):
        return machine_execution_service().current_epoch(node_id)

    def _name(self, project):
        from mop.driver import PREFIX, valid_name
        from mop.server.operations.service import Refused
        from mop.server.sessions.models import Assignment
        used = set(Assignment.objects.filter(project=project).exclude(display_name=None)
                   .values_list('display_name', flat=True))
        number = 1
        while f'{PREFIX}{project.name}-{number}' in used:
            number += 1
        name = f'{PREFIX}{project.name}-{number}'
        if not valid_name(name) or len(name) > 64:
            raise Refused('revision_conflict', safe_reason='project name cannot form a puppet name')
        return name

    def assign(self, actor, *, target_id, incarnation_id, project_id, node_id, receipt, expires_at,
               epoch, revision, image):
        from mop.server.catalog.models import Project
        from mop.server.sessions.models import Assignment, IncarnationLaunch
        project = Project.objects.get(pk=project_id)
        row = Assignment.objects.create(id=target_id, project=project, node_id=node_id,
            owner_id=actor.user_id, role=receipt.role, bundle_digest=receipt.digest, grant_id=receipt.id,
            expires_at=expires_at, display_name=self._name(project))
        assignments_service().activate(actor, row.id, expected_revision=row.revision,
            executor_epoch=epoch, incarnation_id=incarnation_id)
        IncarnationLaunch.objects.create(incarnation_id=incarnation_id, bootstrap_revision=revision.id,
            bootstrap_digest=revision.digest, image_digest=image)
        return Assignment.objects.get(pk=row.id)


def enroll_node(node_id, *, scheduler_id, nomad_node_id, capabilities):
    """Trusted Root enrollment узла в планировщике (#643), не HTTP payload.

    Id узла в Nomad и заявленные capabilities запуска -- факты установки;
    повторная привязка того же узла заменяет их целиком."""
    from uuid import UUID
    from mop.server.operations.models import ScheduledNode
    from mop.server.spec import API_LAUNCH_CAPABILITIES
    if (type(scheduler_id) is not UUID or type(nomad_node_id) is not UUID
            or type(capabilities) not in (set, frozenset) or not set(capabilities) <= API_LAUNCH_CAPABILITIES):
        raise ValueError('typed scheduler identity and known launch capabilities required')
    ScheduledNode.objects.update_or_create(node_id=node_id, defaults=dict(scheduler_id=scheduler_id,
        nomad_node_id=nomad_node_id, capabilities=sorted(capabilities)))


@dataclass(frozen=True)
class LaunchWork:
    """Metadata DTO регистрации (#640): первая machine work инкарнации, не wire."""
    epoch: str
    operation_id: str
    step_id: str
    target_id: str
    incarnation_id: str
    node_id: str
    executor_epoch: str
    grant_id: str
    owner_id: int
    generation: int
    revision: int
    role: str
    bundle_digest: str
    action: str
    not_after: str


class LaunchSpecs:
    """Рендер спеки тела из доверенных фактов БД: узел, его привязка к
    планировщику, настройки пула, пины инкарнации, пин одобренного определения
    и ресурсы политики образа. Байтов определения composition не читает."""
    def _settings(self):
        from mop.server.catalog import models as cm
        from mop.server.catalog.definitions import resolve
        from mop.server.controller.models import Installation
        installation = Installation.objects.get()
        values = {row.key: getattr(row, row.value_kind + '_value')
                  for row in cm.InstallationSetting.objects.filter(installation=installation)}
        return dict(resolve(values, None, require=('pool_home', 'pool_user')).values)

    def render(self, target, *, epoch, restore_generation, operation_id, step_id, not_after,
               definition_id, scheduler_id):
        from pathlib import Path
        from mop.driver.launch import BundlePin, MachineConfig, Registration, RegisteredLayout
        from mop.server.access.models import AgentDefinitionVersion
        from mop.server.catalog.models import Node, NodeImage
        from mop.server.operations.models import ScheduledNode
        from mop.server.sessions.models import Assignment, IncarnationLaunch
        from mop.server.spec import api_job_spec
        node = Node.objects.get(pk=target.node_id)
        enrolled = ScheduledNode.objects.get(node=node, scheduler_id=scheduler_id)
        settings = self._settings()
        launch = IncarnationLaunch.objects.get(incarnation_id=target.incarnation_id)
        name = Assignment.objects.values_list('display_name', flat=True).get(pk=target.target_id)
        definition = AgentDefinitionVersion.objects.get(pk=definition_id)
        policy = NodeImage.objects.select_related('observed_image__policy').get(
            node=node, project_id=target.project_id).observed_image.policy
        work = LaunchWork(str(epoch), str(operation_id), str(step_id), str(target.target_id),
            str(target.incarnation_id), str(target.node_id), str(target.executor_epoch), str(target.grant_id),
            target.owner_id, target.generation, target.revision, target.role, target.bundle_digest,
            'apply_identity', not_after.isoformat())
        registration = Registration(work, str(launch.bootstrap_revision), launch.bootstrap_digest,
                                    launch.image_digest, name)
        return api_job_spec(layout=RegisteredLayout(Path(settings['pool_home']), registration),
            machine=MachineConfig(node.control_origin, node.lan_address, node.bootstrap_address,
                                  node.ssh_port, node.driver),
            bundle=BundlePin(definition.role, definition.digest, definition.driver),
            nomad_node_id=str(enrolled.nomad_node_id), datacenter=settings['pool_datacenter'],
            user=settings['pool_user'], memory_mb=policy.memory_mb, cpu=policy.cpu,
            restore_generation=restore_generation, capabilities=frozenset(enrolled.capabilities))


def service(*, grants=None, issuer=None, scheduler=None):
    from .adminservices import credential_client
    grants = grants or Grants(provider=None, bindings=AssignmentBindings())
    operations = operations_service(grants=grants.local_authority())
    operations.creates = CreatePuppets(operations, grants=grants, targets=LaunchTargets(),
        issuer=issuer if issuer is not None else credential_client(), scheduler=scheduler)
    targets = Targets(grants)
    # Машинная полоса той же операции (apply_identity, confirm_ready) идёт по
    # общему порту целей.
    operations.targets = targets
    operations.launches = LaunchJobs(operations, scheduler=scheduler, targets=targets, specs=LaunchSpecs())
    return operations
