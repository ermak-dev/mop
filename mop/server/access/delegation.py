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
    definition_id: UUID
    role: str
    digest: str
    parent_id: UUID | None
    target_id: UUID
    incarnation: UUID

    def __post_init__(self):
        for value in (self.intent_id, self.project_id, self.definition_id,
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
class DefinitionMetadata:
    id: UUID
    role: str
    digest: str
    project_id: UUID
    owner_id: int


@dataclass(frozen=True)
class AgentProfileInfo:
    """Программа агента как доменные данные; ни одного права внутри (#686)."""
    target_id: UUID
    agent_id: int
    definition_id: UUID
    revision: int
    project_id: UUID
    owner_id: int

    def __post_init__(self):
        uuid_value(self.target_id)
        uuid_value(self.definition_id)
        uuid_value(self.project_id)
        if type(self.agent_id) is not int or self.agent_id < 1:
            raise ValueError('stable agent actor reference required')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('profile revision required')


@dataclass(frozen=True)
class DefinitionContent:
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
    subject_id: int  # субъект делегирования: существующий agent Actor (#688), не User-per-Grant
    project_id: UUID
    target_id: UUID
    incarnation: UUID
    definition_id: UUID
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


# Технические предусловия программы агента (#688), не каталог прав. Допуск
# даёт только штатный Django RBAC (#685): действие в контексте делегирования
# требует права у субъекта каждого звена цепочки и у текущего human owner.
# Словарь лишь называет декларацию манифеста, без которой действие технически
# невозможно: порождать детей может только программа, объявившая cluster add
# (и spawn allowlist). Получение dispatch (runtime.receives_dispatch), allowlist
# секретов и approved версии программы — отдельные ограничения строк definition.
ACTION_CAPABILITIES = {
    'execution.create_puppet': ('cluster', 'add'),
}


def allows_capabilities(caps, action):
    """Программа объявила предусловие действия; не допуск — права решает RBAC."""
    capability = ACTION_CAPABILITIES.get(action)
    if capability is None:
        return False
    scope, verb = capability
    return verb in (caps.node_verbs if scope == 'node' else caps.cluster_verbs)


def allows(definition, action):
    return allows_capabilities(definition.capabilities, action)


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
