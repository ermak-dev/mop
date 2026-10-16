"""Spawn workers: собственное connection, новая test DB, никакого старого пула."""

def service(database, bootstrap, origin, scheduler_id, approvals):
    from _operation_process import _configure
    _configure(database,bootstrap)
    from mop.server.controller.stopbinding import service
    from mop.server.operations.scheduler import NomadStopScheduler
    from _stop_fixture import BodyInspector
    from mop.server.access.grants import Grants
    from mop.server.controller.machinebinding import AssignmentBindings
    # Тот же внешний approved-policy port, не permissive authorization.
    from _grant_cases import ApprovalFixture
    from mop.server.access.grants import ApprovedPolicy
    grants = Grants(provider=None,approvals=ApprovalFixture(ApprovedPolicy(*approvals)),bindings=AssignmentBindings())
    return service(grants=grants,scheduler=NomadStopScheduler(origin=origin,scheduler_id=scheduler_id,token='fixture-only',
        body_inspector=BodyInspector(origin),timeout=2))


def claim(database, bootstrap, origin, scheduler_id, approvals, operation_id, ready, result):
    operations = service(database,bootstrap,origin,scheduler_id,approvals)
    from django.db import connections
    try:
        ready.wait(10)
        ticket = operations.stops.claim(operation_id)
        result.put(None if ticket is None else str(ticket.token))
    finally:
        connections.close_all()


def submit(database, bootstrap, origin, scheduler_id, approvals, actor, command, ready, result):
    operations = service(database,bootstrap,origin,scheduler_id,approvals)
    from django.db import connections
    try:
        ready.wait(10)
        result.put(operations.submit(actor,command)['id'])
    finally:
        connections.close_all()


def crash(database, bootstrap, origin, scheduler_id, approvals, operation_id):
    operations = service(database,bootstrap,origin,scheduler_id,approvals)
    ticket = operations.stops.claim(operation_id)
    assert ticket is not None
    assert operations.stops.prepare(ticket)
    proof = operations.stops.scheduler.stop_job(ticket)
    assert proof.outcome=='succeeded'
    # Нет result COMMIT и finally; родитель не имеет права повторять write.
    __import__('os')._exit(73)
