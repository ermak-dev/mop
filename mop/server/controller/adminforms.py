"""Формы отвечают только за синтаксис; authority/CAS проверяет application."""
from django import forms
from mop.server.catalog import models as c
from mop.server.catalog.definitions import DEFINITIONS


class RevisionForm(forms.ModelForm):
    expected_revision = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            self.fields['expected_revision'].initial = self.instance.revision
            self.fields['expected_revision'].required = True


class ProjectForm(RevisionForm):
    puppet_limit = forms.IntegerField(min_value=0, required=False, help_text='Leave empty for no limit')

    class Meta:
        model = c.Project
        fields = ('origin', 'puppet_limit')


class NodeForm(RevisionForm):
    projects = forms.ModelMultipleChoiceField(queryset=c.Project.objects.none(), required=False)

    class Meta:
        model = c.Node
        fields = ('name', 'driver', 'ssh_host', 'ssh_port', 'control_origin',
                  'lan_address', 'bootstrap_address', 'deployment_member')
        widgets = {'driver': forms.Select(choices=(('host', 'Host'), ('pve', 'PVE')))}


class SettingsForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, node=False, nodes=None, **kwargs):
        super().__init__(*args, **kwargs)
        if node:
            self.fields['node'] = forms.ModelChoiceField(queryset=nodes, label='Node')
        self.setting_keys = []
        for key, definition in DEFINITIONS.items():
            if node and not definition.node_override:
                continue
            if definition.kind == 'bool':
                field = forms.TypedChoiceField(choices=(('', 'Keep unchanged'), ('true', 'True'), ('false', 'False')),
                    coerce=lambda value: value == 'true', empty_value=None, required=False)
                if key in self.initial:
                    self.initial[key] = 'true' if self.initial[key] else 'false'
            elif definition.choices:
                field = forms.ChoiceField(choices=(('', 'Keep unchanged'), *[(v, v) for v in definition.choices]), required=False)
            elif definition.kind == 'int':
                field = forms.IntegerField(min_value=definition.minimum, max_value=definition.maximum, required=False)
            else:
                field = forms.CharField(max_length=definition.max_length, required=False)
            field.label = key
            self.fields[key] = field
            self.setting_keys.append(key)

    def clean(self):
        values = super().clean()
        extras = set(self.data) - set(self.fields) - {'csrfmiddlewaretoken', '_save'}
        if extras:
            raise forms.ValidationError('Unknown setting fields')
        batch = {}
        for key in self.setting_keys:
            value = values.get(key)
            if value is None or value == '':
                continue
            try:
                batch[key] = DEFINITIONS[key].validate(value)
            except ValueError as error:
                self.add_error(key, str(error))
        values['batch'] = batch
        return values


class NodeActionForm(forms.Form):
    epoch = forms.UUIDField(widget=forms.HiddenInput)
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    not_after = forms.DateTimeField(widget=forms.HiddenInput)


class ReplaceSecretForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    value = forms.CharField(max_length=65536, strip=False, widget=forms.PasswordInput(render_value=False))
