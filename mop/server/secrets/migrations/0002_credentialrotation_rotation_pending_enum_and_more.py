# Сгенерировано Django 5.2.18; категории и пары причин lifecycle #637.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('secrets', '0001_initial'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('pending__isnull', True), ('pending__in', ('deliver', 'retire_old', 'restore_old', 'revoke_new')), _connector='OR'), name='rotation_pending_enum'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('retryable__isnull', True), ('retryable__in', ('deliver', 'retire_old', 'restore_old', 'revoke_new')), _connector='OR'), name='rotation_retryable_enum'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('original_failure__isnull', True), ('original_failure__in', ('deliver', 'retire_old', 'restore_old', 'revoke_new')), _connector='OR'), name='rotation_original_failure_enum'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('compensation_failure__isnull', True), ('compensation_failure__in', ('deliver', 'retire_old', 'restore_old', 'revoke_new')), _connector='OR'), name='rotation_compensation_failure_enum'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('original_reason__isnull', True), ('original_reason__in', ('refused', 'precondition_failed', 'unavailable_before_effect', 'partially_applied')), _connector='OR'), name='rotation_original_reason_enum'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('compensation_reason__isnull', True), ('compensation_reason__in', ('refused', 'precondition_failed', 'unavailable_before_effect', 'partially_applied')), _connector='OR'), name='rotation_compensation_reason_enum'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('original_failure__isnull', True), ('original_reason__isnull', True)), models.Q(('original_failure__isnull', False), ('original_reason__isnull', False)), _connector='OR'), name='rotation_original_reason_pair'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('compensation_failure__isnull', True), ('compensation_reason__isnull', True)), models.Q(('compensation_failure__isnull', False), ('compensation_reason__isnull', False)), _connector='OR'), name='rotation_compensation_reason_pair'),
        ),
    ]
