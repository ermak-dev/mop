"""Стандартный Auth Admin с текущим actor; предметные adapters регистрируются явно."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin, GroupAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied

from mop.server.access import services as access
from mop.server.access.models import LoginIdentity, Principal
from .admincommon import IdentifierFreeSuccess, allowed, safe_view


class GlobalAuthAdmin(IdentifierFreeSuccess):
    actions = None
    inlines = ()
    list_editable = ()
    show_full_result_count = False
    save_as = False

    def _permission(self, request, verb):
        return allowed(request, 'auth.' + verb + '_' + self.model._meta.model_name)

    def has_module_permission(self, request):
        return self.has_view_permission(request) or self.has_add_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._permission(request, 'view')

    def has_add_permission(self, request):
        return self._permission(request, 'add')

    def has_change_permission(self, request, obj=None):
        return self._permission(request, 'change')

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}

    def get_inline_instances(self, request, obj=None):
        return []

    @staticmethod
    def _reject_bypass(request):
        if request.method == 'POST' and ('action' in request.POST or '_saveasnew' in request.POST or any(
                name.endswith(('-TOTAL_FORMS', '-INITIAL_FORMS')) for name in request.POST)):
            raise PermissionDenied('bulk actions and inline mutation are disabled')

    @safe_view
    def changelist_view(self, request, extra_context=None):
        self._reject_bypass(request)
        return super().changelist_view(request, extra_context)

    @safe_view
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        self._reject_bypass(request)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def delete_model(self, request, obj):
        raise PermissionDenied('account retirement is not bound')

    def delete_queryset(self, request, queryset):
        raise PermissionDenied('bulk deletion is disabled')

    def save_formset(self, request, form, formset, change):
        raise PermissionDenied('inline mutation is disabled')


class ScopedUserAdmin(GlobalAuthAdmin, UserAdmin):
    def get_queryset(self, request):
        # Машина/делегат не может получить human password/staff через Auth UI.
        return super().get_queryset(request).exclude(principal__kind__in=('service', 'grant'))

    def save_model(self, request, obj, form, change):
        if change and Principal.objects.filter(user=obj).exclude(kind='human').exists():
            raise PermissionDenied('nonhuman identity requires its named lifecycle')
        permitted = self.has_change_permission(request) if change else self.has_add_permission(request)
        if not permitted:
            raise PermissionDenied('user administration permission required')
        super().save_model(request, obj, form, change)
        if not change:
            access.bind_local_identity(obj.pk)

    def user_change_password(self, request, id, form_url=''):
        if not self.has_change_permission(request):
            raise PermissionDenied('user administration permission required')
        if (LoginIdentity.objects.filter(user_id=id, source__kind='ldap').exists()
                or Principal.objects.filter(user_id=id).exclude(kind='human').exists()):
            raise PermissionDenied('password is managed by the enrolled identity source')
        return super().user_change_password(request, id, form_url)

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        if obj and LoginIdentity.objects.filter(user=obj, source__kind='ldap').exists():
            return fields + ('username', 'groups', 'user_permissions')
        return fields

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == 'groups':
            kwargs['queryset'] = Group.objects.filter(directory_owner__isnull=True)
        return super().formfield_for_manytomany(db_field, request, **kwargs)


class ScopedGroupAdmin(GlobalAuthAdmin, GroupAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(directory_owner__isnull=True)

    def save_model(self, request, obj, form, change):
        permitted = self.has_change_permission(request) if change else self.has_add_permission(request)
        if not permitted:
            raise PermissionDenied('group administration permission required')
        return super().save_model(request, obj, form, change)


# Auto-discovery сохранил штатные User/Group forms; меняется только boundary.
for model, adapter in ((get_user_model(), ScopedUserAdmin), (Group, ScopedGroupAdmin)):
    if admin.site.is_registered(model):
        admin.site.unregister(model)
    admin.site.register(model, adapter)

from . import admincatalog, adminevidence  # noqa: E402,F401 — конечный registration set
