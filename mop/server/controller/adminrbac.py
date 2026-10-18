"""Тонкие формы approve/revoke recipe; не generic nonhuman Actor CRUD (#747)."""
from django import forms
from django.contrib import admin, messages
from django.contrib.auth.models import Group
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html

from mop.server.access.models import AgentRBACProfile
from mop.server.access.rbac import AgentRBACProfiles
from mop.server.access.services import Denied
from .admincommon import ReadOnlyAdmin, actor, page, safe_view


class ApproveProfileForm(forms.Form):
    definition_id = forms.UUIDField(label='Exact agent definition ID')
    group_ids = forms.ModelMultipleChoiceField(queryset=Group.objects.none(), required=False,
        label='Ordinary Django Groups (empty is an explicit passive profile)')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['group_ids'].queryset = Group.objects.filter(directory_rule__isnull=True).order_by('pk')


@admin.register(AgentRBACProfile)
class AgentRBACProfileAdmin(ReadOnlyAdmin):
    fields = ('id', 'definition_id', 'group_ids', 'digest', 'approved_by_id', 'created_at', 'commands')
    list_display = ('id', 'definition_id', 'approved_by_id', 'created_at')
    action = 'auth.change_user'

    def has_view_permission(self, request, obj=None):
        try:
            AgentRBACProfiles.authorize(actor(request))
            return True
        except Denied:
            return False

    def get_queryset(self, request):
        AgentRBACProfiles.authorize(actor(request))
        return self.model.objects.all()

    def get_urls(self):
        return [path('approve/', self.admin_site.admin_view(self.approve_view), name='access_agentrbacprofile_approve'),
            path('<uuid:profile_id>/revoke/', self.admin_site.admin_view(self.revoke_view),
                name='access_agentrbacprofile_revoke')] + super().get_urls()

    @safe_view
    def changelist_view(self, request, extra_context=None):
        AgentRBACProfiles.authorize(actor(request))
        return super().changelist_view(request, {**(extra_context or {}), 'subtitle': format_html(
            '<a href="{}">Approve an explicit profile</a>', reverse('admin:access_agentrbacprofile_approve'))})

    @admin.display(description='Named commands')
    def commands(self, obj):
        return format_html('<a href="{}">Revoke recipe for new agents and pending initial activation</a>',
            reverse('admin:access_agentrbacprofile_revoke', args=(obj.pk,)))

    @safe_view
    def approve_view(self, request):
        current = actor(request)
        AgentRBACProfiles.authorize(current)
        if self._bypass(request):
            raise Denied('bulk actions, save-as-new and formsets are disabled')
        form = ApproveProfileForm(request.POST if request.method == 'POST' else None)
        if request.method == 'POST' and form.is_valid():
            row = AgentRBACProfiles().approve(current, form.cleaned_data['definition_id'],
                list(form.cleaned_data['group_ids'].values_list('pk', flat=True)))
            self.log_addition(request, row, 'Agent RBAC profile approved')
            messages.success(request, 'Agent RBAC profile approved')
            return HttpResponseRedirect(reverse('admin:access_agentrbacprofile_change', args=(row.pk,)))
        return page(self, request, 'Approve agent RBAC profile', form=form,
            message='Approval is a one-time Group recipe, not a live membership template.',
            status=400 if request.method == 'POST' else 200)

    @safe_view
    def revoke_view(self, request, profile_id):
        current = actor(request)
        AgentRBACProfiles.authorize(current)
        form = forms.Form(request.POST if request.method == 'POST' else None)
        if request.method == 'POST' and form.is_valid():
            if self._bypass(request):
                raise Denied('bulk actions, save-as-new and formsets are disabled')
            row = AgentRBACProfiles().revoke(current, profile_id)
            self.log_change(request, row.profile, 'Agent RBAC recipe revoked')
            messages.success(request, 'Agent RBAC recipe revoked; existing active memberships are unchanged')
            return HttpResponseRedirect(reverse('admin:access_agentrbacprofile_change', args=(profile_id,)))
        return page(self, request, 'Revoke agent RBAC recipe', form=form,
            message='Blocks new provisioning and pending initial activation. Does not remove existing memberships.')
