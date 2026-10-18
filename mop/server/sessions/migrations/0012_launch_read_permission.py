"""Каноническое право конечной адресной выдачи запуска (#643)."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('execution', '0011_launch_image_revision')]
    operations = [migrations.AlterModelOptions(name='assignmentattempt', options={
        'permissions': [('poll_work', 'Can poll enrolled machine work'),
            ('claim_work', 'Can claim enrolled machine work'),
            ('report_result', 'Can report enrolled machine result'),
            ('read_facts', 'Can read enrolled machine facts'),
            ('read_launch', 'Can read own admitted launch inputs'),
            ('reconcile_work', 'Can reconcile unknown machine work')]})]
