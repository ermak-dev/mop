"""Каталог в Admin: формы вызывают публичный Catalog, не ORM save/delete."""
from datetime import timedelta, timezone as utc
from uuid import UUID, uuid4

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from mop.server.access import services as access
from mop.server.catalog import models as c
from mop.server.operations.node import NodeEligibility
from . import adminservices as services
from .admincommon import GuardedAdmin, ReadOnlyAdmin, actor, allowed, page, safe_view
from .adminforms import ProjectForm, NodeForm, SettingsForm, NodeActionForm


def _adopt(obj, snapshot):
    for field in obj._meta.concrete_fields:
        setattr(obj, field.attname, getattr(snapshot, field.attname))
    obj._state.adding = False


@admin.register(c.Project)
class ProjectAdmin(GuardedAdmin):
    form = ProjectForm
    fields = ('name', 'origin', 'puppet_limit', 'expected_revision', 'revision')
    readonly_fields = ('name', 'revision')
    list_display = ('name', 'origin', 'puppet_limit', 'revision')

    def has_module_permission(self, request):
        return self.has_view_permission(request) or self.has_add_permission(request)

    def has_view_permission(self, request, obj=None):
        return allowed(request, 'catalog.view_project', projects=(obj.pk,) if obj else ())

    def has_add_permission(self, request):
        return allowed(request, 'catalog.add_project')

    def has_change_permission(self, request, obj=None):
        return allowed(request, 'catalog.change_project', projects=(obj.pk,) if obj else ())

    def get_queryset(self, request):
        ids = [row.id for row in services.catalog_api().list_projects(actor(request))]
        return super().get_queryset(request).filter(pk__in=ids)

    def save_model(self, request, obj, form, change):
        api, current = services.catalog_api(), actor(request)
        if change:
            result = api.configure_project(current, obj.pk, origin=obj.origin, limit=obj.puppet_limit,
                                           expected_revision=form.cleaned_data['expected_revision'])
        else:
            result = api.create_project(current, obj.origin, limit=obj.puppet_limit)
        _adopt(obj, result)


@admin.register(c.Node)
class NodeAdmin(GuardedAdmin):
    form = NodeForm
    fields = ('name', 'driver', 'ssh_host', 'ssh_port', 'control_origin', 'lan_address',
              'bootstrap_address', 'deployment_member', 'projects', 'expected_revision', 'revision',
              'desired_eligible', 'observed_eligible', 'observed_at', 'applied_revision', 'commands')
    readonly_fields = ('revision', 'desired_eligible', 'observed_eligible', 'observed_at', 'applied_revision', 'commands')
    list_display = ('name', 'driver', 'revision', 'desired_eligible', 'observed_eligible')

    def has_module_permission(self, request):
        return self.has_view_permission(request) or self.has_add_permission(request)

    def has_view_permission(self, request, obj=None):
        return allowed(request, 'catalog.view_node', nodes=(obj.pk,) if obj else ())

    def has_add_permission(self, request):
        return allowed(request, 'catalog.add_node')

    def has_change_permission(self, request, obj=None):
        return allowed(request, 'catalog.change_node', nodes=(obj.pk,) if obj else ())

    def get_queryset(self, request):
        ids = [row.id for row in services.catalog_api().list_nodes(actor(request))]
        return super().get_queryset(request).filter(pk__in=ids)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if 'projects' in form.base_fields:
            ids = [row.id for row in services.catalog_api().list_projects(actor(request))]
            form.base_fields['projects'].queryset = c.Project.objects.filter(pk__in=ids)
            if obj:
                form.base_fields['projects'].initial = c.NodeProject.objects.filter(node=obj).values_list('project_id', flat=True)
        return form

    def save_model(self, request, obj, form, change):
        api, current = services.catalog_api(), actor(request)
        fields = ('name', 'driver', 'ssh_host', 'ssh_port', 'control_origin', 'lan_address', 'bootstrap_address')
        result = api.configure_pool_node(current, **{name: form.cleaned_data[name] for name in fields},
            deployment_member=form.cleaned_data['deployment_member'],
            project_ids=tuple(row.pk for row in form.cleaned_data['projects']),
            **({'node_id': obj.pk, 'expected_revision': form.cleaned_data['expected_revision']} if change else {}))
        _adopt(obj, result)

    def get_urls(self):
        return [path('<uuid:node_id>/actions/<str:action>/', self.admin_site.admin_view(self.command_view),
                     name='catalog_node_command')] + super().get_urls()

    @admin.display(description='Physical actions')
    def commands(self, obj):
        if obj._state.adding:
            return 'Save the catalogue entry first'
        return format_html_join(' · ', '<a href="{}">{} ({})</a>', ((
            reverse('admin:catalog_node_command', args=(obj.pk, action)), action,
            'available' if action in services.node_actions() else 'unavailable') for action in ('drain', 'enable')))

    @safe_view
    def command_view(self, request, node_id, action):
        if action not in ('drain', 'enable'):
            raise Http404('action is not registered')
        current = actor(request)
        access.Access().authorize(current, 'operations.view_operation', node_ids=(node_id,))
        target = None
        if request.method != 'POST':
            # GET оформляет новую команду; replay POST — query authority,
            # выбранная самим Operations.submit, а не вторым Admin policy.
            target = services.catalog_api().get_node(current, node_id)
            access.Access().authorize(current, 'catalog.' + action + '_node', node_ids=(node_id,))
        if action not in services.node_actions():
            return page(self, request, 'Capability unavailable',
                message='Physical ' + action + ' handler is unavailable; no operation was submitted', status=503)
        initial = (dict(expected_revision=target.revision, epoch=services.installation_snapshot()['epoch'],
            request_id=uuid4(), not_after=timezone.now() + timedelta(minutes=2)) if target else None)
        form = NodeActionForm(request.POST if request.method == 'POST' else None, initial=initial)
        if request.method == 'POST' and form.is_valid():
            values = form.cleaned_data
            command = NodeEligibility(values['epoch'], values['request_id'], node_id, values['expected_revision'],
                values['not_after'].astimezone(utc.utc), action == 'enable')
            result = services.operations_service().submit(current, command)
            messages.success(request, 'Operation status: ' + result['state'])
            return HttpResponseRedirect(reverse('admin:operations_operation_change', args=(result['id'],)))
        return page(self, request, 'Submit ' + action + ' operation', form=form,
            message='Submission records intent only. Accepted does not mean succeeded.',
            status=400 if request.method == 'POST' else 200)


