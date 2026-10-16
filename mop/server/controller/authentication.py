"""Trusted HTTP auth boundary, без загрузки DRF views при настройке defaults."""
from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import BasePermission
from mop.server.access import services as access


def actor_for_request(request, *, grants=None):
    if not request.user or not request.user.is_authenticated:
        raise NotAuthenticated('authentication required')
    if request.auth is not None:
        return access.token_context(request.auth.key, grants=grants)
    raw = getattr(request, '_request', request)
    return access.session_context(raw.session.session_key)


class CurrentActorRequired(BasePermission):
    def has_permission(self, request, view):
        # Adapter задаёт root на view, не параметры body/query пользователя.
        request.actor = actor_for_request(request, grants=getattr(view, 'grant_resolver', None))
        return True
