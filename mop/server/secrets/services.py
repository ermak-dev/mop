"""Encrypted store commands. Provider I/O никогда не входит в SQL-транзакцию.

Actor — доверенная ссылка composition root, не объект запроса. Authorizer
синхронно проверяет текущие ORM-полномочия по реальным refs. DeliveryGate
возвращает закреплённый серверный allowlist и проверяет authenticated node,
project/assignment/role/digest/incarnation, active grant и текущие права owner.
Без будущего #633/#635/#639 binding все порты закрыты. Machine token, operation
id и сами SecretBinding не являются доказательством назначения.

Нет root lookup, общего plaintext download, импорта прежних stores, HTTP или
Admin. Provider инжектируется только защищённой композицией; root_digest не ключ.
"""
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
import json
from typing import Protocol
import uuid

from django.utils import timezone

from django.db import DatabaseError, IntegrityError, connections, transaction
from django.db.models import Q

from ..catalog.services import readmission
from . import models as m
from .crypto import (CryptoProvider, Envelope, SecretContext, SecretCryptoError,
                     SecretCryptoUnavailable, MAX_PLAINTEXT_BYTES)


class Conflict(ValueError):
    """Ссылка/ожидаемая revision изменилась; перечитать, не повторять effect."""


class Refused(ValueError):
    """Адресный набор не соответствует текущему закреплённому назначению."""


class TransactionBoundary(ValueError):
    """Внешний provider запрещён даже внутри транзакции вызывающего кода."""


class StorageUnavailable(ValueError):
    """Без DSN, параметров запроса, ciphertext или exception context драйвера."""


def storage_errors(command):
    @wraps(command)
    def run(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except DatabaseError:
            pass
        raise StorageUnavailable('secret storage is unavailable')
    return run


class Authorizer(Protocol):
    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()): ...


@dataclass(frozen=True)
class DeliverySelection:
    """Только серверные факты из trusted gate, не десериализация пользовательского JSON."""
    binding_ids: tuple
    required_names: tuple[tuple[str, str], ...]
    # Trusted owner projections участвуют в post-provider equality. Не wire input
    # и не замена повторной проверки authority: каждый resolve делает её заново.
    current_proof: tuple = ()


class DeliveryGate(Protocol):
    def resolve(self, actor) -> DeliverySelection: ...


class Deny:
    def authorize(self, actor, action, **refs):
        raise PermissionError('secret authorizer is not bound')

    def resolve(self, actor):
        raise PermissionError('addressed secret delivery gate is not bound')

    def verify_current(self, actor, proof):
        raise PermissionError('current rotation recipient verifier is not bound')


@dataclass(frozen=True)
class SecretMetadata:
    id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    kind: str
    purpose: str
    revision: int
    active_version_id: uuid.UUID | None
    last_number: int
    revoked: bool
    size: int | None


@dataclass(frozen=True)
class VersionMetadata:
    id: uuid.UUID
    secret_id: uuid.UUID
    number: int
    size: int
    key_id: str


@dataclass(frozen=True)
class BindingMetadata:
    id: uuid.UUID
    secret_id: uuid.UUID
    name: str
    kind: str
    node_id: uuid.UUID | None
    client_id: uuid.UUID | None
    controller: bool
    role: str
    digest: str
    incarnation: uuid.UUID
    grant_id: uuid.UUID
    assignment_id: uuid.UUID
    retired_at: datetime | None


@dataclass(frozen=True)
class RotationProof:
    """Полный серверный контекст, не payload и не машинный token + operation id."""
    rotation_id: uuid.UUID
    rotation_revision: int
    secret_id: uuid.UUID
    secret_revision: int
    installation_id: int
    installation_identity: str
    project_id: uuid.UUID | None
    name: str
    kind: str
    purpose: str
    old_version: VersionMetadata
    new_version: VersionMetadata
    active_version_id: uuid.UUID
    recipients: tuple[BindingMetadata, ...]
    action: str
    effect: str | None
    claim_id: uuid.UUID | None
    generation: int


class CurrentRecipientVerifier(Protocol):
    """Trusted sync ORM-only порт, вызываемый под parent/rotation locks.

    Проверяет каждого frozen recipient из proof по текущим authoritative данным:
    project/node/client/controller, role/digest/incarnation, assignment, grant и
    его ancestors/owner, без scope override оператора. True — весь точный набор
    current, включая offline; False/отказ — ни одного нового effect. Никакой сети
    или ожидания. Порт блокирует authoritative ORM refs до commit (их mutators
    обязаны брать те же locks); внешний resolver требует preflight + ORM CAS fence.
    Композиция #639 ещё не подключена; отсутствие порта всегда deny.
    """
    def verify_current(self, actor, proof: RotationProof) -> bool: ...


@dataclass(frozen=True)
class AddressedSecret:
    """Значение только для отдельного защищённого внутреннего consumer, repr закрыт."""
    binding_id: uuid.UUID
    version_id: uuid.UUID
    name: str
    kind: str
    value: bytes = field(repr=False)


def metadata(row):
    version = row.active_version if row.active_version_id else None
    if version and version.secret_id != row.id:
        raise Refused('secret metadata version binding mismatch')
    return SecretMetadata(row.id, row.project_id, row.name, row.kind, row.purpose,
        row.revision, row.active_version_id, row.last_number, row.revoked,
        version.size if version else None)


def version_metadata(row):
    return VersionMetadata(*(getattr(row, f) for f in VersionMetadata.__dataclass_fields__))


def binding_metadata(row):
    return BindingMetadata(row.id, row.secret_id, row.secret.name, row.secret.kind,
        row.node_id, row.client_id, row.controller, row.role, row.digest,
        row.incarnation, row.grant_id, row.assignment_id, row.retired_at)


def expected(row, revision):
    if type(revision) is not int or revision < 0:
        raise ValueError('invalid expected secret revision')
    if row.revision != revision:
        raise Conflict('secret lifecycle revision changed')


