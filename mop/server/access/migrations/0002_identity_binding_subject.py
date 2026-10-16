"""Смена external subject/provider не оживляет прежний credential binding."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('access', '0001_initial')]
    operations = [migrations.AddField(model_name='credentialbinding', name='identity_digest',
        field=models.CharField(max_length=64, blank=True, default=''))]
