"""Action permissions CONTROL, без дубликатов стандартных view_project/node."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('catalog', '0001_initial')]
    operations = [
        migrations.AlterModelOptions(name='project', options={'permissions': [
            ('view_pool', 'Can view pool'), ('configure_project', 'Can configure project'),
            ('set_project_limit', 'Can set project limit'), ('build_project', 'Can build project'),
            ('recover_build', 'Can recover build')]}),
        migrations.AlterModelOptions(name='node', options={'permissions': [
            ('configure_node', 'Can configure node'), ('drain_node', 'Can drain node'),
            ('enable_node', 'Can enable node'), ('forget_node', 'Can forget node'),
            ('record_node_application', 'Can record node application'), ('observe_node', 'Can observe node')]}),
        migrations.AlterModelOptions(name='installationconfiguration', options={
            'permissions': (('record_settings_application', 'Can record settings application'),)}),
        migrations.AlterModelOptions(name='projectpolicysnapshot', options={
            'permissions': [('pin_project_policy', 'Can pin project policy')]}),
    ]
