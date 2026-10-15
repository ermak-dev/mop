"""Admin session boundary; отдельный модуль избегает auto-discovery цикла."""
from functools import wraps
from django.contrib.admin import AdminSite
from django.db import DatabaseError
from django.http import JsonResponse
from mop.server.access import services as access


def _unavailable():
    response = JsonResponse({'error': {'code': 'authority_unavailable',
        'message': 'authentication database unavailable'}}, status=503)
    response['Cache-Control'] = 'no-store'
    return response


class AccessAdminSite(AdminSite):
    def has_permission(self, request):
        if not super().has_permission(request):
            return False
        try:
            access.current_actor(access.session_context(request.session.session_key))
            return True
        except access.Denied:
            return False

    def login(self, request, extra_context=None):
        from .authbackend import AuthenticationUnavailable
        try:
            return super().login(request, extra_context)
        except (DatabaseError, AuthenticationUnavailable):
            return _unavailable()

    def admin_view(self, view, cacheable=False):
        wrapped = super().admin_view(view, cacheable)
        @wraps(wrapped)
        def safe(request, *args, **kwargs):
            try:
                return wrapped(request, *args, **kwargs)
            except DatabaseError:
                return _unavailable()
        return safe
