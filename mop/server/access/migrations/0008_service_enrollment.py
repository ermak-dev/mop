"""#687: минимальное отношение технического enrollment service Actor.

Схема свежей установки; старых строк нет — эпик #623 не раскатан.
Use case и узел (для узловых сервисов) — проверяемые сервером факты
контекста; прав отношение не даёт, операционные права — Django RBAC (#685).
"""
import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion as deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0007_actor_credentials'),
    ]

    operations = [
        migrations.CreateModel(name='ServiceEnrollment', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('use_case', models.CharField(max_length=32, choices=[
                ('node-executor', 'node-executor'), ('controller-worker', 'controller-worker'),
                ('credential-runtime', 'credential-runtime'), ('image-builder', 'image-builder')])),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('actor', models.OneToOneField(on_delete=deletion.PROTECT, related_name='service_enrollment',
                to=settings.AUTH_USER_MODEL)),
            ('node', models.ForeignKey(null=True, on_delete=deletion.PROTECT, to='catalog.node')),
        ], options={'default_permissions': (), 'constraints': [
            models.CheckConstraint(condition=models.Q(use_case__in=(
                'node-executor', 'controller-worker', 'credential-runtime', 'image-builder')),
                name='access_enrollment_use_case'),
            models.CheckConstraint(condition=(models.Q(use_case__in=('node-executor',), node__isnull=False)
                | (models.Q(~models.Q(use_case__in=('node-executor',))) & models.Q(node__isnull=True))),
                name='access_enrollment_node_context'),
        ]}),
    ]

