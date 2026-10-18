"""Точка сборки: scheduler stop и SQL retirement назначения разделены."""
from .machinebinding import Targets, local_grants
from .runtime import operations_service
from mop.server.operations.stop import StopJobs


class StopTargets(Targets):
    def stopped(self, target):
        from mop.server.sessions.services import Assignments
        return Assignments().confirm_job_stopped(target_id=target.target_id,
            incarnation_id=target.incarnation_id,expected_revision=target.revision,
            expected_generation=target.generation,executor_epoch=target.executor_epoch)

    def deleted(self, target):
        from mop.server.sessions.services import Assignments
        return Assignments().confirm_deleted(target_id=target.target_id,
            incarnation_id=target.incarnation_id,expected_revision=target.revision,
            expected_generation=target.generation,executor_epoch=target.executor_epoch)


def service(*, grants=None, scheduler=None):
    grants = grants or local_grants()
    targets = StopTargets(grants)
    operations = operations_service(grants=grants.local_authority())
    operations.stops = StopJobs(operations,scheduler=scheduler,targets=targets)
    operations.deletes = StopJobs(operations,scheduler=scheduler,targets=targets,action='delete')
    return operations
