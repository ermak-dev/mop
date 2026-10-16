"""Отдельный leaf для #628; соседние модели Sessions не переписываются."""
import uuid
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def permissions(apps, schema_editor):
    ContentType=apps.get_model('contenttypes','ContentType')
    Permission=apps.get_model('auth','Permission')
    kind,_=ContentType.objects.get_or_create(app_label='sessions',model='terminalcontrol')
    for code,label in [('read_terminal','Can read private terminal'),('control_terminal','Can control private terminal')]:
        Permission.objects.get_or_create(content_type=kind,codename=code,defaults={'name':label})


class Migration(migrations.Migration):
    dependencies = [('execution','0001_initial'), ('auth','0012_alter_user_first_name_max_length')]
    operations = [
        migrations.CreateModel(name='TerminalControl',fields=[
            ('target',models.OneToOneField(primary_key=True,serialize=False,on_delete=django.db.models.deletion.PROTECT,to='execution.assignment')),
            ('generation',models.PositiveBigIntegerField(default=0))]),
        migrations.CreateModel(name='TerminalAttachment',fields=[
            ('id',models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False,serialize=False)),
            ('credential_id',models.UUIDField()),('principal_id',models.UUIDField()),
            ('mode',models.CharField(max_length=2)),('state',models.CharField(max_length=8,default='reserved')),
            ('generation',models.PositiveBigIntegerField()),('gateway_id',models.UUIDField(null=True)),
            ('connection_id',models.UUIDField(null=True)),('sequence',models.PositiveBigIntegerField(default=0)),
            ('cols',models.PositiveIntegerField()),('rows',models.PositiveIntegerField()),
            ('expires_at',models.DateTimeField()),('created_at',models.DateTimeField(default=django.utils.timezone.now)),
            ('target',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='execution.assignment')),
            ('incarnation',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='execution.executionincarnation')),
        ],options={'constraints':[
            models.CheckConstraint(condition=models.Q(mode__in=('ro','rw')),name='terminal_known_mode'),
            models.CheckConstraint(condition=models.Q(state__in=('reserved','active','closed','unknown')),name='terminal_known_state'),
            models.UniqueConstraint(fields=('target',),condition=models.Q(mode='rw',state__in=('reserved','active')),name='terminal_one_live_writer'),
            models.CheckConstraint(condition=models.Q(cols__gte=20,cols__lte=500,rows__gte=5,rows__lte=200),name='terminal_size_bounds'),
        ]}),
        migrations.RunPython(permissions,migrations.RunPython.noop),
    ]
