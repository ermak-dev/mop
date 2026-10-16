"""ORM use cases делегирования. Никаких registry/file fallback или сетевых effects.

Approval/Bindings — только локальные авторитетные queries будущих #636/#639,
связанные composition root. Они не принимают workspace/owner/force из JSON.
Provider I/O находится до/после коротких SQL sections, никогда внутри них.
"""
from datetime import timedelta
from functools import wraps
import json
from typing import Protocol
import uuid

from django.contrib.auth import get_user_model
from django.db import DatabaseError, models, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables
from rest_framework.authtoken.models import Token

from mop.common import soul
from ..catalog.models import Project
from ..secrets.services import RetainedSecrets, RetentionScope, outside_transaction
from ..secrets.crypto import SecretCryptoError
from . import models as m, services as auth
from .context import ActorContext, PrincipalKind
from .delegation import (ApprovedPolicy, RoleBinding, BundleContent, GrantInfo,
                         MessageAuthority, PolicyBinding, RetirementReceipt, allows, allows_spawn, uuid_value)


class Conflict(ValueError):
    """Изменившееся намерение/revision, не разрешение на повторный внешний effect."""


class StorageUnavailable(ValueError):
    """Raw ошибки драйвера могут содержать credential в SQL parameters."""


def storage_boundary(command):
    @wraps(command)
    def run(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except DatabaseError:
            pass
        raise StorageUnavailable('delegated access storage is unavailable')
    return run


class ApprovalQuery(Protocol):
    """Локальный proof exact bytes; validate сверяет сохранённое intent, не parent latest."""
    def pin(self, binding: PolicyBinding) -> ApprovedPolicy: ...
    def validate(self, proof: ApprovedPolicy, binding: PolicyBinding) -> None: ...


class BindingQuery(Protocol):
    def current(self, project_id, target_id) -> RoleBinding | None: ...
    def activation(self, grant_id) -> RoleBinding | None: ...
    def retirement(self, grant_id, revision) -> RetirementReceipt: ...


class MessageDelivery(Protocol):
    def send(self, authority: MessageAuthority, message: bytes): ...
    def dispatch(self, authority: MessageAuthority, message: bytes): ...


class Unbound:
    def pin(self, binding):
        raise auth.Denied('approved policy query is not bound')

    def validate(self, proof, binding):
        raise auth.Denied('approved policy query is not bound')

    def current(self, project_id, target_id):
        raise auth.Denied('runtime binding query is not bound')

    def activation(self, grant_id):
        raise auth.Denied('activation receipt query is not bound')

    def retirement(self, grant_id, revision):
        raise auth.Denied('transport retirement query is not bound')


class _RetentionAuthority:
    """Root provisioner: одно назначение из ORM, не права human secret manager."""
    def __init__(self, grants):
        self.grants = grants

    def check(self, actor, scope):
        api = self.grants
        if scope.purpose == 'policy':
            owner = api._owner(actor)
            if owner != scope.owner_id:
                raise auth.Denied('bundle owner mismatch')
            row = m.RoleBundle.objects.filter(pk=scope.subject_id).first()
            if row is None:
                api._human(actor, 'access.approve_role', scope.project_id)
            elif (row.owner_id, row.project_id) != (owner, scope.project_id):
                raise auth.Denied('bundle scope mismatch')
            else:
                api._owner_permission(owner, 'access.view_role', scope.project_id)
        elif scope.purpose == 'grant':
            row = api._row(scope.subject_id)
            if (row.intent.owner_id, row.intent.project_id) != (scope.owner_id, scope.project_id):
                raise auth.Denied('credential provisioning target mismatch')
            api._creation_actor(actor, row)
            if row.state != 'reserved' or row.intent.expires_at <= timezone.now():
                raise auth.Denied('credential provisioning is no longer reserved')
        else:
            raise auth.Denied('unsupported retained access value')


class Grants:
    def __init__(self, *, provider, approvals: ApprovalQuery | None = None,
                 bindings: BindingQuery | None = None, messages: MessageDelivery | None = None):
        self.approvals = approvals if approvals is not None else Unbound()
        self.bindings = bindings if bindings is not None else Unbound()
        self.messages = messages
        self.secrets = RetainedSecrets(provider=provider, authority=_RetentionAuthority(self))

    def _row(self, grant_id):
        row = m.Grant.objects.select_related('intent__bundle', 'principal__user').filter(pk=grant_id).first()
        if row is None:
            raise auth.NotVisible('grant not found')
        return row

    def _owner_permission(self, owner_id, action, project_id):
        auth.Access().authorize_owner(owner_id, action, project_ids=(project_id,))

    def _human(self, actor, action, project_id):
        if type(actor) is not ActorContext or actor.kind is not PrincipalKind.HUMAN:
            raise auth.Denied('authenticated human required')
        auth.Access().authorize(actor, action, project_ids=(project_id,))
        return actor.user_id

    def _owner(self, actor):
        if type(actor) is not ActorContext:
            raise auth.Denied('trusted actor context required')
        if actor.kind is PrincipalKind.HUMAN:
            auth.current_actor(actor)
            return actor.user_id
        if actor.kind is PrincipalKind.GRANT:
            row = self._credential(actor)
            self._check_row(row)
            info = self._info(row)
            if self.bindings.current(info.project_id, info.target_id) != info.binding:
                raise auth.Denied('grant runtime binding changed')
            self._check_ancestors_local(row)
            return row.intent.owner_id
        raise auth.Denied('node identity is not a delegated grant')

    def _policy(self, row):
        intent = row.intent
        return ApprovedPolicy(intent.policy_approval_id, intent.policy_version_id,
                              intent.policy_digest, intent.owner_id, intent.project_id)

    def _policy_binding(self, intent):
        return PolicyBinding(intent.id, intent.owner_id, intent.actor_id, intent.project_id,
            intent.bundle_id, intent.bundle.role, intent.bundle.digest, intent.parent_id,
            intent.target_id, intent.incarnation)

    def _validate_policy(self, proof, binding):
        if self.approvals.validate(proof, binding) is not None:
            raise auth.Denied('approval validation must not return caller booleans')
        self.secrets.validate_policy_reference(proof.version_id, project_id=binding.project_id)

    def _info(self, row):
        intent, bundle = row.intent, row.intent.bundle
        return GrantInfo(row.id, intent.owner_id, row.principal_id, intent.project_id,
            intent.target_id, intent.incarnation, bundle.id, bundle.role, bundle.digest,
            intent.parent_id, row.state, row.revision, row.credential_version_id,
            self._policy(row), row.retirement)

    def _bundle_row(self, actor, bundle_id):
        row = m.RoleBundle.objects.filter(pk=bundle_id).first()
        if row is None or row.owner_id != self._owner(actor):
            raise auth.NotVisible('role bundle not found')
        self._owner_permission(row.owner_id, 'access.view_role', row.project_id)
        return row

    def _normalized(self, row, parsed):
        if (row.role, row.driver, row.receives_dispatch, row.dispatch, row.prompt_name) != (
                parsed.soul, parsed.runtime.driver, parsed.runtime.receives_dispatch,
                parsed.capabilities.dispatch, parsed.prompt):
            raise auth.Denied('role bundle normalized identity mismatch')
        actual = set(m.RoleCapability.objects.filter(bundle=row).values_list('scope', 'verb'))
        wanted = {('node', v) for v in parsed.capabilities.node_verbs} | {
                  ('cluster', v) for v in parsed.capabilities.cluster_verbs}
        if actual != wanted or set(m.RoleSpawn.objects.filter(bundle=row).values_list('role', flat=True)) != set(parsed.capabilities.spawn):
            raise auth.Denied('role bundle normalized capabilities mismatch')
        names = set(m.RoleSecretName.objects.filter(bundle=row).values_list('kind', 'name'))
        if names != {('files', v) for v in parsed.secrets.files} | {('vars', v) for v in parsed.secrets.vars}:
            raise auth.Denied('role bundle secret allowlist mismatch')

    @storage_boundary
    def read_bundle(self, actor, bundle_id):
        # Чтение для клиента не выдаёт промпты других ролей его human owner.
        # Внутренняя проверка ancestor/spawn читает bytes, но наружу их не отдаёт.
        if type(actor) is ActorContext and actor.kind is PrincipalKind.GRANT:
            if self._credential(actor).intent.bundle_id != bundle_id:
                raise auth.Denied('grant may read only its pinned role bundle')
        return self._read_bundle(actor, bundle_id)

    @storage_boundary
    def _read_bundle(self, actor, bundle_id):
        outside_transaction()
        row = self._bundle_row(actor, bundle_id)
        try:
            raw = self.secrets.read(actor, RetentionScope(row.project_id, row.owner_id, row.id, 'policy'), row.content_version_id)
        except SecretCryptoError:
            raise auth.Denied('retained role bundle authentication failed') from None
        try:
            manifest, prompt, provenance = json.loads(raw)
            parsed = soul.parse(manifest)
            bundle = soul.from_manifest(parsed.to_dict(), prompt.encode('utf-8'))
        except (ValueError, TypeError, UnicodeError):
            raise auth.Denied('invalid retained role bundle') from None
        if bundle.digest != row.digest or bundle.role != row.role:
            raise auth.Denied('role bundle digest mismatch')
        self._normalized(row, parsed)
        return BundleContent(row.id, row.role, row.digest, parsed.capabilities, parsed.runtime,
                             parsed.secrets, manifest.encode('utf-8'), bundle.prompt, provenance)

    @storage_boundary
    @sensitive_variables('manifest', 'prompt', 'raw')
    def publish_bundle(self, actor, *, project_id, manifest, prompt, provenance):
        outside_transaction()
        owner = self._human(actor, 'access.approve_role', project_id)
        if (type(manifest) is not bytes or type(prompt) is not bytes or not isinstance(provenance, str)
                or not provenance or len(provenance) > 4096):
            raise ValueError('exact bundle bytes and bounded provenance required')
        try:
            parsed = soul.parse(manifest.decode('utf-8'))
            bundle = soul.from_manifest(parsed.to_dict(), prompt)
        except (ValueError, UnicodeError):
            raise ValueError('invalid role bundle bytes') from None
        existing = m.RoleBundle.objects.filter(owner_id=owner, project_id=project_id, digest=bundle.digest).first()
        if existing:
            self._read_bundle(actor, existing.id)
            return self._bundle_metadata(existing)
        bundle_id = uuid.uuid4()
        raw = json.dumps([manifest.decode('utf-8'), prompt.decode('utf-8'), provenance], ensure_ascii=False).encode()
        prepared = self.secrets.prepare(actor, RetentionScope(project_id, owner, bundle_id, 'policy'), raw)
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project_id)
            self._human(actor, 'access.approve_role', project_id)
            existing = m.RoleBundle.objects.filter(owner_id=owner, project_id=project_id, digest=bundle.digest).first()
            if existing:
                return self._bundle_metadata(existing)
            version_id = self.secrets.commit(actor, prepared)
            row = m.RoleBundle.objects.create(id=bundle_id, owner_id=owner, project_id=project_id,
                role=bundle.role, digest=bundle.digest, content_version_id=version_id,
                driver=parsed.runtime.driver, receives_dispatch=parsed.runtime.receives_dispatch,
                dispatch=parsed.capabilities.dispatch, prompt_name=parsed.prompt)
            for scope, verbs in (('node', parsed.capabilities.node_verbs), ('cluster', parsed.capabilities.cluster_verbs)):
                for verb in verbs:
                    m.RoleCapability.objects.create(bundle=row, scope=scope, verb=verb)
            for role in parsed.capabilities.spawn:
                m.RoleSpawn.objects.create(bundle=row, role=role)
            for kind, names in (('files', parsed.secrets.files), ('vars', parsed.secrets.vars)):
                for name in names:
                    m.RoleSecretName.objects.create(bundle=row, kind=kind, name=name)
            return self._bundle_metadata(row)

    def _bundle_metadata(self, row):
        from .delegation import BundleMetadata
        return BundleMetadata(row.id, row.role, row.digest, row.project_id, row.owner_id)

    def approve_bundle(self, actor, bundle_id):
        value = self._read_bundle(actor, bundle_id)
        row = self._bundle_row(actor, value.id)
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=row.project_id)
            owner = self._human(actor, 'access.approve_role', row.project_id)
            if row.owner_id != owner:
                raise auth.Denied('only the human bundle owner may approve')
            self._normalized(row, soul.parse(value.manifest.decode()))
            approval, _ = m.RoleBundleApproval.objects.get_or_create(bundle=row, defaults={'human_id': owner})
            return approval.id

    def _credential(self, actor):
        if type(actor) is not ActorContext or actor.kind is not PrincipalKind.GRANT or actor.grant_id is None:
            raise auth.Denied('authenticated grant context required')
        row = self._row(actor.grant_id)
        principal, user = row.principal, row.principal.user
        if (principal.kind != 'grant' or principal.bound_node_id is not None or not user.is_active
                or user.is_staff or user.is_superuser or user.has_usable_password()):
            raise auth.Denied('invalid grant service identity')
        binding = m.CredentialBinding.objects.filter(pk=actor.credential_id, token_user=user).first()
        token = Token.objects.filter(user=user).first()
        if binding is None or token is None:
            raise auth.Denied('grant credential revoked')
        auth._check_binding(binding, principal, None)
        wanted = ActorContext(user.id, principal.id, PrincipalKind.GRANT, binding.id,
                              row.intent.owner_id, None, row.id)
        if actor != wanted or not constant_time_compare(binding.token_digest, auth._digest(token.key)):
            raise auth.Denied('grant credential binding changed')
        return row

    def _check_row(self, row, *, state='active'):
        if row.state != state or row.intent.expires_at <= timezone.now():
            raise auth.Denied('grant is not currently active')
        self._owner_permission(row.intent.owner_id, 'access.view_role', row.intent.project_id)
        if not m.RoleBundleApproval.objects.filter(bundle_id=row.intent.bundle_id, human_id=row.intent.owner_id).exists():
            raise auth.Denied('role bundle is not human approved')
        self._validate_policy(self._policy(row), self._policy_binding(row.intent))
        if row.credential_version_id:
            self.secrets.check_reference(RetentionScope(row.intent.project_id, row.intent.owner_id, row.id, 'grant'), row.credential_version_id)

    def _chain(self, actor):
        outside_transaction()
        row = self._credential(actor)
        chain, seen = [], set()
        while True:
            if row.id in seen or len(seen) >= 32:
                raise auth.Denied('invalid delegation ancestry')
            seen.add(row.id)
            self._check_row(row)
            info = self._info(row)
            actual = self.bindings.current(info.project_id, info.target_id)
            if type(actual) is not RoleBinding or actual != info.binding:
                raise auth.Denied('grant runtime binding changed')
            content = self._read_bundle(actor, info.bundle_id)
            chain.append((info, content))
            if info.parent_id is None:
                break
            parent = self._row(info.parent_id)
            if (parent.intent.owner_id, parent.intent.project_id) != (info.owner_id, info.project_id):
                raise auth.Denied('delegation ancestor scope mismatch')
            if self._policy(parent) != info.policy:
                raise auth.Denied('delegation approved policy mismatch')
            # Отзыв/ротация credential предка тоже немедленно закрывает детей.
            principal = parent.principal
            binding = m.CredentialBinding.objects.filter(token_user_id=principal.user_id).first()
            if binding is None:
                raise auth.Denied('ancestor credential revoked')
            parent_actor = ActorContext(principal.user_id, principal.id, PrincipalKind.GRANT,
                binding.id, parent.intent.owner_id, None, parent.id)
            self._credential(parent_actor)
            row = parent
        return tuple(chain)

    def _recheck(self, actor, chain):
        self._credential(actor)
        for info, content in chain:
            row = self._row(info.id)
            self._check_row(row)
            if self._info(row) != info:
                raise Conflict('delegation changed during preparation')
            if self.bindings.current(info.project_id, info.target_id) != info.binding:
                raise auth.Denied('grant runtime binding changed')
            self._normalized(row.intent.bundle, soul.parse(content.manifest.decode()))

    @storage_boundary
    def resolve_actor(self, actor):
        self._chain(actor)
        return actor

    @storage_boundary
    @sensitive_variables('key')
    def token_context(self, key):
        outside_transaction()
        token = Token.objects.select_related('user__principal').filter(key=key).first()
        if token is None:
            raise auth.Denied('token revoked or rotated')
        principal = m.Principal.objects.filter(user=token.user, kind='grant').first()
        if principal is None:
            raise auth.Denied('grant principal required')
        row = m.Grant.objects.filter(principal=principal).select_related('intent').first()
        binding = m.CredentialBinding.objects.filter(token_user=token.user).first()
        if row is None or binding is None:
            raise auth.Denied('grant credential is not bound')
        actor = ActorContext(token.user_id, principal.id, PrincipalKind.GRANT, binding.id,
                             row.intent.owner_id, None, row.id)
        self.resolve_actor(actor)
        return actor

    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()):
        chain = self._chain(actor)
        source = chain[0][0]
        if set(project_ids) != {source.project_id} or node_ids or client_ids:
            raise auth.Denied('grant action scope mismatch')
        self._owner_permission(source.owner_id, action, source.project_id)
        if not all(allows(content, action) for info, content in chain):
            raise auth.Denied('action is outside pinned delegated capabilities')

    def _creation_actor(self, actor, row):
        intent = row.intent
        if self._owner(actor) != intent.owner_id or actor.principal_id != intent.actor_id:
            raise auth.Denied('creation actor or owner mismatch')
        self._owner_permission(intent.owner_id, 'sessions.create_puppet', intent.project_id)
        if actor.kind is PrincipalKind.HUMAN:
            self._human(actor, 'access.issue_grant', intent.project_id)
        else:
            parent = self._credential(actor)
            if parent.id != intent.parent_id or parent.state != 'active' or parent.intent.expires_at <= timezone.now():
                raise auth.Denied('creation ancestor is no longer active')

    def _creation_chain(self, actor, row):
        self._creation_actor(actor, row)
        chain = self._chain(actor) if actor.kind is PrincipalKind.GRANT else ()
        if chain:
            child = self._read_bundle(actor, row.intent.bundle_id)
            for info, content in chain:
                if not allows_spawn(content, child):
                    raise auth.Denied('spawn role or secrets are outside approved allowlist')
        return chain

    @storage_boundary
    def reserve(self, actor, *, project_id, bundle_id, target_id, incarnation, request_id, expires_at):
        outside_transaction()
        for value in (project_id, bundle_id, target_id, incarnation, request_id):
            uuid_value(value)
        now = timezone.now()
        if not hasattr(expires_at, 'tzinfo') or timezone.is_naive(expires_at) or not now < expires_at <= now + timedelta(days=30):
            raise ValueError('bounded future grant expiry required')
        owner = self._owner(actor)
        content = self._read_bundle(actor, bundle_id)
        bundle = self._bundle_row(actor, bundle_id)
        if bundle.project_id != project_id or not m.RoleBundleApproval.objects.filter(bundle=bundle, human_id=owner).exists():
            raise auth.Denied('creation requires this project and approved bundle')
        chain = self._chain(actor) if actor.kind is PrincipalKind.GRANT else ()
        parent_id = chain[0][0].id if chain else None
        if chain:
            for info, pinned in chain:
                if info.project_id != project_id or not allows_spawn(pinned, content):
                    raise auth.Denied('spawn role or secrets are outside approved allowlist')
        else:
            self._human(actor, 'access.issue_grant', project_id)
        self._owner_permission(owner, 'sessions.create_puppet', project_id)
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=project_id)
            get_user_model().objects.select_for_update().get(pk=owner)
            if chain:
                self._recheck(actor, chain)
            else:
                self._human(actor, 'access.issue_grant', project_id)
            self._owner_permission(owner, 'sessions.create_puppet', project_id)
            existing = m.CreationIntent.objects.filter(owner_id=owner, actor_id=actor.principal_id,
                project_id=project_id, request_id=request_id).first()
            if existing:
                if (existing.bundle_id, existing.target_id, existing.incarnation, existing.expires_at, existing.parent_id) != (
                        bundle_id, target_id, incarnation, expires_at, parent_id):
                    raise Conflict('creation request already names a different intent')
                row = m.Grant.objects.select_related('intent__bundle').get(intent=existing)
                self._validate_policy(self._policy(row), self._policy_binding(existing))
                return self._info(row)
            intent_id = uuid.uuid4()
            policy_binding = PolicyBinding(intent_id, owner, actor.principal_id, project_id, bundle_id,
                content.role, content.digest, parent_id, target_id, incarnation)
            proof = chain[0][0].policy if chain else self.approvals.pin(policy_binding)
            if type(proof) is not ApprovedPolicy or (proof.owner_id, proof.project_id) != (owner, project_id):
                raise auth.Denied('trusted exact approved policy proof required')
            self._validate_policy(proof, policy_binding)
            self._normalized(bundle, soul.parse(content.manifest.decode()))
            intent = m.CreationIntent.objects.create(id=intent_id, owner_id=owner, actor_id=actor.principal_id,
                project_id=project_id, bundle=bundle, parent_id=parent_id, request_id=request_id,
                target_id=target_id, incarnation=incarnation, expires_at=expires_at,
                policy_approval_id=proof.approval_id, policy_version_id=proof.version_id, policy_digest=proof.digest)
            grant_id = uuid.uuid4()
            user = get_user_model().objects.create_user('grant-' + grant_id.hex, password=None, is_staff=False, is_superuser=False)
            principal = m.Principal.objects.create(user=user, kind='grant')
            row = m.Grant.objects.create(id=grant_id, intent=intent, principal=principal)
            self._event(row, actor, 'reserve')
            return self._info(row)

    def reservation(self, actor, *, project_id, request_id):
        owner = self._owner(actor)
        if actor.kind is PrincipalKind.GRANT:
            self.resolve_actor(actor)
        else:
            self._human(actor, 'access.issue_grant', project_id)
        intent = m.CreationIntent.objects.filter(owner_id=owner, actor_id=actor.principal_id,
            project_id=project_id, request_id=request_id).first()
        if intent is None:
            raise auth.NotVisible('creation request not found')
        return self.get(actor, m.Grant.objects.get(intent=intent).id)

    @storage_boundary
    def get(self, actor, grant_id):
        row = self._row(grant_id)
        if self._owner(actor) != row.intent.owner_id:
            raise auth.NotVisible('grant not found')
        if actor.kind is PrincipalKind.GRANT:
            self.resolve_actor(actor)
            if actor.grant_id not in (row.id, row.intent.parent_id):
                raise auth.NotVisible('grant not found')
        self._owner_permission(row.intent.owner_id, 'access.view_role', row.intent.project_id)
        self._read_bundle(actor, row.intent.bundle_id)
        return self._info(row)

    def _event(self, row, actor, action):
        m.GrantEvent.objects.create(grant=row, actor_id=actor.principal_id if actor else None,
                                    action=action, revision=row.revision)

    def _expected(self, row, revision):
        if type(revision) is not int or row.revision != revision:
            raise Conflict('grant revision changed')

    def _cas(self, row, **changes):
        # Единственный writer lifecycle; public QuerySet/save/bulk закрыты.
        matched = models.QuerySet.update(m.Grant.objects.filter(pk=row.id, revision=row.revision),
                                          revision=row.revision + 1, **changes)
        if matched != 1:
            raise Conflict('grant decision changed')
        row.revision += 1
        for name, value in changes.items():
            setattr(row, name, value)

    @storage_boundary
    @sensitive_variables('key', 'prepared')
    def provision(self, actor, grant_id, *, expected_revision):
        outside_transaction()
        row = self._row(grant_id)
        self._expected(row, expected_revision)
        chain = self._creation_chain(actor, row)
        self._check_row(row, state='reserved')
        if row.credential_version_id:
            return self._info(row)
        key = Token().generate_key()
        prepared = self.secrets.prepare(actor, RetentionScope(row.intent.project_id, row.intent.owner_id, row.id, 'grant'), key.encode())
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=row.intent.project_id)
            row = self._row(grant_id)
            self._expected(row, expected_revision)
            self._creation_actor(actor, row)
            if chain:
                self._recheck(actor, chain)
            self._check_row(row, state='reserved')
            version_id = self.secrets.commit(actor, prepared)
            token = Token.objects.create(key=key, user_id=row.principal.user_id)
            m.CredentialBinding.objects.create(token_user_id=row.principal.user_id,
                **auth._binding_values(row.principal, None), token_digest=auth._digest(token.key))
            self._cas(row, credential_version_id=version_id)
            self._event(row, actor, 'provision')
            return self._info(row)

    def _intent_actor(self, row):
        principal = m.Principal.objects.get(pk=row.intent.actor_id)
        binding = m.CredentialBinding.objects.filter(principal=principal).order_by('id').first()
        if binding is None:
            raise auth.Denied('creation credential is no longer available')
        if principal.kind == 'human':
            actor = auth._context(binding, principal)
            auth.current_actor(actor)
            return actor
        if principal.kind == 'grant' and row.intent.parent_id:
            return ActorContext(principal.user_id, principal.id, PrincipalKind.GRANT,
                                binding.id, row.intent.owner_id, None, row.intent.parent_id)
        raise auth.Denied('creation principal is not human or delegated')

    @storage_boundary
    def activate(self, grant_id, *, expected_revision):
        outside_transaction()
        row = self._row(grant_id)
        self._expected(row, expected_revision)
        actor = self._intent_actor(row)
        chain = self._creation_chain(actor, row)
        content = self._read_bundle(actor, row.intent.bundle_id)
        receipt = self.bindings.activation(grant_id)
        if type(receipt) is not RoleBinding or receipt != self._info(row).binding:
            raise auth.Denied('matching trusted activation receipt required')
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=row.intent.project_id)
            row = self._row(grant_id)
            self._expected(row, expected_revision)
            self._check_row(row, state='reserved')
            self._creation_actor(actor, row)
            if chain:
                self._recheck(actor, chain)
            self._normalized(row.intent.bundle, soul.parse(content.manifest.decode()))
            if row.credential_version_id is None or self.bindings.current(receipt.project_id, receipt.target_id) != receipt:
                raise auth.Denied('activation binding is no longer current')
            if row.intent.parent_id:
                self._check_ancestors_local(row)
            if self.bindings.activation(grant_id) != receipt:
                raise auth.Denied('activation receipt changed')
            self._cas(row, state=row.next_state('activate'))
            self._event(row, None, 'activate')
            return self._info(row)

    def _check_ancestors_local(self, row):
        seen = {row.id}
        child = self._info(row)
        while row.intent.parent_id:
            row = self._row(row.intent.parent_id)
            if row.id in seen or len(seen) >= 32:
                raise auth.Denied('invalid delegation ancestry')
            seen.add(row.id)
            self._check_row(row)
            info = self._info(row)
            if (info.owner_id, info.project_id, info.policy) != (child.owner_id, child.project_id, child.policy):
                raise auth.Denied('ancestor scope or approved policy changed')
            binding = m.CredentialBinding.objects.filter(token_user_id=row.principal.user_id).first()
            if binding is None:
                raise auth.Denied('ancestor credential revoked')
            self._credential(ActorContext(row.principal.user_id, row.principal_id, PrincipalKind.GRANT,
                binding.id, info.owner_id, None, row.id))
            if self.bindings.current(info.project_id, info.target_id) != info.binding:
                raise auth.Denied('ancestor binding changed')
            child = info

    @storage_boundary
    def revoke(self, actor, grant_id, *, expected_revision):
        row = self._row(grant_id)
        self._human(actor, 'access.revoke_grant', row.intent.project_id)
        if row.intent.owner_id != actor.user_id:
            raise auth.NotVisible('grant not found')
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=row.intent.project_id)
            row = self._row(grant_id)
            self._human(actor, 'access.revoke_grant', row.intent.project_id)
            self._expected(row, expected_revision)
            self._cas(row, state=row.next_state('revoke'))
            self._event(row, actor, 'revoke')
            return self._info(row)

    def expire(self, grant_id, *, expected_revision):
        row = self._row(grant_id)
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=row.intent.project_id)
            row = self._row(grant_id)
            self._expected(row, expected_revision)
            if row.intent.expires_at > timezone.now():
                raise Conflict('grant has not expired')
            self._cas(row, state=row.next_state('expire'))
            self._event(row, None, 'expire')
            return self._info(row)

    def record_retirement(self, grant_id, *, expected_revision):
        receipt = self.bindings.retirement(grant_id, expected_revision)
        row = self._row(grant_id)
        if (type(receipt) is not RetirementReceipt or receipt.binding != self._info(row).binding
                or receipt.revision != expected_revision):
            raise auth.Denied('trusted matching transport retirement receipt required')
        outcome = receipt.outcome
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=row.intent.project_id)
            row = self._row(grant_id)
            self._expected(row, expected_revision)
            if row.state not in ('revoked', 'expired'):
                raise Conflict('logical grant must be retired first')
            self._cas(row, retirement=outcome)
            if outcome == 'applied':
                Token.objects.filter(user_id=row.principal.user_id).delete()
            self._event(row, None, 'retirement')
            return self._info(row)

    def _message_authority(self, actor, target_id, dispatch):
        chain = self._chain(actor)
        info = chain[0][0]
        action = 'sessions.dispatch_task' if dispatch else 'sessions.send_message'
        self.authorize(actor, action, project_ids=(info.project_id,))
        target = self.bindings.current(info.project_id, target_id)
        if type(target) is not RoleBinding or target.project_id != info.project_id:
            raise auth.Denied('message recipient binding is unavailable')
        row = self._row(target.grant_id)
        binding = m.CredentialBinding.objects.filter(token_user_id=row.principal.user_id).first()
        if binding is None:
            raise auth.Denied('message recipient credential is unavailable')
        recipient_actor = ActorContext(row.principal.user_id, row.principal_id, PrincipalKind.GRANT,
            binding.id, row.intent.owner_id, None, row.id)
        recipient_chain = self._chain(recipient_actor)
        if recipient_chain[0][0].binding != target:
            raise auth.Denied('message recipient binding changed')
        recipient = recipient_chain[0][1]
        if dispatch and not recipient.runtime.receives_dispatch:
            raise auth.Denied('target does not receive dispatch')
        # После всех provider waits повторяем source/recipient CAS и текущие
        # права в одной короткой admission transaction. Здесь только local queries.
        with transaction.atomic():
            Project.objects.select_for_update().get(pk=info.project_id)
            self._recheck(actor, chain)
            self._recheck(recipient_actor, recipient_chain)
            self._owner_permission(info.owner_id, action, info.project_id)
        return MessageAuthority(info.binding, target, info.owner_id, dispatch, info.owner_id if dispatch else None)

    def authorize_message(self, actor, *, target_id):
        return self._message_authority(actor, target_id, False)

    def authorize_dispatch(self, actor, *, target_id):
        return self._message_authority(actor, target_id, True)

    @storage_boundary
    @sensitive_variables('message')
    def send(self, actor, *, target_id, message):
        outside_transaction()
        if self.messages is None:
            raise auth.Denied('message delivery executor is not bound')
        if type(message) is not bytes or len(message) > 1024 * 1024:
            raise ValueError('bounded message bytes required')
        return self.messages.send(self.authorize_message(actor, target_id=target_id), message)

    @storage_boundary
    @sensitive_variables('message')
    def dispatch(self, actor, *, target_id, message):
        outside_transaction()
        if self.messages is None:
            raise auth.Denied('dispatch executor is not bound')
        if type(message) is not bytes or len(message) > 1024 * 1024:
            raise ValueError('bounded message bytes required')
        return self.messages.dispatch(self.authorize_dispatch(actor, target_id=target_id), message)
