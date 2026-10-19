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


class PreparationApprovals:
    """Реальный SQL query Preparation; его отказ получает тип границы Access."""
    def __init__(self, query):
        self.query = query

    def pin(self, binding):
        from mop.server.access.services import Denied
        try:
            return self.query.pin(binding)
        except PermissionError as error:
            raise Denied(str(error)) from error

    def validate(self, proof, binding):
        from mop.server.access.services import Denied
        try:
            return self.query.validate(proof, binding)
        except PermissionError as error:
            raise Denied(str(error)) from error


def local_grants():
    """SQL-only композиция живого exact approval; root/provider здесь нет."""
    from mop.server.access.services import Access
    from mop.server.preparation.services import Preparation
    query = Preparation(provider=None, authorizer=Access()).approval_query()
    return Grants(provider=None, approvals=PreparationApprovals(query), bindings=AssignmentBindings())


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

    def authorize_launch(self, target):
        self.grants.authorize_launch(RoleBinding(target.project_id, target.target_id, target.incarnation_id,
            target.role, target.bundle_digest, target.grant_id, target.owner_id))


class ReceivingGrant:
    def __init__(self, grants):
        self.grants = grants

    def authorize(self, *, owner_id, project_id, target_id, grant_id, role, bundle_digest, incarnation_id, action):
        # Получатель не обязан иметь исходящую restart/send capability.
        binding = RoleBinding(project_id, target_id, incarnation_id, role, bundle_digest, grant_id, owner_id)
        # #643: идентичность и готовность новой цели -- по выданному, ещё не
        # активному гранту; любое другое действие -- только по активному.
        from mop.server.access.models import Grant
        from mop.common.initialcreate import MACHINE
        if (action in MACHINE
                and Grant.objects.filter(pk=grant_id, state='reserved').exists()):
            self.grants.authorize_launch(binding)
            return
        self.grants.authorize_execution(binding)


class OperationWork:
    def __init__(self, operations):
        self.operations = operations

    def pending(self, **scope):
        return tuple(WorkV1(**asdict(value)) for value in self.operations.pending_machine(**scope))

    def current(self, operation_id, step_id):
        return WorkV1(**asdict(self.operations.current_machine(operation_id, step_id)))

    def abort_unstarted(self, work):
        return self.operations.abort_unstarted_create(MachineStep(**asdict(work)))

    def reconcile_expired(self, *, limit):
        return self.operations.reconcile_expired_creates(limit=limit)

    def claimed(self, work, attempt_id):
        self.operations.claimed_machine(MachineStep(**asdict(work)), attempt_id)

    def completed_result(self, work, attempt_id, outcome, evidence):
        self.operations.completed_machine(MachineStep(**asdict(work)), attempt_id, outcome, evidence)

    def retired(self, target_id, executor_epoch):
        self.operations.retired_machine(target_id, executor_epoch)

    def effect_lease(self, work):
        return self.operations.effect_lease(MachineStep(**asdict(work)))

    def message_input(self, work):
        return self.operations.messages.input(MachineStep(**asdict(work)))

    def launch_definition(self, work, attempt_id):
        return self.operations.launch_machine.launch_definition(MachineStep(**asdict(work)), attempt_id)

    def initial_native_attempt(self, work):
        """Historical locator принадлежит Operations, не composition SQL."""
        from mop.server.operations.launch import CreateMachine
        return CreateMachine(self.operations).initial_native_attempt(MachineStep(**asdict(work)))

    def ready_launch(self, work, attempt_id):
        from mop.server.sessions.services import Unavailable
        if self.operations.launch_machine is None:
            raise Unavailable('initial launch support is not bound')
        return self.operations.launch_machine.ready_launch(MachineStep(**asdict(work)), attempt_id)


def operations(*, grants=None, restart_support=None, message_support=None, message_cipher=None, endpoints=None,
               wipe_support=None, launch_support=None):
    from mop.server.operations.messages import MessageHandler
    from mop.server.operations.puppet import WipeHandler
    from .messagebinding import MessageRecipients
    grants = grants or local_grants()
    service = operations_service(grants=grants.local_authority(), targets=Targets(grants), restart_support=restart_support)
    service.messages = MessageHandler(service, cipher=message_cipher, support=message_support,
        recipients=MessageRecipients(grants), endpoints=endpoints)
    service.wipes = WipeHandler(service, support=wipe_support)
    # Узловые шаги create (#643) -- только со связанной полосой запуска: без
    # неё машинный маршрут по-прежнему честно отказывает.
    from mop.server.operations.launch import CreateMachine
    service.launch_machine = CreateMachine(service, support=launch_support) if launch_support is not None else None
    return service


def executor_discovery(execution, actor, node_id, *, host=False):
    """Один SQL admission для canonical installation и scoped executor; без effects."""
    from .models import Installation
    from .runtime import short_transaction, require_restore_ready
    from mop.server.sessions.services import Unavailable
    try:
        with short_transaction():
            require_restore_ready(lock=True)
            installation = Installation.objects.select_for_update().get(pk=1)
            # Сохраняем весь прежний machine/node/project gate и bounded ambiguity
            # check. Ни transport export, ни numeric pk не становятся identity.
            previous, empty = execution.host_executor_state(actor, node_id) if host else (
                execution.executor_state(actor, node_id), None)
            result = dict(schema_version=1, installation_id=installation.identity,
                epoch=str(installation.epoch), node_id=str(node_id), executor_epoch=str(previous))
            return dict(**result, empty=empty) if host else result
    except Installation.DoesNotExist:
        raise Unavailable('controller installation is not initialized') from None
    except RuntimeError as error:
        raise Unavailable(str(error)) from None


def authority():
    from uuid import UUID
    from django.db import transaction
    from mop.server.sessions.protocol import AuthorityV1
    from mop.server.sessions.services import Conflict, Unavailable
    from .runtime import installation_snapshot, require_restore_ready, RestoreGuardBusy
    try:
        if transaction.get_connection().in_atomic_block:
            require_restore_ready(lock=True)
        value = installation_snapshot()
        return AuthorityV1(UUID(value['epoch']), value['effects_enabled'])
    except RestoreGuardBusy as error:
        raise Conflict(str(error)) from None
    except RuntimeError as error:
        raise Unavailable(str(error)) from None


def compose(*, grants=None, restart_support=None, message_support=None, message_cipher=None, endpoints=None,
            wipe_support=None, launch_support=None):
    grants = grants or local_grants()
    return machine_execution_service(work=OperationWork(operations(grants=grants, restart_support=restart_support,
        message_support=message_support, message_cipher=message_cipher, endpoints=endpoints,
        wipe_support=wipe_support, launch_support=launch_support)),
        grants=ReceivingGrant(grants), installation=authority)
