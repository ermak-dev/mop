"""Точка сборки create (#643): порт credential-сервиса и планировщика.

Без связанного credential-сервиса и планировщика с условной регистрацией
create отказывает до записей: наличие права не заменяет привязку.
"""
from dataclasses import dataclass

from .machinebinding import Targets, local_grants
from .initialselection import LaunchSelections
from .runtime import assignments_service, machine_execution_service, operations_service
from mop.server.operations.launch import CreateMachine, CreatePuppets, GrantActivations, LaunchJobs


class LaunchTargets:
    """SQL-порт create поверх execution: назначение, инкарнация, имя, пины.

    Живёт в composition: операции не владеют execution-моделями и правилом
    имён драйвера; fence цели -- тот же общий, что у остальных назначений."""
    def epoch(self, node_id):
        return machine_execution_service().current_epoch(node_id)

    def require_node(self, node_id, scheduler_id):
        require_anchor(node_id, scheduler_id)

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
        from mop.server.operations.launch import image_pin
        IncarnationLaunch.objects.create(incarnation_id=incarnation_id, bootstrap_revision=revision.id,
            bootstrap_digest=revision.digest, image_digest=image_pin(image), image_revision_id=image)
        return Assignment.objects.get(pk=row.id)


def require_anchor(node_id, scheduler_id):
    """Trusted SQL enrollment проверяется до provisioning и перед каждой фазой."""
    from mop.server.operations.models import ScheduledNode
    from mop.server.operations.service import Refused
    from mop.server.launch_spec import API_LAUNCH_CAPABILITIES, anchor_pin
    enrolled = ScheduledNode.objects.filter(node_id=node_id, scheduler_id=scheduler_id).first()
    if enrolled is None or not API_LAUNCH_CAPABILITIES <= set(enrolled.capabilities):
        raise Refused('controller_unavailable', safe_reason='installed initial create anchor capabilities are unavailable')
    try:
        anchor_pin(enrolled.anchor_executable, enrolled.anchor_digest)
    except ValueError:
        raise Refused('controller_unavailable', safe_reason='installed initial create anchor pin is unavailable') from None
    return enrolled


def enroll_node(node_id, *, scheduler_id, nomad_node_id, capabilities, anchor_executable='', anchor_digest=''):
    """Trusted Root enrollment узла в планировщике (#643), не HTTP payload.

    Id узла в Nomad и заявленные capabilities запуска -- факты установки;
    повторная привязка того же узла заменяет их целиком."""
    from uuid import UUID
    from mop.server.operations.models import ScheduledNode
    from mop.server.launch_spec import API_LAUNCH_CAPABILITIES
    if (type(scheduler_id) is not UUID or type(nomad_node_id) is not UUID
            or type(capabilities) not in (set, frozenset) or not set(capabilities) <= API_LAUNCH_CAPABILITIES):
        raise ValueError('typed scheduler identity and known launch capabilities required')
    if 'initial_create_anchor_v1' in capabilities:
        from mop.server.launch_spec import anchor_pin
        anchor_pin(anchor_executable, anchor_digest)
    elif anchor_executable or anchor_digest:
        raise ValueError('installed anchor capability required for anchor pins')
    ScheduledNode.objects.update_or_create(node_id=node_id, defaults=dict(scheduler_id=scheduler_id,
        nomad_node_id=nomad_node_id, capabilities=sorted(capabilities),
        anchor_executable=anchor_executable, anchor_digest=anchor_digest))


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
        from mop.server.launch_spec import api_job_spec
        node = Node.objects.get(pk=target.node_id)
        enrolled = ScheduledNode.objects.get(node=node, scheduler_id=scheduler_id)
        settings = self._settings()
        launch = IncarnationLaunch.objects.get(incarnation_id=target.incarnation_id)
        name = Assignment.objects.values_list('display_name', flat=True).get(pk=target.target_id)
        definition = AgentDefinitionVersion.objects.get(pk=definition_id)
        from mop.server.operations.launch import image_pin
        from mop.server.operations.service import Refused
        # #708: неизменный образ и его policy -- один пин. Текущий образ узла
        # не вправе подменять ресурсы admission; смена доступности -- отказ.
        image = NodeImage.objects.filter(node=node, project_id=target.project_id, available=True,
            desired_image_id=launch.image_revision_id, observed_image_id=launch.image_revision_id).first()
        if (launch.image_revision_id is None or image is None
                or image_pin(launch.image_revision_id) != launch.image_digest
                or launch.image_revision.policy.project_id != target.project_id):
            raise Refused('revision_conflict', safe_reason='the pinned node image is no longer available')
        policy = launch.image_revision.policy
        work = LaunchWork(str(epoch), str(operation_id), str(step_id), str(target.target_id),
            str(target.incarnation_id), str(target.node_id), str(target.executor_epoch), str(target.grant_id),
            target.owner_id, target.generation, target.revision, target.role, target.bundle_digest,
            'native_exec', not_after.isoformat())
        registration = Registration(work, str(launch.bootstrap_revision), launch.bootstrap_digest,
                                    launch.image_digest, name)
        return api_job_spec(layout=RegisteredLayout(Path(settings['pool_home']), registration),
            machine=MachineConfig(node.control_origin, node.lan_address, node.bootstrap_address,
                                  node.ssh_port, node.driver),
            bundle=BundlePin(definition.role, definition.digest, definition.driver),
            nomad_node_id=str(enrolled.nomad_node_id), datacenter=settings['pool_datacenter'],
            user=settings['pool_user'], memory_mb=policy.memory_mb, cpu=policy.cpu,
            restore_generation=restore_generation, capabilities=frozenset(enrolled.capabilities),
            anchor_executable=enrolled.anchor_executable, anchor_digest=enrolled.anchor_digest)


