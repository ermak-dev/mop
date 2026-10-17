"""Значения и конечная policy делегирования; без ORM, транспорта и role subclasses."""
from dataclasses import dataclass, field
from uuid import UUID

from mop.common import soul


def uuid_value(value):
    if type(value) is not UUID:
        raise ValueError('complete UUID binding required')


@dataclass(frozen=True)
class RoleBinding:
    project_id: UUID
    target_id: UUID
    incarnation: UUID
    role: str
    digest: str
    grant_id: UUID
    human_owner_id: int

    def __post_init__(self):
        for value in (self.project_id, self.target_id, self.incarnation, self.grant_id):
            uuid_value(value)
        if (not isinstance(self.role, str) or not soul.NAME.fullmatch(self.role)
                or not isinstance(self.digest, str) or not soul.HEX64.fullmatch(self.digest)
                or type(self.human_owner_id) is not int or self.human_owner_id < 1):
            raise ValueError('complete role/digest/incarnation binding required')


@dataclass(frozen=True)
class ApprovedPolicy:
    approval_id: UUID
    version_id: UUID
    digest: str
    owner_id: int
    project_id: UUID

    def __post_init__(self):
        for value in (self.approval_id, self.version_id, self.project_id):
            uuid_value(value)
        if (not isinstance(self.digest, str) or not soul.HEX64.fullmatch(self.digest)
                or type(self.owner_id) is not int or self.owner_id < 1):
            raise ValueError('exact approved policy reference required')


@dataclass(frozen=True)
class PolicyBinding:
    """Pinned намерение: actor — сам Actor (#684), int id, не Principal UUID."""
    intent_id: UUID
    owner_id: int
    actor_id: int
    project_id: UUID
    bundle_id: UUID
    role: str
    digest: str
    parent_id: UUID | None
    target_id: UUID
    incarnation: UUID

    def __post_init__(self):
        for value in (self.intent_id, self.project_id, self.bundle_id,
                      self.target_id, self.incarnation):
            uuid_value(value)
        if self.parent_id is not None:
            uuid_value(self.parent_id)
        if type(self.actor_id) is not int or self.actor_id < 1:
            raise ValueError('complete creation actor identity required')
        RoleBinding(self.project_id, self.target_id, self.incarnation, self.role,
                    self.digest, self.intent_id, self.owner_id)


@dataclass(frozen=True)
class RetirementReceipt:
    binding: RoleBinding
    revision: int
    outcome: str

    def __post_init__(self):
        if (type(self.binding) is not RoleBinding or type(self.revision) is not int or self.revision < 1
                or self.outcome not in ('applied', 'refused', 'unknown')):
            raise ValueError('complete transport retirement receipt required')


@dataclass(frozen=True)
class BundleMetadata:
    id: UUID
    role: str
    digest: str
    project_id: UUID
    owner_id: int


@dataclass(frozen=True)
class BundleContent:
    id: UUID
    role: str
    digest: str
    capabilities: soul.Capabilities
    runtime: soul.Runtime
    secrets: soul.Secrets
    manifest: bytes = field(repr=False)
    prompt: bytes = field(repr=False)
    provenance: str = field(repr=False)


@dataclass(frozen=True)
class GrantInfo:
    id: UUID
    owner_id: int
    principal_id: int  # машина делегирования: Actor id (#684), не Principal
    project_id: UUID
    target_id: UUID
    incarnation: UUID
    bundle_id: UUID
    role: str
    digest: str
    parent_id: UUID | None
    state: str
    revision: int
    credential_version_id: UUID | None
    policy: ApprovedPolicy
    retirement: str

    @property
    def binding(self):
        return RoleBinding(self.project_id, self.target_id, self.incarnation, self.role,
                           self.digest, self.id, self.owner_id)


@dataclass(frozen=True)
class AddressedPolicy:
    """Sealed allowlist и точные текущие версии всей цепочки, без secret bytes."""
    required_names: tuple[tuple[str, str], ...]
    grants: tuple[GrantInfo, ...]


@dataclass(frozen=True)
class MessageAuthority:
    source: RoleBinding
    target: RoleBinding
    human_owner_id: int
    claim_assignment: bool
    git_identity_owner_id: int | None


# CONTROL принадлежит серверу. Нет общего execute, admin/write/terminal bypass
# или вывода прав из имени роли. Неперенесённые действия закрыты по умолчанию.
ACTION_CAPABILITIES = {
    'catalog.view_pool': ('cluster', 'roster'),
    'catalog.view_project': ('cluster', 'roster'),
    'execution.create_puppet': ('cluster', 'add'),
    'execution.update_puppet': ('cluster', 'update'),
    'execution.restart_puppet': ('cluster', 'restart'),
    'execution.delete_puppet': ('cluster', 'delete'),
    'execution.stop_allocation': ('cluster', 'stop'),
    'execution.wipe_puppet': ('node', 'wipe'),
    'execution.send_message': ('node', 'send'),
    'execution.send_session_command': ('node', 'type'),
    'execution.change_session_model': ('node', 'type'),
    'execution.view_usage': ('node', 'usage'),
    'execution.view_landing': ('cluster', 'landing'),
    'execution.acquire_landing': ('cluster', 'landing'),
    'execution.release_landing': ('cluster', 'landing'),
    'execution.dispatch_task': ('dispatch', ''),
}


def allows_capabilities(caps, action):
    capability = ACTION_CAPABILITIES.get(action)
    if capability is None:
        return False
    scope, verb = capability
    if scope == 'dispatch':
        return caps.dispatch
    return verb in (caps.node_verbs if scope == 'node' else caps.cluster_verbs)


def allows(bundle, action):
    return allows_capabilities(bundle.capabilities, action)


def allows_spawn(parent, child):
    return (allows(parent, 'execution.create_puppet') and child.role in parent.capabilities.spawn
            and set(child.secrets.files) <= set(parent.secrets.files)
            and set(child.secrets.vars) <= set(parent.secrets.vars))


def next_state(state, event):
    transitions = {('reserved', 'activate'): 'active', ('reserved', 'revoke'): 'revoked',
                   ('reserved', 'expire'): 'expired', ('active', 'revoke'): 'revoked',
                   ('active', 'expire'): 'expired'}
    try:
        return transitions[state, event]
    except KeyError:
        raise ValueError('grant transition is terminal or unavailable') from None
