"""Разовое явное назначение обычных Django Groups (#747), не role permissions.

Recipe не владеет membership после применения. Даже отозванная recipe не
становится вторым permission engine у активного Actor; receipt хранит прошлое.
"""
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from django.contrib.auth.models import Group
from django.db import transaction

from . import models as m, services as auth


def canonical_groups(values):
    if (type(values) is not list or len(values) > 32
            or any(type(value) is not int or not 0 < value < 2**63 for value in values)
            or len(set(values)) != len(values)):
        raise ValueError('bounded distinct positive Django Group IDs required')
    return sorted(values)


def profile_digest(definition_id, groups):
    return hashlib.sha256(json.dumps(dict(definition_id=str(definition_id), group_ids=groups),
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def ordinary_groups(identifiers, *, lock=False):
    rows = Group.objects.order_by('pk')
    if lock:
        rows = rows.select_for_update()
    groups = tuple(rows.filter(pk__in=identifiers))
    if len(groups) != len(identifiers) or m.DirectoryGroupRule.objects.filter(group_id__in=identifiers).exists():
        raise auth.Denied('ordinary local Django Groups required')
    return groups


def require_profile(profile_id, digest, *, definition_id, owner_id, project_id, live=True, lock=False):
    rows = m.AgentRBACProfile.objects.select_related('definition')
    if lock:
        rows = rows.select_for_update(of=('self',))
    row = rows.filter(pk=profile_id).first()
    if (row is None or (row.digest, row.definition_id, row.definition.owner_id, row.definition.project_id) !=
            (digest, definition_id, owner_id, project_id)
            or canonical_groups(row.group_ids) != row.group_ids
            or profile_digest(row.definition_id, row.group_ids) != row.digest):
        raise auth.Denied('selected agent RBAC profile scope or immutable proof changed')
    if live and m.ProfileRevocation.objects.filter(profile=row).exists():
        raise auth.Denied('selected agent RBAC profile is revoked')
    return row


@dataclass(frozen=True)
class InitialRBACSelection:
    """SQL authority port возвращает exact pins, но не subject/groups из wire."""
    profile_id: UUID
    profile_digest: str
    policy_id: UUID
    policy_digest: str
    reservation_id: UUID


class AgentRBACProfiles:
    @staticmethod
    def authorize(actor):
        for action in ('auth.change_user', 'auth.change_group'):
            auth.Access().authorize(actor, action)

    def approve(self, actor, definition_id, group_ids):
        identifiers = canonical_groups(group_ids)
        if type(definition_id) is not UUID:
            raise ValueError('exact agent definition ID required')
        with transaction.atomic():
            self.authorize(actor)
            definition = m.AgentDefinitionVersion.objects.filter(pk=definition_id).first()
            if definition is None or not m.AgentDefinitionSeal.objects.filter(definition=definition).exists():
                raise auth.NotVisible('sealed agent definition not found')
            ordinary_groups(identifiers, lock=True)
            self.authorize(actor)
            return m.AgentRBACProfile.objects.create(definition=definition, group_ids=identifiers,
                digest=profile_digest(definition.id, identifiers), approved_by_id=actor.user_id)

    def revoke(self, actor, profile_id):
        with transaction.atomic():
            self.authorize(actor)
            row = m.AgentRBACProfile.objects.select_for_update().filter(pk=profile_id).first()
            if row is None:
                raise auth.NotVisible('agent RBAC profile not found')
            self.authorize(actor)
            return m.ProfileRevocation.objects.get_or_create(profile=row,
                defaults={'revoked_by_id': actor.user_id})[0]
