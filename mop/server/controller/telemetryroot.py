"""SQL-only composition producer/telemetry. Нет provider или remote OS I/O.

Отчёт процесса доверен enrolled узлу, не hook JSON. Контроллер проверяет свой
current claim/launch/job/resource fences; существование pid на узле не заявляет.
"""
from django.db import transaction
from django.utils import timezone

from mop.server.access.services import Denied
from mop.server.sessions.models import IncarnationLaunch
from mop.server.sessions.producer import CurrentSessions, ProducerRegistration
from mop.server.sessions.telemetry import Telemetry
from mop.server.sessions.services import Assignments, Conflict, Unavailable
from .machinebinding import authority, compose


class CurrentLaunch:
    """Публичные execution/work ports и root-only склейка владельцев SQL.

    Пока доступен только claimed create apply_identity/confirm_ready. Это не
    разрешение регистрироваться из произвольной рабочей stage или старого job.
    """
    def check(self, actor, work, report):
        from mop.server.operations import models as om
        from mop.server.operations.launch import image_pin
        from mop.server.preparation.models import BootstrapApproval
        from .createbinding import LaunchSupport
        from .models import RestoreGuard
        if not transaction.get_connection().in_atomic_block:
            raise Unavailable('producer launch proof requires caller transaction')
        if not om.ScheduledPuppet.objects.filter(target_id=work.target_id, incarnation_id=work.incarnation_id).exists():
            raise Conflict('current fixed launch job incarnation changed')
        execution = compose(launch_support=LaunchSupport())
        current, attempt_id = execution.action_admission(actor,
            operation_id=work.operation_id, step_id=work.step_id)
        if current != work:
            raise Conflict('producer addressed launch work changed')
        # Этот public port сверяет durable attempt/token, live stage и четыре
        # shared resource fences; для произвольной non-create work он закрыт.
        execution.work.launch_definition(work, attempt_id)
        binding = Assignments().target(work.target_id)
        launch = IncarnationLaunch.objects.select_related('image_revision__policy').get(incarnation_id=work.incarnation_id)
        expected = (str(launch.bootstrap_revision), launch.bootstrap_digest, launch.image_digest)
        if (expected != (str(report.bootstrap_revision), report.bootstrap_digest, report.image_digest)
                or report.display_name != execution_assignment_name(work.target_id)):
            raise Conflict('producer launch pins changed')
        approved = BootstrapApproval.objects.filter(revision_id=launch.bootstrap_revision,
            human_id=binding.owner_id, revision__owner_id=binding.owner_id,
            revision__project_id=binding.project_id, revision__digest=launch.bootstrap_digest,
            approvalrevocation__isnull=True).exists()
        if not approved:
            raise Denied('pinned producer bootstrap approval is not current')
        if (launch.image_revision_id is None or launch.image_revision.policy.project_id != binding.project_id
                or image_pin(launch.image_revision_id) != launch.image_digest):
            raise Denied('pinned producer image changed')
        job = om.ScheduledPuppet.objects.select_for_update().filter(target_id=work.target_id).first()
        launch_job = om.CreateBinding.objects.select_for_update().get(operation_id=work.operation_id)
        operation = om.Operation.objects.get(pk=work.operation_id)
        if (job is None or job.incarnation_id != work.incarnation_id or job.epoch != work.epoch
                or job.restore_generation != operation.restore_generation
                or job.restore_generation != RestoreGuard.objects.get(pk=1).generation
                or job.scheduler_id != launch_job.scheduler_id or job.job_id != str(work.target_id)
                or job.job_id != launch_job.job_id or job.namespace != launch_job.namespace
                or job.modify_index != launch_job.applied_index
                or not om.ScheduledNode.objects.filter(node_id=work.node_id, scheduler_id=job.scheduler_id).exists()):
            raise Conflict('current fixed launch job or scheduler enrollment changed')
        # Удерживаемый claim должен принадлежать реально dispatched scheduler
        # stage этого create, а не строке с совпавшим operation UUID.
        claims = om.ResourceClaim.objects.filter(operation_id=work.operation_id, released_at=None)
        for claim in claims.select_related('step'):
            step = claim.step
            if (step.operation_id != work.operation_id or step.token != claim.token
                    or step.attempt_id != claim.token or step.name != 'scheduler_register'
                    or step.outcome != 'succeeded' or step.quiescent_at is None):
                raise Conflict('producer launch resource fence token changed')
        if work.not_after <= timezone.now():
            raise Conflict('producer launch deadline expired')
        return attempt_id


def execution_assignment_name(target_id):
    from mop.server.sessions.models import Assignment
    return Assignment.objects.values_list('display_name', flat=True).get(pk=target_id)


current_sessions = CurrentSessions(installation=authority)
producer_registration = ProducerRegistration(installation=authority, launch=CurrentLaunch())
telemetry = Telemetry(installation=authority, sessions=current_sessions)
