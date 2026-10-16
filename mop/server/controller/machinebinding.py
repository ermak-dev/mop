"""Composition root Operations -> Sessions; никаких private ORM imports."""
from dataclasses import asdict, dataclass

from mop.server.access.delegation import RoleBinding
from mop.server.access.grants import Grants
from mop.server.operations.puppet import RestartTarget, MachineStep
from mop.server.sessions.protocol import WorkV1
from mop.server.sessions.services import Assignments
from .runtime import operations_service, machine_execution_service


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str


def owner_identity(owner_id):
    from django.contrib.auth import get_user_model
    from mop.server.operations.service import Refused
    owner = get_user_model().objects.get(pk=owner_id, is_active=True)
    value = GitIdentity(owner.get_full_name().strip() or owner.username, owner.email)
    if any(type(item) is not str or not 1 <= len(item) <= 256 or
            any(ord(char)<32 or ord(char)==127 for char in item) for item in (value.name,value.email)):
        raise Refused('controller_unavailable', safe_reason='current owner Git identity is unavailable')
    return value


class AssignmentBindings:
    """Access читает live binding без обратного захвата resource locks."""
    def current(self, project_id, target_id):
        value = Assignments().current(project_id=project_id, target_id=target_id)
        if value is None:
            return None
        return RoleBinding(value.project_id, value.target_id, value.incarnation_id,
            value.role, value.bundle_digest, value.grant_id, value.owner_id)


class Targets:
    def __init__(self, grants):
        self.grants = grants

    def _target(self, target_id, lock):
        value = Assignments().target(target_id, lock=lock)
        from mop.server.catalog.services import Catalog
        if value is None or not Catalog.node_is_current(value.node_id):
            return None
        return RestartTarget(**{name: getattr(value, name) for name in RestartTarget.__dataclass_fields__})

    def current(self, target_id):
        return self._target(target_id, False)

    def locked(self, target_id):
        return self._target(target_id, True)

    def authorize(self, target):
        self.grants.authorize_execution(RoleBinding(target.project_id, target.target_id, target.incarnation_id,
            target.role, target.bundle_digest, target.grant_id, target.owner_id))
        owner_identity(target.owner_id)


class ReceivingGrant:
    def __init__(self, grants):
        self.grants = grants

    def authorize(self, *, owner_id, project_id, target_id, grant_id, role, bundle_digest, incarnation_id, action):
        # Получатель не обязан иметь исходящую restart/send capability.
        self.grants.authorize_execution(RoleBinding(project_id, target_id, incarnation_id, role,
            bundle_digest, grant_id, owner_id))


class OperationWork:
    def __init__(self, operations):
        self.operations = operations

    def pending(self, **scope):
        return tuple(WorkV1(**asdict(value)) for value in self.operations.pending_machine(**scope))

    def current(self, operation_id, step_id):
        return WorkV1(**asdict(self.operations.current_machine(operation_id, step_id)))

    def claimed(self, work, attempt_id):
        self.operations.claimed_machine(MachineStep(**asdict(work)), attempt_id)

    def completed_result(self, work, attempt_id, outcome, evidence):
        self.operations.completed_machine(MachineStep(**asdict(work)), attempt_id, outcome, evidence)

    def retired(self, target_id, executor_epoch):
        self.operations.retired_machine(target_id, executor_epoch)


def operations(*, grants=None, restart_support=None):
    grants = grants or Grants(provider=None, bindings=AssignmentBindings())
    return operations_service(grants=grants.local_authority(), targets=Targets(grants), restart_support=restart_support)


def authority():
    from uuid import UUID
    from django.db import transaction
    from mop.server.sessions.protocol import AuthorityV1
    from mop.server.sessions.services import Unavailable
    from .runtime import installation_snapshot, require_restore_ready
    try:
        if transaction.get_connection().in_atomic_block:
            require_restore_ready(lock=True)
        value = installation_snapshot()
        return AuthorityV1(UUID(value['epoch']), value['effects_enabled'])
    except RuntimeError as error:
        raise Unavailable(str(error)) from None


def compose(*, grants=None, restart_support=None):
    grants = grants or Grants(provider=None, bindings=AssignmentBindings())
    return machine_execution_service(work=OperationWork(operations(grants=grants, restart_support=restart_support)),
        grants=ReceivingGrant(grants), installation=authority)
