"""Admin adapters используют тот же Catalog use case и live Access, не staff role."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin, GroupAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from mop.server.access import services as access
from mop.server.access.models import LoginIdentity, Principal
from mop.server.catalog.models import Project


def _actor(request):
    return access.session_context(request.session.session_key)


def _allowed(request, action, projects=()):
    try:
        access.Access().authorize(_actor(request), action, project_ids=projects)
        return True
    except access.Denied:
        return False


class GlobalAuthAdmin:
    def _permission(self, request, verb):
        return _allowed(request, 'auth.' + verb + '_' + self.model._meta.model_name)

    def has_view_permission(self, request, obj=None):
        return self._permission(request, 'view')

    def has_add_permission(self, request):
        return self._permission(request, 'add')

    def has_change_permission(self, request, obj=None):
        return self._permission(request, 'change')

    def has_delete_permission(self, request, obj=None):
        return self._permission(request, 'delete')


class ScopedUserAdmin(GlobalAuthAdmin, UserAdmin):
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            access.bind_local_identity(obj.pk)

    def user_change_password(self, request, id, form_url=''):
        if not self.has_change_permission(request):
            raise PermissionDenied('user administration permission required')
        user = self.get_object(request, id)
        if user is None:
            from django.http import Http404
            raise Http404('resource not found')
        if (LoginIdentity.objects.filter(user=user, source__kind='ldap').exists()
                or Principal.objects.filter(user=user).exclude(kind='human').exists()):
            raise PermissionDenied('password is managed by the enrolled identity source')
        return super().user_change_password(request, id, form_url)

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        if obj and LoginIdentity.objects.filter(user=obj, source__kind='ldap').exists():
            return fields + ('username', 'groups')
        return fields

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == 'groups':
            kwargs['queryset'] = Group.objects.filter(directory_owner__isnull=True)
        return super().formfield_for_manytomany(db_field, request, **kwargs)


class ScopedGroupAdmin(GlobalAuthAdmin, GroupAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(directory_owner__isnull=True)


# auto-discovery зарегистрировал стандартные формы; меняются только adapters.
if admin.site.is_registered(get_user_model()):
    admin.site.unregister(get_user_model())
if admin.site.is_registered(Group):
    admin.site.unregister(Group)
admin.site.register(get_user_model(), ScopedUserAdmin)
admin.site.register(Group, ScopedGroupAdmin)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'origin', 'puppet_limit', 'revision')
    fields = ('name', 'origin', 'puppet_limit', 'revision')
    readonly_fields = ('name', 'origin', 'revision')

    def has_view_permission(self, request, obj=None):
        return _allowed(request, 'catalog.view_project', (obj.pk,) if obj else ())

    def has_change_permission(self, request, obj=None):
        return _allowed(request, 'catalog.set_project_limit', (obj.pk,) if obj else ())

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        from .authviews import catalog_api
        try:
            ids = tuple(row.id for row in catalog_api().list_projects(_actor(request)))
        except access.Denied as error:
            raise PermissionDenied(str(error)) from None
        return super().get_queryset(request).filter(pk__in=ids)

    def save_model(self, request, obj, form, change):
        from .authviews import catalog_api
        try:
            result = catalog_api().set_project_limit(_actor(request), obj.pk,
                obj.puppet_limit, expected_revision=obj.revision)
        except access.Denied as error:
            raise PermissionDenied(str(error)) from None
        obj.revision = result.revision
