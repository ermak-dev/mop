"""Общие HTML/permission adapters: никаких доменных переходов."""
from functools import wraps

from django.contrib import admin, messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.http import Http404
from django.template.response import TemplateResponse

from mop.common.credentials import CredentialError
from mop.server.access import services as access
from mop.server.catalog.services import Conflict, ProtectionUnavailable
from mop.server.operations.service import Refused
from mop.server.secrets.services import StorageUnavailable


def actor(request):
    return access.session_context(request.session.session_key)


def allowed(request, action, *, projects=(), nodes=(), clients=()):
    try:
        access.Access().authorize(actor(request), action, project_ids=projects, node_ids=nodes, client_ids=clients)
        return True
    except access.Denied:
        return False


def page(adapter, request, title, *, form=None, message='', status=200, links=()):
    response = TemplateResponse(request, 'admin/mop/form.html', {
        **adapter.admin_site.each_context(request), 'title': title,
        'opts': adapter.model._meta, 'form': form, 'message': message, 'links': links})
    response.status_code = status
    response['Cache-Control'] = 'no-store'
    return response


def safe_view(method):
    @wraps(method)
    def wrapped(self, request, *args, **kwargs):
        try:
            return method(self, request, *args, **kwargs)
        except access.NotVisible:
            raise Http404('resource not found') from None
        except access.Denied as error:
            raise PermissionDenied(str(error)) from None
        except ObjectDoesNotExist:
            raise Http404('resource not found') from None
        except Conflict as error:
            return page(self, request, 'Conflict', message=str(error), status=409)
        except Refused as error:
            return page(self, request, 'Operation refused', message=str(error), status=error.status)
        except (ProtectionUnavailable, CredentialError, StorageUnavailable) as error:
            return page(self, request, 'Capability unavailable', message=str(error), status=503)
        except (ValidationError, ValueError) as error:
            # Catalog/finite command validators возвращают безопасные фразы.
            # Повторная санитизация уничтожила бы причину доменного отказа.
            return page(self, request, 'Invalid command', message=str(error), status=400)
    return wrapped


class IdentifierFreeSuccess:
    """Очередь flash не становится вторым, нескоупленным object query."""
    def message_user(self, request, message, level=messages.INFO, extra_tags='', fail_silently=False):
        if level == messages.SUCCESS:
            message = 'Application command completed'
        return super().message_user(request, message, level, extra_tags, fail_silently)


class GuardedAdmin(IdentifierFreeSuccess, admin.ModelAdmin):
    """Admin не предоставляет неявных mutation paths."""
    actions = None
    inlines = ()
    list_editable = ()
    save_as = False
    show_full_result_count = False

    def get_actions(self, request):
        return {}

    def get_inline_instances(self, request, obj=None):
        return []

    @staticmethod
    def _bypass(request):
        return request.method == 'POST' and ('action' in request.POST or '_saveasnew' in request.POST or
            any(key.endswith(('-TOTAL_FORMS', '-INITIAL_FORMS')) for key in request.POST))

    @safe_view
    def changelist_view(self, request, extra_context=None):
        if self._bypass(request):
            raise PermissionDenied('bulk actions, save-as-new and formsets are disabled')
        return super().changelist_view(request, extra_context)

    @safe_view
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if self._bypass(request):
            raise PermissionDenied('bulk actions, save-as-new and formsets are disabled')
        return super().changeform_view(request, object_id, form_url, extra_context)

    def delete_model(self, request, obj):
        raise PermissionDenied('use the named retirement operation')

    def delete_queryset(self, request, queryset):
        raise PermissionDenied('bulk deletion is disabled')

    def has_delete_permission(self, request, obj=None):
        return False

    def save_formset(self, request, form, formset, change):
        raise PermissionDenied('inline mutation is disabled')

    def save_related(self, request, form, formsets, change):
        if formsets:
            raise PermissionDenied('inline mutation is disabled')
        # Все non-model relation fields передаются явному application command.

    def log_addition(self, request, obj, message):
        return super().log_addition(request, obj, 'Catalogue command completed')

    def log_change(self, request, obj, message):
        return super().log_change(request, obj, 'Application command completed')


class ReadOnlyAdmin(GuardedAdmin):
    """Явный scalar allowlist, live Access scope до list/count/detail/history."""
    action = ''
    project_path = None
    node_path = None
    owner_path = None

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_view_permission(self, request, obj=None):
        refs = {}
        if obj is not None:
            for key, path in (('projects', self.project_path), ('nodes', self.node_path)):
                if path:
                    value = self.model.objects.filter(pk=obj.pk).values_list(path, flat=True).get()
                    refs[key] = (value,) if value is not None else ()
        return allowed(request, self.action, **refs)

    def get_queryset(self, request):
        access.Access().authorize(actor(request), self.action)
        rows = super().get_queryset(request)
        if self.owner_path:
            rows = rows.filter(**{self.owner_path: actor(request).user_id})
        columns = ['pk', *([self.project_path] if self.project_path else []),
                   *([self.node_path] if self.node_path else [])]
        ids = []
        for values in rows.values_list(*columns):
            refs, index = {}, 1
            for key, path in (('project_ids', self.project_path), ('node_ids', self.node_path)):
                if path:
                    refs[key] = (values[index],) if values[index] is not None else ()
                    index += 1
            try:
                access.Access().authorize(actor(request), self.action, **refs)
            except access.NotVisible:
                continue
            ids.append(values[0])
        access.Access().authorize(actor(request), self.action)
        return rows.filter(pk__in=ids)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        raise PermissionDenied('evidence is read-only')

    def get_readonly_fields(self, request, obj=None):
        return self.fields