class SettingsAdmin(ReadOnlyAdmin):
    fields = ('id', 'key', 'typed_value', 'revision')
    list_display = ('key', 'typed_value', 'revision')
    change_list_template = 'admin/mop/settings_list.html'
    node = False

    @admin.display(description='Desired value')
    def typed_value(self, obj):
        return getattr(obj, obj.value_kind + '_value')

    def get_urls(self):
        return [path('configure/', self.admin_site.admin_view(self.configure_view),
                     name=self.model._meta.app_label + '_' + self.model._meta.model_name + '_configure')] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), 'configure_url': reverse(
            'admin:' + self.model._meta.app_label + '_' + self.model._meta.model_name + '_configure')})

    @safe_view
    def configure_view(self, request):
        current, api = actor(request), services.catalog_api()
        nodes, target = None, None
        if self.node:
            access.Access().authorize(current, 'catalog.view_nodesetting')
            access.Access().authorize(current, 'catalog.change_nodesetting')
            ids = [row.id for row in api.list_nodes(current)]
            nodes = c.Node.objects.filter(pk__in=ids)
            raw = request.POST.get('node') if request.method == 'POST' else request.GET.get('node')
            if raw:
                try:
                    target = UUID(raw)
                except (ValueError, TypeError):
                    raise Http404('resource not found') from None
                if not nodes.filter(pk=target).exists():
                    raise Http404('resource not found')
            state = api.get_node_settings(current, target) if target else None
        else:
            access.Access().authorize(current, 'catalog.change_installationsetting')
            state = api.get_installation_settings(current)
        initial = {'expected_revision': state.revision if state else 1, **(dict(state.values) if state else {})}
        if target:
            initial['node'] = target
        form = SettingsForm(request.POST if request.method == 'POST' else None,
            node=self.node, nodes=nodes, initial=initial)
        if request.method == 'POST' and form.is_valid():
            values = form.cleaned_data
            if self.node:
                api.set_node_settings(current, values['node'].pk, values['batch'], expected_revision=values['expected_revision'])
            else:
                api.set_installation_settings(current, values['batch'], expected_revision=values['expected_revision'])
            messages.success(request, 'Desired settings saved. No deployment was started.')
            return HttpResponseRedirect(reverse('admin:' + self.model._meta.app_label + '_' + self.model._meta.model_name + '_changelist'))
        return page(self, request, 'Desired settings', form=form,
            message='Only typed desired configuration is saved. Application requires a separate operation.',
            status=400 if request.method == 'POST' else 200)


@admin.register(c.InstallationSetting)
class InstallationSettingsAdmin(SettingsAdmin):
    action = 'catalog.view_installationsetting'


@admin.register(c.NodeSetting)
class NodeSettingsAdmin(SettingsAdmin):
    action = 'catalog.view_nodesetting'
    node_path = 'node_id'
    node = True
