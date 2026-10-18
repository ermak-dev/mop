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
        'desired_model', 'model_config_digest')}


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

    def transfer_profile(self, selected):
        """SQL-only byte proof: immutable sizes + выбранные public metadata.

        Definition publisher seal связывает normalized manifest с retained bytes.
        Полная v1 shape включает optional secrets и потому не короче исходного
        manifest после JSON parsing. Порядок списков на длину не влияет, повторов
        soul schema не допускает. Retained UTF-8 [manifest,prompt,provenance] имеет
        длину >= UTF-8 prompt; это консервативный bound, не secret decrypt в SQL.
        Display name ещё не выделен: wire validator ограничивает его 64 ASCII.
        """
        from mop.common import initial_transfer as transfer
        from mop.common.initialcreate import MACHINE, PHASE_CONSUMERS
        from mop.common.soul import Soul, Runtime, Capabilities, Secrets, VERSION
        from mop.server.access import models as a
        from mop.server.catalog.models import ImageRevision
        from mop.server.secrets.models import SecretVersion
        definition = a.AgentDefinitionVersion.objects.filter(pk=selected.definition_id,
            project_id=selected.project_id, owner_id=selected.owner_id,
            agentdefinitionseal__isnull=False).values('role', 'digest', 'driver',
                'receives_dispatch', 'dispatch', 'prompt_name', 'content_version__size').first()
        image = ImageRevision.objects.filter(pk=selected.image_id).values('policy_id',
            'policy__cpu', 'policy__memory_mb', 'policy__disk_mb').first()
        sizes = dict(SecretVersion.objects.filter(pk=selected.bootstrap_policy.version_id).values_list('id', 'size'))
        versions = {row['id']: row for row in SecretVersion.objects.filter(
            pk__in=[version for _, version in selected.consumers]).values('id', 'size', 'secret__name', 'secret__kind')}
        if definition is None or image is None or selected.bootstrap_policy.version_id not in sizes or len(versions) != len(selected.consumers):
            raise Denied('initial launch transfer metadata is unavailable')
        capabilities = tuple(a.DefinitionCapability.objects.filter(definition_id=selected.definition_id).values_list('scope', 'verb'))
        names = tuple(a.DefinitionSecretName.objects.filter(definition_id=selected.definition_id).values_list('kind', 'name'))
        manifest = Soul(definition['role'], VERSION,
            Runtime(definition['driver'], definition['receives_dispatch']),
            Capabilities(tuple(verb for scope, verb in capabilities if scope == 'node'),
                tuple(verb for scope, verb in capabilities if scope == 'cluster'), definition['dispatch'],
                tuple(a.DefinitionSpawn.objects.filter(definition_id=selected.definition_id).values_list('role', flat=True))),
            Secrets(tuple(name for kind, name in names if kind == 'files'), tuple(name for kind, name in names if kind == 'vars')),
            definition['prompt_name']).to_dict()
        result = {}
        for phase in MACHINE:
            binary_sizes = []
            consumers = []
            for purpose, version in selected.consumers:
                if purpose in PHASE_CONSUMERS[phase]:
                    row = versions[version]
                    consumers.append(dict(purpose=purpose, version_id=str(version),
                        name=row['secret__name'], kind=row['secret__kind'], value=''))
                    binary_sizes.append(row['size'])
            role = None
            if phase in ('initial_identity', 'native_exec'):
                role = dict(manifest=manifest, prompt='')
                binary_sizes.append(definition['content_version__size'])
            bootstrap = None
            if phase == 'bootstrap_body':
                bootstrap = dict(envelope='')
                binary_sizes.append(sizes[selected.bootstrap_policy.version_id])
            value = dict(schema_version=1, work=transfer.bound_work(definition['role'], definition['digest'], phase),
                phase=phase, bootstrap_revision=str(selected.bootstrap_revision), bootstrap_digest=selected.bootstrap_policy.digest,
                image_digest=hashlib.sha256(('image-revision:'+str(selected.image_id)).encode()).hexdigest(),
                display_name='pu-'+'x'*59+'-1', image_revision=str(selected.image_id),
                image_policy=dict(policy_id=str(image['policy_id']), cpu=image['policy__cpu'],
                    memory_mb=image['policy__memory_mb'], disk_mb=image['policy__disk_mb']),
                selection=selected.public(), git_name=selected.git_name, git_email=selected.git_email,
                role=role, bootstrap=bootstrap, consumers=consumers)
            measured = transfer.measure(value)
            for size in binary_sizes:
                measured = measured.add_binary(size)
            result[phase] = measured
        return result

    def require_transfer(self, selected):
        from mop.common.initial_transfer import TransferError
        try:
            for phase, profile in self.transfer_profile(selected).items():
                profile.require(phase)
        except TransferError as error:
            raise Denied(str(error)) from None

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

    def read(self, policy_id, *, git_name=None, git_email=None):
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
            policy.desired_model, policy.model_config_digest, refs, identity.name, identity.email)


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
              source_origin, source_commit, source_ref, desired_model, model_config_digest, consumers):
    """Явный trusted SQL application port, не node wire и не включение physical caps."""
    from mop.server.catalog.models import InitialCreatePolicy, InitialCreateConsumer, InitialCreateSelection, Project
    if (type(consumers) is not tuple or any(type(item) is not tuple or len(item) != 2
            or item[0] not in CONSUMERS or type(item[1]) is not UUID for item in consumers)):
        raise ValueError('exact typed initial launch consumer references required')
    with transaction.atomic():
        Access().authorize(actor, 'catalog.configure_project', project_ids=(project_id,))
        Project.objects.select_for_update().get(pk=project_id)
        values = dict(project_id=project_id, owner_id=actor.human_owner_id, definition_id=definition_id,
            bootstrap_revision=bootstrap_revision, image_id=image_id, source_origin=source_origin,
            source_commit=source_commit, source_ref=source_ref, desired_model=desired_model,
            model_config_digest=model_config_digest)
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