def outside_transaction():
    if any(connections[alias].in_atomic_block or not connections[alias].get_autocommit()
           for alias in connections):
        raise TransactionBoundary('secret provider requires an outer transaction-free boundary')


def authorize(authorizer, actor, action, secret, bindings=()):
    authorizer.authorize(actor, 'secrets.' + action,
        project_ids=(secret.project_id,) if secret.project_id else (),
        node_ids=tuple(sorted({b.node_id for b in bindings if b.node_id}, key=str)),
        client_ids=tuple(sorted({b.client_id for b in bindings if b.client_id}, key=str)))


def audit(actor, action, secret, rotation=None):
    # Actor приходит с trusted boundary; произвольные поля payload не читаются.
    m.SecretAudit.objects.create(secret=secret, rotation=rotation, action=action,
        revision=rotation.revision if rotation else secret.revision,
        actor_id=getattr(actor, 'user_id', None))


def context(secret, version):
    """AAD строится заново из actual FK, не из сохранённого envelope/пользовательского draft."""
    purpose = json.dumps(['mop.secret.value.v1', str(secret.id),
        str(secret.project_id) if secret.project_id else 'installation',
        secret.kind, secret.name, secret.purpose], separators=(',', ':')).encode()
    return SecretContext(secret.installation.identity.encode(), version.id.bytes, purpose, version.number)


def available(secret):
    if secret.revoked:
        raise Conflict('secret is revoked')


def no_rotation(secret):
    if m.CredentialRotation.objects.filter(secret=secret, resolved=False).exists():
        raise Conflict('secret has an unresolved credential rotation')


def value_bytes(value):
    if type(value) is not bytes or len(value) > MAX_PLAINTEXT_BYTES:
        raise ValueError('invalid secret bytes or size')


def seal(provider, value, aad):
    result = None
    try:
        result = provider.encrypt(value, aad)
    except SecretCryptoError:
        raise
    except Exception:
        pass
    if type(result) is not Envelope:
        raise SecretCryptoError('secret encryption failed')
    return result


def reveal(provider, envelope, aad):
    try:
        result = provider.decrypt(envelope, aad)
    except SecretCryptoError:
        raise
    except Exception:
        pass
    else:
        if type(result) is not bytes or len(result) > MAX_PLAINTEXT_BYTES:
            raise SecretCryptoError('secret decryption failed')
        return result
    raise SecretCryptoUnavailable('secret decryption failed')


@storage_errors
def verify_storage(provider_for_key, *, installation_id, using='default'):
    """Maintenance-only проверка quiescent архива; наружу только число версий."""
    outside_transaction()
    after, count = None, 0
    while True:
        rows = m.SecretVersion.objects.using(using).select_related('secret__installation').order_by('pk')
        if after is not None:
            rows = rows.filter(pk__gt=after)
        batch = tuple(rows[:8])
        if not batch:
            return count
        for version in batch:
            if version.secret.installation.identity != installation_id:
                raise SecretCryptoError('secret recovery installation mismatch')
            envelope = Envelope.from_bytes(bytes(version.envelope))
            if envelope.key_id != version.key_id or envelope.nonce != bytes(version.nonce):
                raise SecretCryptoError('secret recovery envelope metadata mismatch')
            value = reveal(provider_for_key(envelope.key_id), envelope, context(version.secret, version))
            if len(value) != version.size:
                raise SecretCryptoError('secret recovery plaintext size mismatch')
            count += 1
        after = batch[-1].pk


@dataclass(frozen=True)
class RetentionScope:
    """Адрес одного внутреннего агрегата; не право пользователя на secret management."""
    project_id: uuid.UUID
    owner_id: int
    subject_id: uuid.UUID
    purpose: str

    def __post_init__(self):
        if (type(self.project_id) is not uuid.UUID or type(self.subject_id) is not uuid.UUID
                or type(self.owner_id) is not int or self.owner_id < 1
                or self.purpose not in ('policy', 'grant')):
            raise Refused('invalid retained secret scope')

    @property
    def name(self):
        return f'retained.{self.purpose}.{self.owner_id}.{self.subject_id.hex}'


@dataclass(frozen=True)
class PreparedRetention:
    """Только ciphertext и точные AAD inputs; plaintext здесь не удерживается."""
    scope: RetentionScope
    secret_id: uuid.UUID
    version_id: uuid.UUID
    size: int
    envelope: Envelope = field(repr=False)
    installation_identity: str


class RetentionAuthority(Protocol):
    def check(self, actor, scope: RetentionScope): ...


class _DenyRetention:
    def check(self, actor, scope):
        raise PermissionError('retained secret authority is not bound')


