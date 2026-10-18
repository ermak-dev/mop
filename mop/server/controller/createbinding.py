"""Точка сборки create (#643): порт credential-сервиса и планировщика.

Без связанного credential-сервиса и планировщика с условной регистрацией
create отказывает до записей: наличие права не заменяет привязку.
"""
from .machinebinding import AssignmentBindings
from .runtime import assignments_service, machine_execution_service, operations_service
from mop.server.access.grants import Grants
from mop.server.operations.launch import CreatePuppets


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


def service(*, grants=None, issuer=None, scheduler=None):
    from .adminservices import credential_client
    grants = grants or Grants(provider=None, bindings=AssignmentBindings())
    operations = operations_service(grants=grants.local_authority())
    operations.creates = CreatePuppets(operations, grants=grants, targets=LaunchTargets(),
        issuer=issuer if issuer is not None else credential_client(), scheduler=scheduler)
    return operations
