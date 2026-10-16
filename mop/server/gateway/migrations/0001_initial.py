from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(name='KeyState', fields=[
            ('fingerprint', models.CharField(primary_key=True, max_length=64, editable=False, serialize=False)),
            ('state', models.CharField(max_length=16, default='unproven', editable=False)),
            ('version_id', models.UUIDField(editable=False)),
            ('revision', models.PositiveBigIntegerField(default=1, editable=False)),
        ], options={'default_permissions': (), 'constraints': [
            models.CheckConstraint(condition=models.Q(state__in=('unproven', 'proven', 'rejected')), name='gateway_key_state')]}),
        migrations.CreateModel(name='Circuit', fields=[
            ('id', models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1, editable=False)),
            ('claim_id', models.UUIDField(null=True, editable=False)),
            ('claim_fingerprint', models.CharField(max_length=64, blank=True, editable=False)),
            ('version_id', models.UUIDField(null=True, editable=False)),
            ('deadline', models.DateTimeField(null=True, editable=False)),
            ('cooldown_until', models.DateTimeField(null=True, editable=False)),
            ('state', models.CharField(max_length=16, default='idle', editable=False)),
            ('write', models.BooleanField(default=False, editable=False)),
        ], options={'default_permissions': (), 'permissions': (('manage_proxy', 'Manage proxy management gateway'),),
            'constraints': [models.CheckConstraint(condition=models.Q(id=1), name='gateway_single_circuit'),
                models.CheckConstraint(condition=models.Q(state__in=('idle', 'running', 'unknown')), name='gateway_circuit_state'),
                models.CheckConstraint(condition=(models.Q(state='idle', claim_id__isnull=True, deadline__isnull=True)
                    | models.Q(state__in=('running', 'unknown'), claim_id__isnull=False, deadline__isnull=False)),
                    name='gateway_claim_lifecycle')]}),
    ]