class RetainedSecrets:
    """Публичный narrow port prepare/commit/read для связанного агрегата.

    Root связывает authority конкретного владельца use case. Это не callback
    из JSON и не global admin. prepare/read вызываются вне SQL/provider waits;
    commit повторяет authority и пишет только ранее подготовленный ciphertext
    внутри транзакции вызывающего агрегата. Secrets не импортирует Access.
    """
    def __init__(self, *, provider: CryptoProvider, authority: RetentionAuthority | None = None):
        self.provider = provider
        self.authority = authority if authority is not None else _DenyRetention()

    def _secret(self, scope, secret_id):
        if type(scope) is not RetentionScope:
            raise Refused('trusted retention scope required')
        return m.Secret(id=secret_id, installation_id=1, project_id=scope.project_id,
                        name=scope.name, kind='credential', purpose=scope.purpose)

    @storage_errors
    def prepare(self, actor, scope, value):
        outside_transaction()
        value_bytes(value)
        self.authority.check(actor, scope)
        row = self._secret(scope, uuid.uuid4())
        row.project
        version = m.SecretVersion(secret=row, number=1, size=len(value))
        aad = context(row, version)
        encrypted = seal(self.provider, value, aad)
        return PreparedRetention(scope, row.id, version.id, len(value), encrypted, row.installation.identity)

    @storage_errors
    def commit(self, actor, prepared):
        if not connections['default'].in_atomic_block or type(prepared) is not PreparedRetention:
            raise TransactionBoundary('retained secret commit requires aggregate transaction')
        self.authority.check(actor, prepared.scope)
        row = self._secret(prepared.scope, prepared.secret_id)
        if row.installation.identity != prepared.installation_identity:
            raise Conflict('retained secret installation changed')
        row.save(force_insert=True)
        env = prepared.envelope
        version = m.SecretVersion(id=prepared.version_id, secret=row, number=1, size=prepared.size,
                                  envelope=env.to_bytes(), key_id=env.key_id, nonce=env.nonce)
        version.save()
        row.active_version, row.last_number = version, 1
        row.save(update_fields=['active_version', 'last_number'])
        audit(actor, 'create', row)
        return version.id

    def _reference(self, scope, version_id):
        """Проверка exact reference без plaintext/provider; годится внутри SQL."""
        if type(scope) is not RetentionScope:
            raise Refused('trusted retention scope required')
        version = m.SecretVersion.objects.select_related('secret__installation').get(pk=version_id)
        row = version.secret
        if (row.project_id, row.kind, row.name, row.purpose) != (scope.project_id, 'credential', scope.name, scope.purpose):
            raise Refused('retained secret target mismatch')
        available(row)
        return version

    @storage_errors
    def check_reference(self, scope, version_id):
        self._reference(scope, version_id)

    @storage_errors
    def check_project_references(self, version_ids, *, project_id):
        """Локальный metadata-only guard для immutable preparation references.

        Не выдаёт значения или authority; caller отдельно проверяет action и
        actual project. Установочные keys/токены не являются project input.
        """
        ids = tuple(version_ids)
        if len(ids) > 32 or any(type(value) is not uuid.UUID for value in ids) or len(set(ids)) != len(ids):
            raise Refused('invalid project secret references')
        rows = tuple(m.SecretVersion.objects.select_related('secret').filter(pk__in=ids))
        if len(rows) != len(ids):
            raise Refused('project secret reference is unavailable')
        for version in rows:
            row = version.secret
            if (row.project_id != project_id or row.purpose != 'project'
                    or row.kind not in ('variable', 'file') or row.active_version_id != version.id):
                raise Refused('project secret reference mismatch')
            available(row)

    @storage_errors
    def validate_policy_reference(self, version_id, *, project_id):
        version = m.SecretVersion.objects.select_related('secret').get(pk=version_id)
        if version.secret.project_id != project_id or version.secret.purpose != 'policy':
            raise Refused('approved policy secret reference mismatch')
        available(version.secret)

    @storage_errors
    def read(self, actor, scope, version_id):
        outside_transaction()
        self.authority.check(actor, scope)
        version = self._reference(scope, version_id)
        envelope = Envelope.from_bytes(bytes(version.envelope))
        if envelope.key_id != version.key_id or envelope.nonce != bytes(version.nonce):
            raise SecretCryptoError('secret envelope metadata mismatch')
        value = reveal(self.provider, envelope, context(version.secret, version))
        if len(value) != version.size:
            raise SecretCryptoError('secret envelope size mismatch')
        self.authority.check(actor, scope)
        self._reference(scope, version_id)
        return value


@dataclass(frozen=True)
class InitialConsumerScope:
    """Внутренний finite consumer выбранного initial launch, не wire secret lookup."""
    project_id: uuid.UUID
    owner_id: int
    policy_id: uuid.UUID
    purpose: str
    version_id: uuid.UUID


class InitialLaunchSecrets:
    """Purpose-specific read; authority принадлежит exact claimed фазе Root."""
    def __init__(self, *, provider, authority):
        self.provider, self.authority = provider, authority

    @storage_errors
    def read(self, actor, scope):
        from mop.common.initialcreate import CONSUMERS
        outside_transaction()
        if type(scope) is not InitialConsumerScope or scope.purpose not in CONSUMERS:
            raise Refused('finite initial launch consumer scope required')
        self.authority.check_consumer(actor, scope)
        version = m.SecretVersion.objects.select_related('secret__installation').get(pk=scope.version_id)
        row = version.secret
        expected_purpose = dict(source_credential='git', bootstrap_input='project', model_config='project', model_credential='proxy')
        if (row.project_id != scope.project_id or row.created_by_id != scope.owner_id
                or row.active_version_id != version.id or row.purpose != expected_purpose[scope.purpose]):
            raise Refused('selected initial launch consumer changed')
        available(row)
        envelope = Envelope.from_bytes(bytes(version.envelope))
        if envelope.key_id != version.key_id or envelope.nonce != bytes(version.nonce):
            raise SecretCryptoError('secret envelope metadata mismatch')
        value = reveal(self.provider, envelope, context(row, version))
        if len(value) != version.size:
            raise SecretCryptoError('secret envelope size mismatch')
        self.authority.check_consumer(actor, scope)
        return dict(purpose=scope.purpose, version_id=str(version.id), name=row.name, kind=row.kind, value=value)


