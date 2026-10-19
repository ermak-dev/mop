"""Стандартный Auth Admin с текущим actor; предметные adapters регистрируются явно."""
from django import forms
from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin, GroupAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied

from mop.server.access import services as access
from mop.server.access.models import LoginIdentity
from mop.server.access.rbac import ActorMemberships
from .admincommon import GuardedAdmin, IdentifierFreeSuccess, allowed, safe_view
from mop.server.catalog.models import Project
from mop.server.sessions.models import Assignment


class GlobalAuthAdmin(IdentifierFreeSuccess):
    actions = None
    inlines = ()
    list_editable = ()
    show_full_result_count = False
    save_as = False

    def _permission(self, request, verb):
        # Логический auth namespace: Actor зареган под своим app label,
        # action остаётся auth.<verb>_user, имя модели не участвует.
        return allowed(request, 'auth.' + verb + '_user' if self.model._meta.model_name == 'actor'
                       else 'auth.' + verb + '_' + self.model._meta.model_name)

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


class NonhumanMembershipForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ('groups', 'user_permissions')


class ScopedUserAdmin(GlobalAuthAdmin, UserAdmin):
    def get_queryset(self, request):
        rows = super().get_queryset(request)
        # Неавторизованный query hook по-прежнему не раскрывает nonhuman (#684).
        if not hasattr(request, 'session') or not (
                self.has_view_permission(request) or self.has_change_permission(request)):
            return rows.exclude(kind__in=('service', 'agent'))
        return rows

    @safe_view
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        self._reject_bypass(request)
        nonhuman = False
        if object_id is None:
            if request.method == 'POST' and 'kind' in request.POST:
                raise PermissionDenied('nonhuman identity requires its named lifecycle')
        else:
            permitted = self.has_change_permission(request) if request.method == 'POST' else (
                self.has_view_permission(request) or self.has_change_permission(request))
            if not permitted:
                raise PermissionDenied('user administration permission required')
            obj = self.get_object(request, unquote(object_id))
            nonhuman = obj is not None and obj.kind != 'human'
            if nonhuman and request.method == 'POST':
                # Readonly/unknown поля ModelForm игнорирует: здесь это явный отказ,
                # включая совпавшие значения canonical identity и будущие поля.
                writable = {'groups', 'user_permissions', 'csrfmiddlewaretoken', '_save', '_continue', '_addanother'}
                if set(request.POST) - writable:
                    raise PermissionDenied('only groups and user_permissions may be changed for a nonhuman actor')
        response = super().changeform_view(request, object_id, form_url, extra_context)
        if nonhuman and request.method == 'POST' and response.status_code == 200:
            response.status_code = 400
        return response

    def get_fieldsets(self, request, obj=None):
        if obj is not None and obj.kind != 'human':
            return ((None, {'fields': ('id', 'username', 'kind')}), ('Django RBAC', {
                'fields': ('groups', 'user_permissions'),
                'description': 'Revoking membership does not stop a running workload and does not delete the Actor.'}))
        return super().get_fieldsets(request, obj)

    def get_form(self, request, obj=None, **kwargs):
        if obj is not None and obj.kind != 'human':
            kwargs['form'] = NonhumanMembershipForm
        return super().get_form(request, obj, **kwargs)

    def save_model(self, request, obj, form, change):
        if change and obj.kind != 'human':
            # Не перевычисляем filtered queryset после form validation: managed
            # group мог сменить owner, и новый query молча выкинул бы выбранный ID.
            identifiers = {name: [row.pk for row in form.cleaned_data[name]]
                           for name in ('groups', 'user_permissions') if name in form.cleaned_data}
            ActorMemberships().replace(access.session_context(request.session.session_key), obj.pk,
                group_ids=identifiers.get('groups', []), permission_ids=identifiers.get('user_permissions', []))
            obj._rbac_memberships = identifiers
            return
        permitted = self.has_change_permission(request) if change else self.has_add_permission(request)
        if not permitted:
            raise PermissionDenied('user administration permission required')
        super().save_model(request, obj, form, change)
        if not change:
            access.bind_local_identity(obj.pk)

    def save_related(self, request, form, formsets, change):
        if form.instance.kind != 'human':
            if formsets:
                raise PermissionDenied('inline mutation is disabled')
            # Named owner уже применил M2M; generic form.save_m2m обошёл бы его.
            return
        return super().save_related(request, form, formsets, change)

    def log_change(self, request, obj, message):
        if obj.kind != 'human':
            message = 'Django RBAC memberships replaced: ' + str(obj._rbac_memberships)
        return super().log_change(request, obj, message)

    def user_change_password(self, request, id, form_url=''):
        if not self.has_change_permission(request):
            raise PermissionDenied('user administration permission required')
        if (LoginIdentity.objects.filter(user_id=id, source__kind='ldap').exists()
                or get_user_model().objects.filter(pk=id).exclude(kind='human').exists()):
            raise PermissionDenied('password is managed by the enrolled identity source')
        return super().user_change_password(request, id, form_url)

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        if obj is not None and obj.kind != 'human':
            fields += ('id', 'username', 'kind')
        if obj and LoginIdentity.objects.filter(user=obj, source__kind='ldap').exists():
            return fields + ('username', 'groups', 'user_permissions')
        return fields

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == 'groups':
            # Управляемые общие группы правил скрыты из ручного назначения (#685).
            kwargs['queryset'] = Group.objects.filter(directory_rule__isnull=True)
        return super().formfield_for_manytomany(db_field, request, **kwargs)


