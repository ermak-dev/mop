"""Admin session boundary; отдельный модуль избегает auto-discovery цикла."""
from functools import wraps
from django.contrib.admin import AdminSite
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from mop.server.access import services as access


def _unavailable():
    response = JsonResponse({'error': {'code': 'authority_unavailable',
        'message': 'authentication database unavailable'}}, status=503)
    response['Cache-Control'] = 'no-store'
    return response


class AccessAdminSite(AdminSite):
    index_template = 'admin/mop/index.html'

    def each_context(self, request):
        from .admincommon import allowed
        return {**super().each_context(request),
                'proxy_panel_url': '/llm/management.html' if request.user.is_authenticated
                and allowed(request, 'controller.manage_proxy') else None}

    def has_permission(self, request):
        if not super().has_permission(request):
            return False
        try:
            access.current_actor(access.session_context(request.session.session_key))
            return True
        except access.Denied:
            return False

    @method_decorator(never_cache)
    @login_not_required
    def login(self, request, extra_context=None):
        from django.contrib.admin.forms import AdminAuthenticationForm
        from django.contrib.auth.views import LoginView
        from .authbackend import AuthenticationUnavailable
        from .authviews import login_session

        class BoundLoginView(LoginView):
            def form_valid(self, form):
                # Form validation завершает LDAP I/O до начала login transaction.
                login_session(self.request, form.get_user())
                return HttpResponseRedirect(self.get_success_url())

        try:
            if request.method == 'GET' and self.has_permission(request):
                return HttpResponseRedirect(reverse('admin:index', current_app=self.name))
            # Стандартный Admin context/form сохранён; меняется только form_valid.
            context = {**self.each_context(request), 'title': _('Log in'), 'subtitle': None,
                'app_path': request.get_full_path(), 'username': request.user.get_username()}
            if REDIRECT_FIELD_NAME not in request.GET and REDIRECT_FIELD_NAME not in request.POST:
                context[REDIRECT_FIELD_NAME] = reverse('admin:index', current_app=self.name)
            context.update(extra_context or {})
            request.current_app = self.name
            return BoundLoginView.as_view(extra_context=context,
                authentication_form=self.login_form or AdminAuthenticationForm,
                template_name=self.login_template or 'admin/login.html')(request)
        except (DatabaseError, AuthenticationUnavailable):
            return _unavailable()
        except access.Denied as error:
            raise PermissionDenied(str(error)) from None

    def admin_view(self, view, cacheable=False):
        wrapped = super().admin_view(view, cacheable)
        @wraps(wrapped)
        def safe(request, *args, **kwargs):
            try:
                return wrapped(request, *args, **kwargs)
            except DatabaseError:
                return _unavailable()
        return safe
