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
        return result

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
        return SelectedLaunch(policy.id, policy.digest, policy.project_id, policy.owner_id, policy.definition_id,
            policy.bootstrap_revision, policy.image_id, policy.source_origin, policy.source_commit, policy.source_ref,
            policy.desired_model, policy.model_config_digest, refs, identity.name, identity.email)


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