class ScopedGroupAdmin(GlobalAuthAdmin, GroupAdmin):
    @safe_view
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST' and set(request.POST) - {
                'name', 'permissions', 'csrfmiddlewaretoken', '_save', '_continue', '_addanother', '_popup', '_to_field'}:
            raise PermissionDenied('group reverse membership mutation is disabled')
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_queryset(self, request):
        # Проекция правила — общая группа, редактирование через admin запрещено.
        return super().get_queryset(request).filter(directory_rule__isnull=True)

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

from . import admincatalog, adminevidence, adminrbac  # noqa: E402,F401 — конечный registration set


# Сохраняем прежнюю owner-scoped evidence view под отдельным адресом;
# основной Assignment detail читает тот же DTO, что DRF.
assignment_evidence_admin = admin.site._registry[Assignment]
admin.site.unregister(Assignment)


def _actor(request):
    return access.session_context(request.session.session_key)


def _allowed(request, action, projects=()):
    try:
        access.Access().authorize(_actor(request), action, project_ids=projects)
        return True
    except access.Denied:
        return False


@admin.register(Assignment)
class AssignmentReadAdmin(GuardedAdmin):
    """Admin не получает отдельную authority/write path или Collector."""
    def get_urls(self):
        from django.urls import path
        return [
            path('evidence/', self.admin_site.admin_view(assignment_evidence_admin.changelist_view)),
            path('evidence/<path:object_id>/change/', self.admin_site.admin_view(assignment_evidence_admin.change_view)),
        ] + super().get_urls()

    list_display = ('id',)
    fields = ('read_projection',)
    readonly_fields = ('read_projection',)
    actions = None

    def has_module_permission(self, request):
        return _allowed(request, 'catalog.view_pool')

    def has_view_permission(self, request, obj=None):
        return _allowed(request, 'catalog.view_pool', (obj.project_id,) if obj else ())

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        from .projectionviews import queries
        try:
            ids = queries().visible_ids(_actor(request))
        except access.Denied as error:
            raise PermissionDenied(str(error)) from None
        return super().get_queryset(request).filter(pk__in=ids)

    def get_object(self, request, object_id, from_field=None):
        from .projectionviews import queries
        obj = super().get_object(request, object_id, from_field)
        if obj is not None:
            # Только request-local Model instance; Admin singleton не хранит actor/DTO.
            obj._read_projection = queries().detail(_actor(request), obj.pk)['puppet']
        return obj

    @admin.display(description='Read projection')
    def read_projection(self, obj):
        import json
        # Django экранирует readonly string; недоверенные branch/label не HTML.
        return json.dumps(obj._read_projection, ensure_ascii=False, indent=2)
