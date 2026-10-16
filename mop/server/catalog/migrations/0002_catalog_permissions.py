"""Назначаемые custom permissions создаёт стандартный Django post_migrate."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('catalog', '0001_initial')]

    operations = [
        migrations.AlterModelOptions(
            name='project',
            options={'abstract': False, 'permissions': (
                ('configure_project', 'Can configure project'),
                ('set_project_limit', 'Can set project limit'),
            )},
        ),
        migrations.AlterModelOptions(
            name='node',
            options={'abstract': False, 'permissions': (
                ('configure_node', 'Can configure node'),
                ('record_node_application', 'Can record node application'),
                ('observe_node', 'Can observe node'),
            )},
        ),
        migrations.AlterModelOptions(
            name='installationconfiguration',
            options={'abstract': False, 'permissions': (
                ('record_settings_application', 'Can record settings application'),
            )},
        ),
        migrations.AlterModelOptions(
            name='projectpolicysnapshot',
            options={'abstract': False, 'permissions': (
                ('pin_project_policy', 'Can pin project policy'),
            )},
        ),
    ]