class LaunchSupport:
    """Точные пины каждой фазы create (#721): planned без job, registered с job.

    Подготовка не требует scheduler receipt, native/readiness его не обходят."""
    def selection(self, target, operation):
        from mop.server.access.services import Denied
        from mop.server.sessions.models import IncarnationLaunch
        from mop.server.catalog.models import NodeImage
        from mop.server.operations.launch import image_pin
        reservation = operation.createbinding.reservation
        if reservation is None or reservation.policy_id is None:
            raise Denied('pinned initial launch selection is unavailable')
        selected = LaunchSelections().read(reservation.policy_id, git_name=reservation.git_name, git_email=reservation.git_email)
        LaunchSelections().require_reservation(selected, reservation)
        LaunchSelections().require_grant(selected, target.grant_id)
        launch = IncarnationLaunch.objects.get(incarnation_id=target.incarnation_id)
        if ((selected.project_id, selected.owner_id, selected.definition_id, selected.digest) !=
                (target.project_id, target.owner_id, operation.createbinding.definition_id, reservation.policy_digest)
                or selected.bootstrap_revision != launch.bootstrap_revision
                or selected.bootstrap_policy.digest != launch.bootstrap_digest or selected.image_id != launch.image_revision_id
                or image_pin(selected.image_id) != launch.image_digest
                or not NodeImage.objects.filter(node_id=target.node_id, project_id=target.project_id, available=True,
                    desired_image_id=selected.image_id, observed_image_id=selected.image_id).exists()):
            raise Denied('pinned initial launch selection or node image changed')
        return selected

    def require_planned(self, target, operation):
        from mop.server.operations.models import ScheduledPuppet
        from mop.server.operations.service import Refused
        self.selection(target, operation)
        require_anchor(target.node_id, operation.createbinding.scheduler_id)
        if ScheduledPuppet.objects.filter(target_id=target.target_id).exists():
            raise Refused('revision_conflict', safe_reason='planned create already has a scheduler job')

    def require(self, target, operation):
        from mop.server.operations.models import ScheduledPuppet
        from mop.server.operations.service import Refused
        self.selection(target, operation)
        binding = operation.createbinding
        enrolled = require_anchor(target.node_id, binding.scheduler_id)
        if binding.applied_index is None or binding.dispatched_job is None:
            raise Refused('controller_unavailable', safe_reason='the registered launch binding is unavailable')
        if (not ScheduledPuppet.objects.filter(
                target_id=target.target_id, incarnation_id=target.incarnation_id, epoch=operation.epoch,
                restore_generation=operation.restore_generation, scheduler_id=binding.scheduler_id,
                job_id=binding.job_id, namespace=binding.namespace, modify_index=binding.applied_index).exists()
                or binding.dispatched_job.get('Constraints') != [dict(
                    LTarget='${node.unique.id}', Operand='=', RTarget=str(enrolled.nomad_node_id))]):
            raise Refused('revision_conflict', safe_reason='the exact registered job or placement of the new target changed')


def service(*, grants=None, issuer=None, scheduler=None, activator=None):
    from .adminservices import credential_client
    grants = grants or local_grants()
    operations = operations_service(grants=grants.local_authority())
    operations.creates = CreatePuppets(operations, grants=grants, targets=LaunchTargets(),
        issuer=issuer if issuer is not None else credential_client(), scheduler=scheduler, selections=LaunchSelections())
    targets = Targets(grants)
    # Конечные planned/registered machine фазы идут по общему порту целей;
    # post-launch apply_identity сохраняет отдельный active-grant допуск.
    operations.targets = targets
    operations.launches = LaunchJobs(operations, scheduler=scheduler, targets=targets, specs=LaunchSpecs())
    operations.launch_machine = CreateMachine(operations, support=LaunchSupport())
    # Активатор -- credential-сервис под служебным credential; связывает установка (#644).
    operations.activations = GrantActivations(operations, targets=targets, activator=activator)
    return operations