class SecretStore:
    def __init__(self, *, provider: CryptoProvider, authorizer: Authorizer | None = None,
                 delivery_gate: DeliveryGate | None = None, controller_gate=None):
        self.provider = provider
        self.authorizer = authorizer if authorizer is not None else Deny()
        self.delivery_gate = delivery_gate if delivery_gate is not None else Deny()
        self.controller_gate = controller_gate if controller_gate is not None else Deny()

    def _proxy_selection(self, actor):
        # Только root-bound gate выбирает конкретный installation secret.
        secret_id = self.controller_gate.resolve(actor)
        if type(secret_id) is not uuid.UUID:
            raise Refused('invalid proxy management secret selection')
        row = m.Secret.objects.select_related('installation', 'active_version').filter(pk=secret_id).first()
        if row is None:
            raise Refused('proxy management secret is missing')
        available(row)
        if (row.installation_id != 1 or row.project_id is not None or row.purpose != 'proxy'
                or row.kind != 'variable' or row.active_version_id is None
                or row.active_version.secret_id != row.pk):
            raise Refused('proxy management secret scope or purpose mismatch')
        return row

    @storage_errors
    def proxy_management_current(self, actor, version_id):
        row = self._proxy_selection(actor)
        return row.active_version_id == version_id

    @storage_errors
    def issue_proxy_management(self, actor):
        """Узкий внутренний consumer, не plaintext GET или metadata permission."""
        outside_transaction()
        row = self._proxy_selection(actor)
        pin = row.id, row.revision, row.active_version_id
        value = self._decrypt(row, row.active_version)
        with transaction.atomic():
            m.Secret.objects.select_for_update().get(pk=row.pk)
            current = self._proxy_selection(actor)
            if pin != (current.id, current.revision, current.active_version_id):
                raise Conflict('proxy management secret changed during issuance')
            audit(actor, 'proxy_issue', current)
        return AddressedSecret(row.id, row.active_version_id, row.name, row.kind, value)

    @storage_errors
    def metadata(self, actor, secret_id):
        row = m.Secret.objects.get(pk=secret_id)
        authorize(self.authorizer, actor, 'view_secret_metadata', row)
        return metadata(row)

    @storage_errors
    def list(self, actor, *, project_id):
        # Установка и проект — разные relational namespaces; wildcard list нет.
        rows = tuple(m.Secret.objects.filter(project_id=project_id).order_by('id'))
        self.authorizer.authorize(actor, 'secrets.view_secret_metadata',
            project_ids=(project_id,) if project_id else (), node_ids=(), client_ids=())
        return tuple(metadata(row) for row in rows)

    @storage_errors
    def create(self, actor, *, project_id, name, kind, purpose, value):
        outside_transaction()
        value_bytes(value)
        row = m.Secret(installation_id=1, project_id=project_id, name=name, kind=kind, purpose=purpose,
                       created_by_id=getattr(actor, 'user_id', None))
        row.clean()
        # Явно разрешаем реальную project FK до provider I/O.
        if project_id:
            row.project
        authorize(self.authorizer, actor, 'manage_project_secret', row)
        version = m.SecretVersion(secret=row, number=1, size=len(value))
        aad = context(row, version)
        for _ in range(4):
            env = seal(self.provider, value, aad)
            nonce_collision = False
            try:
                with transaction.atomic():
                    authorize(self.authorizer, actor, 'manage_project_secret', row)
                    # INSERT заново после rollback; Django изменяет _state даже при abort.
                    row._state.adding = True
                    row.save(force_insert=True)
                    version._state.adding = True
                    version.envelope, version.key_id, version.nonce = env.to_bytes(), env.key_id, env.nonce
                    version.save()
                    row.active_version, row.last_number = version, 1
                    row.save(update_fields=['active_version', 'last_number'])
                    audit(actor, 'create', row)
                    return metadata(row)
            except IntegrityError:
                nonce_collision = m.SecretVersion.objects.filter(key_id=env.key_id, nonce=env.nonce).exists()
            if not nonce_collision:
                raise Conflict('secret identity already exists or reference changed')
        raise Conflict('secret nonce reservation exhausted')

    def _write(self, actor, secret_id, value, expected_revision, activate):
        outside_transaction()
        value_bytes(value)
        row = m.Secret.objects.select_related('installation').get(pk=secret_id)
        authorize(self.authorizer, actor, 'manage_project_secret', row)
        expected(row, expected_revision)
        available(row)
        no_rotation(row)
        if activate and (row.purpose != 'project' or row.kind not in ('variable', 'file')):
            raise Refused('credential replacement requires staged rotation')
        version = m.SecretVersion(secret=row, number=row.last_number + 1, size=len(value))
        aad = context(row, version)
        for _ in range(4):
            env = seal(self.provider, value, aad)
            nonce_collision = False
            try:
                with transaction.atomic():
                    current = m.Secret.objects.select_for_update().get(pk=secret_id)
                    authorize(self.authorizer, actor, 'manage_project_secret', current)
                    expected(current, expected_revision)
                    available(current)
                    no_rotation(current)
                    version._state.adding = True
                    version.envelope, version.key_id, version.nonce = env.to_bytes(), env.key_id, env.nonce
                    version.save()
                    current.last_number = version.number
                    current.revision += 1
                    if activate:
                        current.active_version = version
                    current.save(update_fields=['active_version', 'revision', 'last_number'])
                    audit(actor, 'update' if activate else 'stage', current)
                    return metadata(current) if activate else version_metadata(version)
            except IntegrityError:
                nonce_collision = m.SecretVersion.objects.filter(key_id=env.key_id, nonce=env.nonce).exists()
            if not nonce_collision:
                raise Conflict('secret version could not be committed')
        raise Conflict('secret nonce reservation exhausted')

    @storage_errors
    def stage(self, actor, secret_id, value, *, expected_revision):
        return self._write(actor, secret_id, value, expected_revision, False)

    @storage_errors
    def update(self, actor, secret_id, value, *, expected_revision):
        return self._write(actor, secret_id, value, expected_revision, True)

    @storage_errors
    def revoke(self, actor, secret_id, *, expected_revision):
        with transaction.atomic():
            row = m.Secret.objects.select_for_update().get(pk=secret_id)
            authorize(self.authorizer, actor, 'manage_project_secret', row)
            expected(row, expected_revision)
            row.revoked = True
            row.revision += 1
            row.save(update_fields=['revoked', 'revision'])
            audit(actor, 'revoke', row)
            return metadata(row)

    @storage_errors
    def bind(self, actor, secret_id, *, expected_revision, node_id=None, client_id=None,
             controller=False, role, digest, incarnation, grant_id, assignment_id):
        with transaction.atomic():
            secret = m.Secret.objects.select_for_update().get(pk=secret_id)
            expected(secret, expected_revision)
            available(secret)
            binding = m.SecretBinding(secret=secret, node_id=node_id, client_id=client_id,
                controller=controller, role=role, digest=digest, incarnation=incarnation,
                grant_id=grant_id, assignment_id=assignment_id)
            binding.clean()
            authorize(self.authorizer, actor, 'deliver_secret', secret, (binding,))
            current = m.SecretBinding.objects.filter(secret=secret, retired_at__isnull=True,
                node_id=node_id, client_id=client_id, controller=controller, role=role).first()
            if current:
                if any(getattr(current, f) != getattr(binding, f)
                       for f in ('digest', 'incarnation', 'grant_id', 'assignment_id')):
                    raise Conflict('current secret binding must be explicitly retired before replacement')
                # Потерянный ответ bind не создаёт дополнительного обязательного recipient.
                return binding_metadata(current)
            no_rotation(secret)
            binding.save()
            audit(actor, 'bind', secret)
            return binding_metadata(binding)

    @storage_errors
    def retire_binding(self, actor, binding_id, *, expected_revision):
        lookup = m.SecretBinding.objects.only('secret_id').get(pk=binding_id)
        with transaction.atomic():
            secret = m.Secret.objects.select_for_update().get(pk=lookup.secret_id)
            binding = m.SecretBinding.objects.get(pk=binding_id)
            expected(secret, expected_revision)
            authorize(self.authorizer, actor, 'deliver_secret', secret, (binding,))
            if binding.retired_at is None:
                no_rotation(secret)
                binding.retired_at = timezone.now()
                binding.save(update_fields=['retired_at'])
                audit(actor, 'retire_binding', secret)
            return binding_metadata(binding)

    @storage_errors
    def addressed_selection(self, actor, *, project_id, node_id, assignment_id, incarnation,
                            role, digest, grant_id, required_names, current_proof):
        """SQL-only подбор по exact trusted owner refs; не выдаёт plaintext.

        Root сначала проверяет Assignment/grant/approved policy публичными owner
        APIs. Только этот серверный allowlist определяет выбор; наличие Binding
        само по себе не создаёт authority и не расширяет список роли.
        """
        self.authorizer.authorize(actor, 'secrets.deliver_secret', project_ids=(project_id,),
            node_ids=(node_id,), client_ids=())
        if (type(required_names) is not tuple or not 1 <= len(required_names) <= 128
                or len(set(required_names)) != len(required_names) or type(current_proof) is not tuple):
            raise Refused('invalid approved addressed secret allowlist')
        names = Q(pk__in=())
        for kind, name in required_names:
            if kind not in ('variable', 'file'):
                raise Refused('addressed project secret kind required')
            m.validate_name(name, kind)
            names |= Q(secret__kind=kind, secret__name=name)
        bindings = tuple(m.SecretBinding.objects.filter(names, secret__project_id=project_id,
            secret__purpose='project', node_id=node_id, client_id__isnull=True, controller=False,
            assignment_id=assignment_id, incarnation=incarnation, role=role, digest=digest,
            grant_id=grant_id, retired_at__isnull=True).order_by('id').values_list('id', flat=True)[:129])
        if len(bindings) != len(required_names):
            raise Refused('required addressed project secret binding is missing')
        return DeliverySelection(bindings, required_names, current_proof)

    def _selection(self, actor):
        selection = self.delivery_gate.resolve(actor)
        if (type(selection) is not DeliverySelection or type(selection.binding_ids) is not tuple
                or type(selection.required_names) is not tuple or not selection.binding_ids
                or len(selection.binding_ids) != len(set(selection.binding_ids))):
            raise Refused('invalid addressed secret selection')
        bindings = tuple(m.SecretBinding.objects.filter(pk__in=selection.binding_ids, retired_at__isnull=True)
                         .select_related('secret__installation', 'secret__active_version').order_by('id'))
        if len(bindings) != len(selection.binding_ids):
            raise Refused('addressed secret binding is missing')
        names = tuple((b.secret.kind, b.secret.name) for b in bindings)
        if len(set(names)) != len(names) or set(names) != set(selection.required_names):
            raise Refused('required addressed secret set does not match pinned allowlist')
        # Один delivery не смешивает scopes/назначения даже при ошибке gate.
        identities = {(b.secret.project_id, b.node_id, b.client_id, b.controller,
                       b.role, b.digest, b.incarnation, b.grant_id, b.assignment_id) for b in bindings}
        if len(identities) != 1:
            raise Refused('addressed secret recipient binding mismatch')
        for binding in bindings:
            # Назначение не заменяет action/scope permission. _selection вызывается
            # до crypto и повторно под CAS locks после любого provider wait.
            authorize(self.authorizer, actor, 'deliver_secret', binding.secret, (binding,))
            available(binding.secret)
            if not binding.secret.active_version_id:
                raise Refused('addressed secret has no active version')
            if binding.secret.active_version.secret_id != binding.secret_id:
                raise Refused('addressed secret version mismatch')
        return selection, bindings

    @storage_errors
    def issue_addressed(self, actor):
        outside_transaction()
        selected, bindings = self._selection(actor)
        pins = tuple((b.id, b.secret.revision, b.secret.active_version_id) for b in bindings)
        result = []
        for binding in bindings:
            secret, version = binding.secret, binding.secret.active_version
            value = self._decrypt(secret, version)
            result.append(AddressedSecret(binding.id, version.id, secret.name, secret.kind, value))
        # После любого provider wait заново gate + current refs. Общая
        # короткая SQL-only секция под locks (#687); root/provider I/O уже
        # закончился и в неё не входит.
        readmission(self._after_crypto_selection, actor, selected, pins, bindings,
                    reason='addressed delivery')
        return tuple(result)

    def _after_crypto_selection(self, actor, selected, pins, bindings):
        with transaction.atomic():
            tuple(m.Secret.objects.select_for_update().filter(pk__in=[b.secret_id for b in bindings]).order_by('id'))
            current, current_bindings = self._selection(actor)
            current_pins = tuple((b.id, b.secret.revision, b.secret.active_version_id) for b in current_bindings)
            if current != selected or current_pins != pins:
                raise Conflict('addressed secret assignment or version changed')

    def _decrypt(self, secret, version):
        envelope = Envelope.from_bytes(bytes(version.envelope))
        if envelope.key_id != version.key_id or envelope.nonce != bytes(version.nonce):
            raise SecretCryptoError('secret envelope metadata mismatch')
        value = reveal(self.provider, envelope, context(secret, version))
        if len(value) != version.size:
            raise SecretCryptoError('secret envelope size mismatch')
        return value

    def _rotation_version(self, row, bindings, claim_id, generation):
        if (row.claim_id != claim_id or claim_id is None or row.generation != generation
                or row.state not in (Stage.DELIVERING.value, Stage.RESTORING_OLD.value)
                or row.pending not in (Effect.DELIVER.value, Effect.RESTORE_OLD.value)
                or row.secret_revision != row.secret.revision or row.secret.revoked):
            raise Conflict('rotation issuance requires current pending claim')
        required = set(m.RotationRecipient.objects.filter(rotation=row).values_list('binding_id', flat=True))
        if any(b.secret_id != row.secret_id or b.id not in required for b in bindings):
            raise Refused('rotation issuance recipient mismatch')
        version = row.new_version if row.pending == Effect.DELIVER.value else row.old_version
        if version.secret_id != row.secret_id:
            raise Refused('rotation issuance version mismatch')
        return version

    @storage_errors
    def issue_rotation(self, actor, rotation_id, *, claim_id, generation):
        outside_transaction()
        if type(generation) is not int or generation < 1:
            raise Conflict('invalid rotation issuance generation')
        selection, bindings = self._selection(actor)
        row = m.CredentialRotation.objects.select_related('secret', 'old_version', 'new_version').get(pk=rotation_id)
        version = self._rotation_version(row, bindings, claim_id, generation)
        pins = tuple((b.id, b.secret.revision, b.secret.active_version_id) for b in bindings)
        results = tuple(AddressedSecret(b.id, version.id, b.secret.name, b.secret.kind,
                        self._decrypt(b.secret, version)) for b in bindings)
        with transaction.atomic():
            secret = m.Secret.objects.select_for_update().get(pk=row.secret_id)
            current = m.CredentialRotation.objects.select_for_update().get(pk=rotation_id)
            current.secret = secret
            selected, bound = self._selection(actor)
            actual_version = self._rotation_version(current, bound, claim_id, generation)
            actual_pins = tuple((b.id, b.secret.revision, b.secret.active_version_id) for b in bound)
            if (selected != selection or pins != actual_pins or current.revision != row.revision
                    or actual_version.id != version.id):
                raise Conflict('rotation issuance binding changed after provider wait')
        return results


