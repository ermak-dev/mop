"""Read-only предметные проекции; credentials/payloads не входят в allowlists."""
from django.contrib import admin, messages
from django.http import Http404, HttpResponseRedirect
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.views.decorators.debug import sensitive_post_parameters

from mop.common.credentials import Credential
from mop.server.access import models as a, services as access
from mop.server.catalog import models as c
from mop.server.operations import models as o
from mop.server.secrets import models as s
from mop.server.secrets.services import SecretStore
from mop.server.sessions import models as e
from . import adminservices as services
from .admincommon import ReadOnlyAdmin, actor, page, safe_view
from .adminforms import ReplaceSecretForm


@admin.register(o.Operation)
class OperationAdmin(ReadOnlyAdmin):
    action = 'operations.view_operation'
    fields = ('id', 'request_id', 'epoch', 'action_name', 'node_id', 'expected_revision',
              'state', 'revision', 'code', 'reason', 'not_after', 'created_at', 'completed_at', 'query_link')
    list_display = ('id', 'action_name', 'state', 'revision', 'created_at')
    node_path = 'node_id'

    @admin.display(description='Action')
    def action_name(self, obj):
        return obj.action

    @admin.display(description='Current API result')
    def query_link(self, obj):
        return format_html('<a href="{}">Query operation status</a>', '/api/v1/operations/' + str(obj.pk))

    def get_queryset(self, request):
        access.Access().authorize(actor(request), self.action)
        ids = []
        for pk in self.model.objects.values_list('pk', flat=True):
            try:
                services.operations_service().status(actor(request), pk)
            except access.NotVisible:
                continue
            ids.append(pk)
        return self.model.objects.filter(pk__in=ids)

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj:
            services.operations_service().status(actor(request), obj.pk)
        return obj


@admin.register(s.Secret)
class SecretAdmin(ReadOnlyAdmin):
    action = 'secrets.view_secret_metadata'
    project_path = 'project_id'
    fields = ('id', 'project_id', 'kind', 'name', 'purpose', 'revision', 'last_number', 'revoked', 'replacement')
    list_display = ('name', 'project_id', 'kind', 'purpose', 'revision', 'revoked')

    def get_queryset(self, request):
        # Installation credentials ещё не имеют named application authority
        # в #669. Project permission не превращается в право над Nomad/root.
        return super().get_queryset(request).filter(project__isnull=False)

    @admin.display(description='Protected replacement')
    def replacement(self, obj):
        if obj.purpose != 'project':
            return 'Named credential lifecycle unavailable'
        return format_html('<a href="{}">Replace project secret ({})</a>',
            reverse('admin:secrets_secret_replace', args=(obj.pk,)),
            'available' if services.credential_client() is not None else 'unavailable')

    def get_urls(self):
        return [path('<uuid:secret_id>/replace/', self.admin_site.admin_view(self.replace_view),
                     name='secrets_secret_replace')] + super().get_urls()

    @method_decorator(sensitive_post_parameters('value'))
    @safe_view
    def replace_view(self, request, secret_id):
        current = actor(request)
        row = self.get_queryset(request).filter(pk=secret_id).first()
        if row is None:
            raise Http404('resource not found')
        # Публичный metadata use case без crypto: provider здесь не вызывается.
        state = SecretStore(provider=None, authorizer=access.Access()).metadata(current, secret_id)
        access.Access().authorize(current, 'secrets.manage_project_secret', project_ids=(state.project_id,))
        client = services.credential_client()
        if client is None or row.purpose != 'project':
            return page(self, request, 'Capability unavailable',
                message='Protected project secret replacement is unavailable; no bytes were saved', status=503)
        form = ReplaceSecretForm(request.POST if request.method == 'POST' else None,
                                 initial={'expected_revision': state.revision})
        if request.method == 'POST' and form.is_valid():
            values = form.cleaned_data
            # Socket re-resolves реальную session и current authority после I/O.
            # Это не generic save и не proxy-key rotation; web не получает root.
            client.update(Credential('session', request.session.session_key), secret_id,
                values['value'].encode('utf-8'), expected_revision=values['expected_revision'])
            self.log_change(request, row, 'Protected project secret replaced')
            messages.success(request, 'Protected project secret replaced')
            return HttpResponseRedirect(reverse('admin:secrets_secret_change', args=(secret_id,)))
        return page(self, request, 'Replace project secret', form=form,
            message='Replacement uses the protected credential service. Proxy rotation is a separate unavailable operation.',
            status=400 if request.method == 'POST' else 200)


