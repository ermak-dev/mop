"""#643: явное RBAC-право и узловой enrollment worker активации create."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('access', '0012_delete_principal')]

    operations = [
        migrations.AlterModelOptions(name='grant', options={
            'default_permissions': (),
            'permissions': [('view_role', 'Can view role'), ('approve_role', 'Can approve role'),
                ('issue_grant', 'Can issue grant'), ('revoke_grant', 'Can revoke grant'),
                ('activate_grant', 'Can activate claimed create grant')],
        }),
        migrations.RemoveConstraint(model_name='serviceenrollment', name='access_enrollment_use_case'),
        migrations.RemoveConstraint(model_name='serviceenrollment', name='access_enrollment_node_context'),
        migrations.AlterField(model_name='serviceenrollment', name='use_case', field=models.CharField(
            max_length=32, choices=[(x, x) for x in ('node-executor', 'controller-worker',
                'credential-runtime', 'image-builder', 'grant-activator')])),
        migrations.AddConstraint(model_name='serviceenrollment', constraint=models.CheckConstraint(
            condition=models.Q(use_case__in=('node-executor', 'controller-worker', 'credential-runtime',
                'image-builder', 'grant-activator')), name='access_enrollment_use_case')),
        migrations.AddConstraint(model_name='serviceenrollment', constraint=models.CheckConstraint(
            condition=(models.Q(use_case__in=('node-executor', 'grant-activator'), node__isnull=False)
                | (models.Q(~models.Q(use_case__in=('node-executor', 'grant-activator')))
                    & models.Q(node__isnull=True))), name='access_enrollment_node_context')),
    ]