# Конкретный ORM lifecycle только оборачивает проверенную чистую политику.
from .rotation import Rotation, Stage, Effect, Outcome, Observation  # noqa: E402


@dataclass(frozen=True)
class RotationMetadata:
    id: uuid.UUID
    secret_id: uuid.UUID
    old_version_id: uuid.UUID
    new_version_id: uuid.UUID
    secret_revision: int
    claim_id: uuid.UUID | None
    generation: int
    policy: Rotation

    def __getattr__(self, name):
        if name in Rotation.__dataclass_fields__:
            return getattr(self.policy, name)
        raise AttributeError(name)


def _e(enum, value):
    return enum(value) if value is not None else None


class RotationService:
    """Persist intent -> внешний executor -> record/reconcile, без исполнения I/O.

    Claim не истекает по таймауту. Внешний executor обязан сохранить returned
    claim_id/generation и не исполнять begin до COMMIT. Readback ABSENT/PARTIAL
    означает доказанный покой старого executor, не результат сетевого timeout.
    Эти факты будущий #639/#648 адаптер подаёт через авторизованный порт.
    """
    def __init__(self, *, authorizer: Authorizer | None = None, delivery_gate: DeliveryGate | None = None,
                 current_verifier: CurrentRecipientVerifier | None = None):
        self.authorizer = authorizer if authorizer is not None else Deny()
        self.delivery_gate = delivery_gate if delivery_gate is not None else Deny()
        self.current_verifier = current_verifier if current_verifier is not None else Deny()

    def _current(self, actor, row, action, effect=None):
        bindings = self._bindings(row)
        if not bindings or any(b.retired_at is not None for b in bindings):
            raise Refused('rotation requires current recipient membership')
        secret = row.secret
        proof = RotationProof(row.id, row.revision, secret.id, secret.revision,
            secret.installation_id, secret.installation.identity, secret.project_id,
            secret.name, secret.kind, secret.purpose,
            version_metadata(row.old_version), version_metadata(row.new_version),
            secret.active_version_id, tuple(binding_metadata(b) for b in bindings),
            action, effect.value if effect is not None else row.pending, row.claim_id, row.generation)
        if self.current_verifier.verify_current(actor, proof) is not True:
            raise Refused('rotation has no current addressed recipient proof')

    def _bindings(self, row):
        return tuple(m.SecretBinding.objects.filter(rotationrecipient__rotation=row).order_by('id'))

    def _receipts(self, row, *, generation=None, version_id=None):
        return frozenset(str(x) for x in m.DeliveryReceipt.objects.filter(
            recipient__rotation=row, generation=row.delivery_generation if generation is None else generation,
            version_id=version_id or row.new_version_id, outcome=Outcome.APPLIED.value
        ).values_list('recipient__binding_id', flat=True))

    def _policy(self, row):
        from .rotation import FailureReason
        # Незавершённая доставка может уже иметь отдельные receipts. Они ещё
        # не подтверждают весь effect; explicit record/reconcile вводит их в policy.
        unconfirmed = row.pending == Effect.DELIVER.value or row.retryable == Effect.DELIVER.value or row.state == Stage.STAGED.value
        receipts = frozenset() if unconfirmed else self._receipts(row)
        return Rotation(required=frozenset(str(b.id) for b in self._bindings(row)),
            state=Stage(row.state), revision=row.revision, receipts=receipts,
            pending=_e(Effect, row.pending), retryable=_e(Effect, row.retryable),
            original_failure=_e(Effect, row.original_failure), compensation_failure=_e(Effect, row.compensation_failure),
            original_reason=_e(FailureReason, row.original_reason), compensation_reason=_e(FailureReason, row.compensation_reason))

    def _metadata(self, row):
        return RotationMetadata(row.id, row.secret_id, row.old_version_id, row.new_version_id,
                                row.secret_revision, row.claim_id, row.generation, self._policy(row))

    def _authorize(self, actor, row, action='rotate_proxy_key'):
        authorize(self.authorizer, actor, action, row.secret, self._bindings(row))

    def _locked(self, actor, rotation_id, expected_revision=None):
        lookup = m.CredentialRotation.objects.only('secret_id').get(pk=rotation_id)
        secret = m.Secret.objects.select_for_update().get(pk=lookup.secret_id)
        row = m.CredentialRotation.objects.select_for_update().get(pk=rotation_id)
        row.secret = secret
        self._authorize(actor, row)
        if expected_revision is not None:
            expected(row, expected_revision)
        if secret.revision != row.secret_revision or secret.revoked:
            raise Conflict('rotation secret decision changed')
        if secret.active_version_id not in (row.old_version_id, row.new_version_id):
            raise Conflict('rotation active version changed')
        return row

    @storage_errors
    def metadata(self, actor, rotation_id):
        row = m.CredentialRotation.objects.select_related('secret').get(pk=rotation_id)
        self._authorize(actor, row, 'view_secret_metadata')
        return self._metadata(row)

    @storage_errors
    def stage(self, actor, secret_id, new_version_id, binding_ids, *, expected_revision):
        if not binding_ids or len(set(binding_ids)) != len(binding_ids):
            raise Refused('rotation requires exact addressed recipients')
        failed = False
        try:
            with transaction.atomic():
                secret = m.Secret.objects.select_for_update().get(pk=secret_id)
                expected(secret, expected_revision)
                available(secret)
                no_rotation(secret)
                new = m.SecretVersion.objects.get(pk=new_version_id)
                if (new.secret_id != secret.id or new.id == secret.active_version_id
                        or new.number != secret.last_number):
                    raise Refused('rotation requires latest staged version of its secret')
                bindings = tuple(m.SecretBinding.objects.filter(secret=secret, retired_at__isnull=True).order_by('id'))
                # Все текущие получатели, включая offline; retired остаются evidence.
                # Перед каждым новым effect проверяется полное текущее назначение.
                if set(binding_ids) != {b.id for b in bindings}:
                    raise Refused('rotation recipient set is incomplete or cross-secret')
                authorize(self.authorizer, actor, 'rotate_proxy_key', secret, bindings)
                row = m.CredentialRotation.objects.create(secret=secret,
                    old_version_id=secret.active_version_id, new_version=new,
                    secret_revision=secret.revision, state=Stage.STAGED.value)
                for binding in bindings:
                    m.RotationRecipient.objects.create(rotation=row, binding=binding)
                audit(actor, 'rotation_stage', secret, row)
                return self._metadata(row)
        except IntegrityError:
            failed = True
        if failed:
            raise Conflict('secret already has an unresolved rotation or invalid reference')

    def _save(self, actor, row, policy, action):
        if policy.state in (Stage.ACTIVE, Stage.OLD_RESTORED):
            # Старые receipts не переживают смену назначения как полномочие.
            # После wait разрешено записать UNKNOWN, но не активировать версию.
            self._current(actor, row, action)
        for name in ('revision', 'state', 'pending', 'retryable', 'original_failure',
                     'compensation_failure', 'original_reason', 'compensation_reason'):
            value = getattr(policy, name)
            setattr(row, name, value.value if hasattr(value, 'value') else value)
        row.resolved = policy.state in (Stage.RETIRED, Stage.COMPENSATED)
        if policy.state in (Stage.ACTIVE, Stage.OLD_RESTORED):
            version_id = row.new_version_id if policy.state is Stage.ACTIVE else row.old_version_id
            if row.secret.active_version_id != version_id:
                row.secret.active_version_id = version_id
                row.secret.revision += 1
                row.secret.save(update_fields=['active_version', 'revision'])
                row.secret_revision = row.secret.revision
        row.save()
        audit(actor, action, row.secret, row)
        return self._metadata(row)

    @storage_errors
    def begin(self, actor, rotation_id, effect, *, expected_revision):
        if type(effect) is not Effect:
            raise ValueError('invalid rotation effect')
        with transaction.atomic():
            row = self._locked(actor, rotation_id, expected_revision)
            policy = self._policy(row)
            if policy.retryable is effect:
                next_policy = policy.retry_effect(expected_revision=expected_revision)
            else:
                method = {Effect.DELIVER: policy.begin_delivery, Effect.RETIRE_OLD: policy.begin_retirement,
                          Effect.RESTORE_OLD: policy.begin_compensation, Effect.REVOKE_NEW: policy.begin_new_revocation}[effect]
                next_policy = method(expected_revision=expected_revision)
            row.claim_id = uuid.uuid4()
            row.generation += 1
            if effect is Effect.DELIVER:
                row.delivery_generation = row.generation
            self._current(actor, row, 'begin', effect)
            return self._save(actor, row, next_policy, 'rotation_begin')

    def _claim(self, row, claim_id):
        if row.pending is None or claim_id is None or row.claim_id != claim_id:
            raise Conflict('rotation claim changed or has no pending intent')

    @storage_errors
    def verify_effect(self, actor, rotation_id, *, claim_id, generation, expected_revision):
        """Перепроверить pending dispatch после wait, не выдать долгоживущий permit.

        Executor вызывает непосредственно перед внешним effect и заново после
        каждого await. Сам вызов не повторяет unknown effect и не меняет intent.
        """
        if type(generation) is not int or generation < 1:
            raise Conflict('invalid rotation execution generation')
        with transaction.atomic():
            row = self._locked(actor, rotation_id, expected_revision)
            self._claim(row, claim_id)
            effect = Effect(row.pending)
            state = {Effect.DELIVER: Stage.DELIVERING, Effect.RETIRE_OLD: Stage.RETIRING,
                     Effect.RESTORE_OLD: Stage.RESTORING_OLD, Effect.REVOKE_NEW: Stage.REVOKING_NEW}[effect]
            if row.generation != generation or row.state != state.value or row.retryable is not None:
                raise Conflict('rotation execution requires a current confirmed pending intent')
            self._current(actor, row, 'execute', effect)
            return self._metadata(row)

    @storage_errors
    def receipt(self, actor, rotation_id, binding_id, *, version_id, claim_id, generation, outcome):
        if type(outcome) is not Outcome or type(generation) is not int or generation < 1:
            raise ValueError('invalid rotation receipt')
        if outcome is Outcome.UNKNOWN:
            # Unknown — durable checkpoint record(UNKNOWN), а не immutable
            # квитанция, которая запретит последующий подтверждённый readback.
            raise Refused('unconfirmed delivery requires rotation reconciliation')
        with transaction.atomic():
            row = self._locked(actor, rotation_id)
            self._claim(row, claim_id)
            if generation != row.generation:
                raise Conflict('rotation receipt generation changed')
            if row.pending not in (Effect.DELIVER.value, Effect.RESTORE_OLD.value):
                raise Refused('rotation effect does not accept delivery receipts')
            expected_version = row.new_version_id if row.pending == Effect.DELIVER.value else row.old_version_id
            recipient = m.RotationRecipient.objects.filter(rotation=row, binding_id=binding_id).first()
            if recipient is None or version_id != expected_version:
                raise Refused('rotation receipt recipient or version mismatch')
            authorize(self.authorizer, actor, 'deliver_secret', row.secret, (recipient.binding,))
            self._current(actor, row, 'receipt')
            selected = self.delivery_gate.resolve(actor)
            if (type(selected) is not DeliverySelection or recipient.binding_id not in selected.binding_ids
                    or (row.secret.kind, row.secret.name) not in selected.required_names):
                raise Refused('rotation receipt has no current addressed proof')
            receipt = m.DeliveryReceipt.objects.filter(recipient=recipient, generation=generation).first()
            if receipt:
                if (receipt.claim_id, receipt.version_id, receipt.outcome) != (claim_id, version_id, outcome.value):
                    raise Refused('rotation receipt identity has conflicting outcome')
            else:
                receipt = m.DeliveryReceipt.objects.create(recipient=recipient, version_id=version_id,
                    claim_id=claim_id, generation=generation, outcome=outcome.value)
                audit(actor, 'receipt', row.secret, row)
            return receipt.id

    @storage_errors
    def record(self, actor, rotation_id, outcome, *, claim_id, expected_revision, reason=None):
        with transaction.atomic():
            row = self._locked(actor, rotation_id, expected_revision)
            self._claim(row, claim_id)
            policy = self._policy(row)
            receipts = self._receipts(row) if outcome is Outcome.APPLIED and policy.pending is Effect.DELIVER else frozenset()
            if (outcome is Outcome.APPLIED and policy.pending is Effect.RESTORE_OLD
                    and self._receipts(row, generation=row.generation, version_id=row.old_version_id) != policy.required):
                raise Refused('old credential restoration requires addressed receipts')
            next_policy = policy.record(expected_revision=expected_revision, outcome=outcome, receipts=receipts, reason=reason)
            return self._save(actor, row, next_policy, 'rotation_record')

    @storage_errors
    def reconcile(self, actor, rotation_id, outcome, *, claim_id, expected_revision, quiescent=False):
        with transaction.atomic():
            row = self._locked(actor, rotation_id, expected_revision)
            self._claim(row, claim_id)
            policy = self._policy(row)
            receipts = self._receipts(row) if policy.pending is Effect.DELIVER and outcome in (Observation.APPLIED, Observation.PARTIAL) else frozenset()
            if outcome is Observation.ABSENT:
                evidence = (self._receipts(row) if policy.pending is Effect.DELIVER
                    else self._receipts(row, generation=row.generation, version_id=row.old_version_id)
                    if policy.pending is Effect.RESTORE_OLD else frozenset())
                if evidence:
                    raise Refused('absence contradicts confirmed addressed delivery; reconcile partial effect')
            if (outcome is Observation.APPLIED and policy.pending is Effect.RESTORE_OLD
                    and self._receipts(row, generation=row.generation, version_id=row.old_version_id) != policy.required):
                raise Refused('old credential restoration requires addressed receipts')
            next_policy = policy.reconcile(expected_revision=expected_revision, outcome=outcome,
                                           receipts=receipts, quiescent=quiescent)
            return self._save(actor, row, next_policy, 'rotation_reconcile')

    @storage_errors
    def activate(self, actor, rotation_id, *, expected_revision):
        with transaction.atomic():
            row = self._locked(actor, rotation_id, expected_revision)
            policy = self._policy(row).activate(expected_revision=expected_revision)
            return self._save(actor, row, policy, 'rotation_activate')
