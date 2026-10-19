"""SQL-only выбор первого запуска (#721); provider и физические ports здесь не живут.

Frozen public create(node,definition) читает явный каталоговый указатель. Он
никогда не выбирает source из image policy, provenance bootstrap или файлов.
Immutable policy+digest фиксируются до reserve и читаются заново на каждой фазе.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID

from django.db import transaction

from mop.common.initialcreate import CONSUMERS, source_ref as valid_source_ref
from mop.server.access.services import Access, Denied
from mop.server.access.delegation import ApprovedPolicy
from mop.server.operations.service import Refused
from .machinebinding import owner_identity


@dataclass(frozen=True)
class SelectedLaunch:
    policy_id: UUID
    digest: str
    project_id: UUID
    owner_id: int
    definition_id: UUID
    bootstrap_revision: UUID
    bootstrap_policy: ApprovedPolicy
    image_id: UUID
    source_origin: str
    source_commit: str
    source_ref: str
    desired_model: str
    model_config_digest: str
    consumers: tuple
    git_name: str
    git_email: str
    rbac_profile_id: UUID
    rbac_profile_digest: str

    def public(self):
        """Только пины; version refs не являются API для чтения секретов по UUID."""
        return dict(policy_id=str(self.policy_id), digest=self.digest,
            source=dict(origin=self.source_origin, commit=self.source_commit, ref=self.source_ref),
            desired_model=self.desired_model, model_config_digest=self.model_config_digest,
            consumers=[dict(purpose=purpose, version_id=str(version)) for purpose, version in self.consumers])


def fingerprint(values, refs):
    return hashlib.sha256(json.dumps({**values, 'consumers': [(purpose, str(version)) for purpose, version in refs]},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def policy_values(row):
    return {name: str(getattr(row, name)) for name in ('project_id', 'owner_id', 'definition_id',
        'bootstrap_revision', 'image_id', 'source_origin', 'source_commit', 'source_ref',
        'desired_model', 'model_config_digest', 'rbac_profile_id', 'rbac_profile_digest')}


def validate_references(policy, refs):
    """Только project-scoped allowlist; установка/чужой owner не становятся consumer."""
    from mop.server.secrets.models import SecretVersion
    from mop.server.preparation.models import BootstrapSecretReference
    if (len(refs) > 32 or len({version for _, version in refs}) != len(refs)
            or sum(purpose == 'model_config' for purpose, _ in refs) != 1):
        raise Denied('exact initial launch model configuration and bounded consumers required')
    versions = {row.id: row for row in SecretVersion.objects.select_related('secret').filter(
        pk__in=[version for _, version in refs])}
    for purpose, version_id in refs:
        row = versions.get(version_id)
        if purpose not in CONSUMERS or row is None:
            raise Denied('initial launch consumer reference is unavailable')
        secret = row.secret
        expected = {'source_credential': ('git', 'credential'), 'bootstrap_input': ('project', None),
            'model_config': ('project', 'file'), 'model_credential': ('proxy', 'credential')}[purpose]
        if (secret.project_id != policy.project_id or secret.created_by_id != policy.owner_id
                or secret.revoked or secret.active_version_id != version_id
                or secret.purpose != expected[0] or expected[1] is not None and secret.kind != expected[1]
                or secret.purpose == 'project' and secret.kind not in ('file', 'variable')):
            raise Denied('initial launch consumer scope or active version changed')
    bootstrap = set(BootstrapSecretReference.objects.filter(revision_id=policy.bootstrap_revision)
        .values_list('version_id', flat=True))
    if bootstrap != {version for purpose, version in refs if purpose == 'bootstrap_input'}:
        raise Denied('initial launch bootstrap consumer references changed')


class LaunchSelections:
    """Реальный production reader каталога; отсутствие настроенного выбора -- отказ."""
    def rbac_authority(self, actor, reservation_id):
        return InitialRBACAuthority(actor, reservation_id)

    def resolve(self, actor, command, reservation=None):
        from mop.server.catalog.models import InitialCreateSelection
        if reservation is not None:
            policy_id = reservation.policy_id
        else:
            policy_id = InitialCreateSelection.objects.filter(project_id=command.intent.project_id,
                owner_id=actor.human_owner_id, definition_id=command.definition_id).values_list('policy_id', flat=True).first()
        if policy_id is None:
            raise Refused('controller_unavailable', safe_reason='trusted SQL initial launch selection is unavailable')
        result = self.read(policy_id, git_name=reservation.git_name if reservation else None,
            git_email=reservation.git_email if reservation else None)
        if (result.project_id, result.owner_id, result.definition_id) != (
                command.intent.project_id, actor.human_owner_id, command.definition_id):
            raise Denied('initial launch selection belongs to another owner, project or definition')
        if reservation is not None:
            self.require_reservation(result, reservation)
        return result

    def require_reservation(self, selected, reservation):
        if ((selected.project_id, selected.owner_id, selected.digest) !=
                (reservation.project_id, reservation.actor_id, reservation.policy_digest)
                or (selected.bootstrap_policy.approval_id, selected.bootstrap_policy.version_id,
                    selected.bootstrap_policy.digest) != (reservation.bootstrap_approval_id,
                    reservation.bootstrap_version_id, reservation.bootstrap_digest)):
            raise Denied('pinned initial bootstrap approval, content or digest changed')

    def require_grant(self, selected, grant_id):
        from mop.server.access.models import Grant
        row = Grant.objects.select_related('intent').filter(pk=grant_id).first()
        proof = selected.bootstrap_policy
        if (row is None or (row.intent.owner_id, row.intent.project_id, row.intent.definition_id,
                row.intent.policy_approval_id, row.intent.policy_version_id, row.intent.policy_digest) !=
                (selected.owner_id, selected.project_id, selected.definition_id,
                 proof.approval_id, proof.version_id, proof.digest)):
            raise Denied('initial grant and selected bootstrap proof differ')

    def read(self, policy_id, *, git_name=None, git_email=None, check_rbac=True):
        from mop.server.catalog.models import InitialCreatePolicy, InitialCreateConsumer
        policy = InitialCreatePolicy.objects.select_related('project', 'image__policy').filter(pk=policy_id).first()
        if policy is None:
            raise Refused('controller_unavailable', safe_reason='trusted SQL initial launch selection is unavailable')
        refs = tuple(InitialCreateConsumer.objects.filter(policy=policy).order_by('purpose', 'version_id')
                     .values_list('purpose', 'version_id'))
        if (fingerprint(policy_values(policy), refs) != policy.digest
                or policy.source_origin != policy.project.origin or policy.image.policy.project_id != policy.project_id
                or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', policy.source_commit)
                or not valid_source_ref(policy.source_ref)
                or not policy.desired_model or len(policy.desired_model) > 128
                or any(ord(c) < 33 or ord(c) == 127 for c in policy.desired_model)
                or not re.fullmatch('[0-9a-f]{64}', policy.model_config_digest)):
            raise Denied('immutable initial launch selection changed')
        if policy.rbac_profile_id is None:
            raise Denied('explicit approved initial agent RBAC profile required')
        if check_rbac:
            from mop.server.access.rbac import require_profile
            require_profile(policy.rbac_profile_id, policy.rbac_profile_digest, definition_id=policy.definition_id,
                project_id=policy.project_id, owner_id=policy.owner_id)
        validate_references(policy, refs)
        identity = owner_identity(policy.owner_id)
        if (git_name is not None or git_email is not None) and (git_name, git_email) != (identity.name, identity.email):
            raise Denied('pinned owner Git profile changed')
        from mop.server.preparation.models import BootstrapApproval, ApprovalRevocation
        approval = BootstrapApproval.objects.select_related('revision').filter(
            revision_id=policy.bootstrap_revision, human_id=policy.owner_id,
            revision__project_id=policy.project_id).first()
        if approval is None:
            raise Denied('selected initial bootstrap approval is unavailable')
        if ApprovalRevocation.objects.filter(approval=approval).exists():
            raise Denied('selected initial bootstrap approval is revoked')
        proof = ApprovedPolicy(approval.id, approval.revision.content_version_id, approval.revision.digest,
            approval.human_id, approval.revision.project_id)
        return SelectedLaunch(policy.id, policy.digest, policy.project_id, policy.owner_id, policy.definition_id,
            policy.bootstrap_revision, proof, policy.image_id, policy.source_origin, policy.source_commit, policy.source_ref,
            policy.desired_model, policy.model_config_digest, refs, identity.name, identity.email,
            policy.rbac_profile_id, policy.rbac_profile_digest)


class InitialRBACAuthority:
    """Контроллер связывает exact committed reservation и Grant; Access не знает Ops."""
    def __init__(self, actor, reservation_id):
        self.actor, self.reservation_id = actor, reservation_id

    def resolve(self, actor, grant_id):
        from mop.server.access.context import ActorKind
        from mop.server.access.services import current_actor
        from mop.server.access.models import Grant
        from mop.server.access.rbac import InitialRBACSelection
        from mop.server.controller.models import Installation
        from mop.server.operations.models import CreateReservation
        from mop.server.operations.launch import identities
        current_actor(actor)
        if actor != self.actor or actor.kind is not ActorKind.HUMAN or actor.grant_id is not None:
            raise Denied('initial RBAC requires its current human creator')
        reservation = CreateReservation.objects.filter(pk=self.reservation_id, actor_id=actor.user_id).first()
        installation = Installation.objects.get(pk=1)
        if reservation is None or reservation.epoch != installation.epoch or not installation.effects_enabled:
            raise Denied('current initial RBAC reservation is unavailable')
        selections = LaunchSelections()
        # Receipt retry читает immutable pins, не actual memberships/live recipe.
        selected = selections.read(reservation.policy_id, git_name=reservation.git_name,
            git_email=reservation.git_email, check_rbac=False)
        selections.require_reservation(selected, reservation)
        selections.require_grant(selected, grant_id)
        target, incarnation = identities(actor, reservation.request_id)
        grant = Grant.objects.select_related('intent').get(pk=grant_id)
        intent = grant.intent
        if ((intent.actor_id, intent.actor_credential_id, intent.parent_id, intent.target_id,
                intent.incarnation, intent.request_id, intent.expires_at) !=
                (actor.user_id, actor.credential_id, None, target, incarnation,
                 reservation.request_id, reservation.expires_at)):
            raise Denied('initial RBAC grant origin differs from its committed reservation')
        return InitialRBACSelection(selected.rbac_profile_id, selected.rbac_profile_digest,
            selected.policy_id, selected.digest, reservation.id)


def require_initial_rbac_activation(grant_id):
    """Только pending INITIAL activation: lock profile до Grant COMMIT.

    Replacement/обычная active delegation сюда не приходят. Recipe revocation
    не переопределяет Django Groups после состоявшейся активации.
    """
    from mop.server.operations.models import CreateBinding
    from mop.server.access.models import AgentRBACProvisioning, Grant
    from mop.server.access.rbac import require_profile
    binding = CreateBinding.objects.select_related('reservation', 'operation__restartbinding').filter(
        operation__restartbinding__grant_id=grant_id).first()
    if binding is None:
        if AgentRBACProvisioning.objects.filter(grant_id=grant_id, grant__state='reserved').exists():
            raise Denied('initial RBAC activation create binding is unavailable')
        return
    grant = Grant.objects.select_related('intent').get(pk=grant_id)
    if grant.state == 'active':
        return
    if binding.reservation_id is None:
        raise Denied('initial RBAC activation reservation is unavailable')
    selections = LaunchSelections()
    selected = selections.read(binding.reservation.policy_id, git_name=binding.reservation.git_name,
        git_email=binding.reservation.git_email, check_rbac=False)
    selections.require_reservation(selected, binding.reservation)
    selections.require_grant(selected, grant_id)
    if not AgentRBACProvisioning.objects.filter(subject_id=grant.subject_id, grant_id=grant_id,
            profile_id=selected.rbac_profile_id, profile_digest=selected.rbac_profile_digest,
            policy_id=selected.policy_id, policy_digest=selected.digest,
            reservation_id=binding.reservation_id).exists():
        raise Denied('exact initial RBAC provisioning receipt required for activation')
    require_profile(selected.rbac_profile_id, selected.rbac_profile_digest,
        definition_id=selected.definition_id, owner_id=selected.owner_id, project_id=selected.project_id,
        lock=transaction.get_connection().in_atomic_block)


class InitialReservationApprovals:
    """Request-local SQL authority initial reserve, не передаваемый по wire proof.

    Generic Grants.reserve сохраняет все owner/issuer/spawn проверки; только
    его approval port получает exact committed selection вместо latest.
    """
    def __init__(self, query, actor, reservation_id):
        self.query, self.actor, self.reservation_id = query, actor, reservation_id

    def resolve(self):
        from mop.server.access.context import ActorKind
        from mop.server.access.services import current_actor
        from mop.server.controller.models import Installation
        from mop.server.operations.models import CreateReservation
        from mop.server.operations.launch import identities
        current_actor(self.actor)
        if self.actor.kind is not ActorKind.HUMAN or self.actor.grant_id is not None:
            raise Denied('initial reservation requires its current human owner')
        row = CreateReservation.objects.filter(pk=self.reservation_id, actor_id=self.actor.user_id).first()
        installation = Installation.objects.get(pk=1)
        if row is None or row.epoch != installation.epoch or not installation.effects_enabled:
            raise Denied('current initial create reservation is unavailable')
        selected = LaunchSelections().read(row.policy_id, git_name=row.git_name, git_email=row.git_email)
        LaunchSelections().require_reservation(selected, row)
        target, incarnation = identities(self.actor, row.request_id)
        return row, selected, target, incarnation

    def arguments(self):
        row, selected, target, incarnation = self.resolve()
        return dict(project_id=row.project_id, definition_id=selected.definition_id, target_id=target,
            incarnation=incarnation, request_id=row.request_id, expires_at=row.expires_at)

    def _proof(self, binding):
        row, selected, target, incarnation = self.resolve()
        if ((binding.owner_id, binding.actor_id, binding.project_id, binding.definition_id,
                binding.parent_id, binding.target_id, binding.incarnation) !=
                (row.actor_id, row.actor_id, row.project_id, selected.definition_id, None, target, incarnation)):
            raise Denied('initial reservation policy binding changed')
        return selected.bootstrap_policy

    def pin(self, binding):
        proof = self._proof(binding)
        self.query.validate(proof, binding)
        return proof

    def validate(self, proof, binding):
        if proof != self._proof(binding):
            raise Denied('initial reservation requires its exact selected bootstrap proof')
        return self.query.validate(proof, binding)


def configure(actor, *, project_id, definition_id, bootstrap_revision, image_id,
              source_origin, source_commit, source_ref, desired_model, model_config_digest, consumers, rbac_profile_id):
    """Явный trusted SQL application port, не node wire и не включение physical caps."""
    from mop.server.catalog.models import InitialCreatePolicy, InitialCreateConsumer, InitialCreateSelection, Project
    if (type(consumers) is not tuple or any(type(item) is not tuple or len(item) != 2
            or item[0] not in CONSUMERS or type(item[1]) is not UUID for item in consumers)):
        raise ValueError('exact typed initial launch consumer references required')
    with transaction.atomic():
        Access().authorize(actor, 'catalog.configure_project', project_ids=(project_id,))
        Project.objects.select_for_update().get(pk=project_id)
        from mop.server.access.models import AgentRBACProfile
        from mop.server.access.rbac import require_profile
        profile = AgentRBACProfile.objects.filter(pk=rbac_profile_id).first()
        if profile is None:
            raise Denied('explicit approved initial agent RBAC profile required')
        require_profile(profile.id, profile.digest, definition_id=definition_id, project_id=project_id,
            owner_id=actor.human_owner_id)
        values = dict(project_id=project_id, owner_id=actor.human_owner_id, definition_id=definition_id,
            bootstrap_revision=bootstrap_revision, image_id=image_id, source_origin=source_origin,
            source_commit=source_commit, source_ref=source_ref, desired_model=desired_model,
            model_config_digest=model_config_digest, rbac_profile_id=profile.id, rbac_profile_digest=profile.digest)
        row = InitialCreatePolicy(**values)
        refs = tuple(sorted(consumers, key=lambda item: (item[0], str(item[1]))))
        row.digest = fingerprint(policy_values(row), refs)
        row.save()
        for purpose, version_id in refs:
            InitialCreateConsumer.objects.create(policy=row, purpose=purpose, version_id=version_id)
        result = LaunchSelections().read(row.id)
        InitialCreateSelection.objects.update_or_create(project_id=project_id, owner_id=actor.human_owner_id,
            definition_id=definition_id, defaults={'policy': row})
        return result
