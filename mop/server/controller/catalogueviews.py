"""Authenticated action catalogue #633: конечный query, no-store, без claims."""
from uuid import UUID

from rest_framework import serializers
from rest_framework.response import Response

from mop.server.access import services as access
from mop.server.access.catalogue import action_permissions
from .authviews import PrivateView
from .runtime import installation_snapshot


def grant_authority():
    """SQL-only root: тот же живой assignment/ancestry resolver, что у команд."""
    from mop.server.access.grants import Grants
    from .machinebinding import AssignmentBindings
    return Grants(provider=None, bindings=AssignmentBindings()).local_authority()


def actor_snapshot(actor):
    return {'user_id': actor.user_id, 'kind': actor.kind.value,
        'human_owner_id': actor.human_owner_id,
        'node_id': str(actor.node_id) if actor.node_id else None,
        'grant_id': str(actor.grant_id) if actor.grant_id else None}


def scope_query(query):
    """QueryDict не теряет повторяющиеся поля до валидации."""
    fields = ('project_id', 'node_id', 'client_id')
    if (set(query) - {'schema_version', *fields} or
            any(len(query.getlist(name)) != 1 for name in query) or
            query.get('schema_version', '1') != '1'):
        raise serializers.ValidationError('invalid capability query')
    scope = {}
    for name in fields:
        value = query.get(name)
        if value is not None:
            try:
                parsed = UUID(value)
            except (ValueError, TypeError, AttributeError):
                raise serializers.ValidationError('canonical scope ids required') from None
            if str(parsed) != value:
                raise serializers.ValidationError('canonical scope ids required')
            value = parsed
        scope[name] = value
    return scope


class Capabilities(PrivateView):
    @property
    def grant_resolver(self):
        return grant_authority()

    def get(self, request):
        scope = scope_query(request.query_params)
        before = installation_snapshot()['epoch']
        grants = self.grant_resolver
        actor = access.current_actor(request.actor, grants=grants)
        actions = action_permissions(actor, grants=grants, **scope)
        access.current_actor(actor, grants=grants)
        if installation_snapshot()['epoch'] != before:
            raise access.Denied('installation epoch changed')
        return Response({'schema_version': 1, 'catalogue_version': 1, 'epoch': before,
            'actor': actor_snapshot(actor),
            'scope': {name: str(value) if value is not None else None for name, value in scope.items()},
            'actions': list(actions)})