def register(model, fields, action, *, project=None, node=None, owner=None):
    """Конечная registration table; никакой автоматической публикации новых полей."""
    adapter = type(model.__name__ + 'ReadOnlyAdmin', (ReadOnlyAdmin,), {
        'fields': tuple(fields.split()), 'list_display': tuple(fields.split())[:4],
        'action': action, 'project_path': project, 'node_path': node, 'owner_path': owner,
        '__module__': __name__})
    admin.site.register(model, adapter)


register(c.ProjectPolicySnapshot, 'id project_id sequence digest source_commit confidential created_at',
         'catalog.view_projectpolicysnapshot', project='project_id')
register(c.ImageRevision, 'id policy_id source revision', 'catalog.view_projectpolicysnapshot', project='policy__project_id')
register(c.NodeImage, 'id node_id project_id revision available observed_at received_at',
         'catalog.view_node', project='project_id', node='node_id')
register(a.AgentDefinitionVersion, 'id project_id owner_id role digest driver receives_dispatch dispatch created_at',
         'access.view_role', project='project_id', owner='owner_id')
register(a.AgentDefinitionSeal, 'id definition_id created_at', 'access.view_role', project='definition__project_id', owner='definition__owner_id')
register(a.AgentDefinitionApproval, 'id definition_id human_id created_at', 'access.view_role',
         project='definition__project_id', owner='definition__owner_id')
register(a.DefinitionCapability, 'id definition_id scope verb', 'access.view_role', project='definition__project_id', owner='definition__owner_id')
register(a.DefinitionSpawn, 'id definition_id role', 'access.view_role', project='definition__project_id', owner='definition__owner_id')
register(a.AgentProfile, 'id project_id owner_id agent_id target_id definition_id revision created_at',
         'access.view_role', project='project_id', owner='owner_id')
register(a.Grant, 'id intent_id state revision retirement created_at', 'access.view_role',
         project='intent__project_id', owner='intent__owner_id')
register(a.GrantEvent, 'id grant_id action revision created_at', 'access.view_role',
         project='grant__intent__project_id', owner='grant__intent__owner_id')
register(s.CredentialRotation, 'id secret_id revision state generation created_at', 'secrets.view_secret_metadata', project='secret__project_id')
register(s.SecretAudit, 'id secret_id action created_at', 'secrets.view_secret_metadata', project='secret__project_id')
register(s.DeliveryReceipt, 'id recipient_id version_id generation outcome created_at',
         'secrets.view_secret_metadata', project='recipient__rotation__secret__project_id')
register(o.AuditEvent, 'id operation_id revision state code reason created_at', 'operations.view_audit', node='operation__node_id')
register(o.EffectReceipt, 'id step_id node_id source_revision applied received_at', 'operations.view_audit', node='node_id')
register(o.Step, 'id operation_id name started_at dispatched_at lease_until quiescent_at', 'operations.view_audit', node='operation__node_id')
register(o.OutboxEvent, 'id operation_id revision state code created_at published_at', 'operations.view_audit', node='operation__node_id')
register(e.Assignment, 'id project_id node_id owner_id revision generation role expires_at',
         'execution.reconcile_work', project='project_id', node='node_id', owner='owner_id')
register(e.AssignmentAttempt, 'id assignment_id action revision state created_at', 'execution.reconcile_work',
         project='assignment__project_id', node='assignment__node_id', owner='owner_id')
register(e.ResultReceipt, 'id attempt_id outcome evidence created_at', 'execution.reconcile_work',
         project='attempt__assignment__project_id', node='attempt__assignment__node_id', owner='attempt__owner_id')
