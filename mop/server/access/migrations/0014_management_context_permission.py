"""#742: отдельное право конечного post-activation handoff, не secret read."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('access', '0013_create_grant_activation')]

    operations = [migrations.AlterModelOptions(name='grant', options={
        'default_permissions': (),
        'permissions': [('view_role', 'Can view role'), ('approve_role', 'Can approve role'),
            ('issue_grant', 'Can issue grant'), ('revoke_grant', 'Can revoke grant'),
            ('activate_grant', 'Can activate claimed create grant'),
            ('read_management_context', 'Can read current addressed agent management context')],
    })]
