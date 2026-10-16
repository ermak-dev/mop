"""Immutable LDAP attribute задаётся явно, DN остаётся только адресом bind."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('access', '0002_identity_binding_subject')]
    operations = [migrations.AddField(model_name='directoryconfiguration', name='subject_attribute',
        field=models.CharField(max_length=16, default='entryUUID',
            choices=[('entryUUID', 'entryUUID'), ('objectGUID', 'objectGUID')]))]
