"""Материализованный proof конечного project-read; права решает только Access.

Cursor хранит HMAC proof, не membership/credential и не снимок полномочий.
Повторное разрешение после SELECT замечает revoke даже при прежнем ActorContext.
"""
from dataclasses import dataclass
import json
from uuid import UUID

from django.contrib.auth import get_user_model
from django.utils.crypto import salted_hmac

from ..catalog.models import NodeProject
from ..catalog.services import Catalog
from . import models as m
from .services import Access, Denied, NotVisible, current_actor, visible_project_ids


class ScopeTooWide(ValueError):
    """Нужен явный проект, а не усечённая область с успешным ответом."""


@dataclass(frozen=True)
class ReadScope:
    project_ids: tuple[UUID, ...]
    project_id: UUID | None
    node_id: UUID | None
    target_id: UUID | None
    incarnation: UUID | None
    proof: str


def _user_proof(user_id):
    """Только локальная SQL metadata; ни token/password, ни private definition."""
    user = get_user_model().objects.get(pk=user_id)
    # PK связей замечают remove/re-add с прежним эффективным набором прав.
    groups = tuple(user.groups.order_by('pk').values_list('pk', flat=True))
    group_links = tuple(user.groups.through.objects.filter(actor_id=user_id).order_by('pk').values_list('pk', 'group_id'))
    direct = tuple(user.user_permissions.through.objects.filter(actor_id=user_id).order_by('pk').values_list('pk', 'permission_id'))
    from django.contrib.auth.models import Group
    group_permissions = tuple(Group.permissions.through.objects.filter(group_id__in=groups)
        .order_by('pk').values_list('pk', 'group_id', 'permission_id'))
    memberships = tuple(m.ProjectMembership.objects.filter(user_id=user_id).order_by('pk').values_list(
        'pk','project_id','source_id','source__enabled','source__revision'))
    global_memberships = tuple(m.AllProjectMembership.objects.filter(user_id=user_id).order_by('pk').values_list(
        'pk','source_id','source__enabled','source__revision'))
    return (user.pk, user.kind, user.is_active, user.is_staff, user.is_superuser,
            group_links, direct, group_permissions, memberships, global_memberships)


def read_scope(actor, action, *, project_id=None, grants=None, max_projects=100):
    """Общая live authority; explicit filter сужает выборку, не права на соседей.

    Без фильтра scope ограничен до materialization. При explicit filter полный
    allowed set нужен для M2M anti-join: часть operation не становится публичной.
    Enrollment сужает project membership сервиса, grant — target/incarnation.
    """
    if project_id is not None and type(project_id) is not UUID:
        raise ValueError('canonical project UUID required')
    current_actor(actor, grants=grants)
    node_id, target_id, incarnation = actor.node_id, None, None
    users, lineage = {actor.user_id}, []
    if actor.grant_id is not None:
        row = m.Grant.objects.select_related('intent').get(pk=actor.grant_id)
        projects = (row.intent.project_id,)
        target_id, incarnation = row.intent.target_id, row.intent.incarnation
        while True:
            if len(lineage) >= 32 or row.pk in {link[0] for link in lineage}:
                raise Denied('invalid delegation ancestry')
            lineage.append((row.pk,row.revision,row.state,row.intent_id))
            users.update((row.subject_id,row.intent.owner_id))
            if row.intent.parent_id is None:
                break
            row = m.Grant.objects.select_related('intent').get(pk=row.intent.parent_id)
    elif node_id is not None:
        allowed = visible_project_ids(actor)
        projects = Catalog.current_project_ids(NodeProject.objects.filter(node_id=node_id,
            project_id__in=allowed).values_list('project_id', flat=True),
            limit=max_projects+1 if project_id is None else None)
    else:
        projects = visible_project_ids(actor, limit=max_projects+1 if project_id is None else None)
    if project_id is None and len(projects) > max_projects:
        raise ScopeTooWide('audit project scope exceeds 100; select a project')
    if project_id is not None and project_id not in projects:
        raise NotVisible('resource not found')
    if not projects:
        raise Denied('no currently authorized project scope')
    Access(grants=grants).authorize(actor, action, project_ids=projects,
                                   node_ids=(node_id,) if node_id else ())
    enrollment = tuple(m.ServiceEnrollment.objects.filter(actor_id=actor.user_id).values_list('pk','node_id','use_case'))
    clients = tuple(m.ClientOwnership.objects.filter(user_id=actor.user_id).order_by('pk').values_list('pk','client_id'))
    value = (action, actor.user_id, actor.credential_id, actor.grant_id, projects, project_id,
             node_id, target_id, incarnation, lineage, enrollment, clients,
             tuple(_user_proof(user_id) for user_id in sorted(users)))
    proof = salted_hmac('mop.read.scope.v1', json.dumps(value, default=str, separators=(',',':')),
                        algorithm='sha256').hexdigest()
    return ReadScope(tuple(projects), project_id, node_id, target_id, incarnation, proof)
