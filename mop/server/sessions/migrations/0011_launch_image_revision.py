"""#708: пин образа хранит immutable reference, а не только digest его id."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('execution', '0010_create_launch')]
    operations = [
        migrations.AddField(model_name='incarnationlaunch', name='image_revision',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                    to='catalog.imagerevision')),
    ]
