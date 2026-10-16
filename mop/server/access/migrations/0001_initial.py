"""Пустая новая Access schema; никакого импорта legacy users/hashes/cookies."""
import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion as deletion
import django.utils.timezone


def identifier():
    return [('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False))]


def fk(target, **kwargs):
    return models.ForeignKey(to=target, on_delete=deletion.PROTECT, **kwargs)


def options(*constraints, **kwargs):
    return {'abstract': False, 'default_permissions': (), 'constraints': list(constraints), **kwargs}


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('auth', '0012_alter_user_first_name_max_length'), ('sessions', '0001_initial'),
        ('catalog', '0002_action_permissions')]
    operations = [
        migrations.CreateModel(name='IdentitySource', fields=identifier() + [
            ('name', models.CharField(max_length=64, unique=True)),
            ('kind', models.CharField(max_length=8, choices=[('local', 'Local'), ('ldap', 'LDAP')])),
            ('priority', models.IntegerField(default=0)), ('enabled', models.BooleanField(default=True)),
            ('revision', models.PositiveBigIntegerField(default=1)),
            ('freshness_seconds', models.PositiveIntegerField(default=300)),
        ], options=options(models.CheckConstraint(condition=models.Q(kind__in=('local', 'ldap'), revision__gte=1,
            freshness_seconds__gte=1, freshness_seconds__lte=3600), name='access_source_valid'))),
        migrations.CreateModel(name='Principal', fields=identifier() + [
            ('user', models.OneToOneField(to=settings.AUTH_USER_MODEL, on_delete=deletion.PROTECT)),
            ('kind', models.CharField(max_length=8, choices=[('human', 'Human'), ('service', 'Service'), ('grant', 'Grant')])),
            ('bound_node', fk('catalog.node', null=True)), ('revision', models.PositiveBigIntegerField(default=1)),
        ], options=options(models.CheckConstraint(condition=models.Q(kind__in=('human', 'service', 'grant'), revision__gte=1), name='access_principal_valid'),
            permissions=[('view_role', 'Can view role'), ('approve_role', 'Can approve role'),
                ('issue_grant', 'Can issue grant'), ('revoke_grant', 'Can revoke grant')])),
        migrations.CreateModel(name='LoginIdentity', fields=identifier() + [
            ('user', models.OneToOneField(to=settings.AUTH_USER_MODEL, on_delete=deletion.PROTECT)),
            ('source', fk('access.identitysource')), ('external_subject', models.CharField(max_length=1024)),
            ('authorized', models.BooleanField(default=True)), ('validated_revision', models.PositiveBigIntegerField(default=0)),
            ('validated_at', models.DateTimeField(null=True)),
        ], options=options(models.UniqueConstraint(fields=('source', 'external_subject'), name='access_source_subject_unique'))),
        migrations.CreateModel(name='ProjectMembership', fields=identifier() + [
            ('user', fk(settings.AUTH_USER_MODEL)), ('project', fk('catalog.project')), ('source', fk('access.identitysource')),
        ], options=options(models.UniqueConstraint(fields=('user', 'project', 'source'), name='access_project_source_unique'))),
        migrations.CreateModel(name='AllProjectMembership', fields=identifier() + [
            ('user', fk(settings.AUTH_USER_MODEL)), ('source', fk('access.identitysource')),
        ], options=options(models.UniqueConstraint(fields=('user', 'source'), name='access_all_source_unique'))),
        migrations.CreateModel(name='ClientOwnership', fields=identifier() + [
            ('client', models.OneToOneField(to='catalog.clientregistration', on_delete=deletion.PROTECT)),
            ('user', fk(settings.AUTH_USER_MODEL)),
        ], options=options()),
        migrations.CreateModel(name='CredentialBinding', fields=identifier() + [
            ('principal', fk('access.principal')), ('principal_revision', models.PositiveBigIntegerField()),
            ('principal_kind', models.CharField(max_length=8)), ('bound_node', fk('catalog.node', null=True)),
            ('identity', fk('access.loginidentity', null=True)), ('identity_source', fk('access.identitysource', null=True)),
            ('token_user', models.OneToOneField(to=settings.AUTH_USER_MODEL, null=True, on_delete=deletion.CASCADE, related_name='+')),
            ('token_digest', models.CharField(max_length=64, blank=True)),
            ('session', models.OneToOneField(to='sessions.session', null=True, on_delete=deletion.CASCADE)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options(models.CheckConstraint(condition=(models.Q(token_user__isnull=False, session__isnull=True) & ~models.Q(token_digest='')) |
            models.Q(token_user__isnull=True, session__isnull=False, token_digest=''), name='access_credential_exclusive'))),
        migrations.CreateModel(name='DirectoryConfiguration', fields=[
            ('source', models.OneToOneField(to='access.identitysource', primary_key=True, serialize=False, on_delete=deletion.PROTECT, related_name='directory')),
            ('url', models.CharField(max_length=2048)), ('bind_dn', models.CharField(max_length=1024)),
            ('bind_secret_ref', models.CharField(max_length=256)), ('user_base', models.CharField(max_length=1024)),
            ('group_base', models.CharField(max_length=1024, blank=True)),
            ('login_attribute', models.CharField(max_length=128, default='uid')),
            ('nested_ad', models.BooleanField(default=False)), ('starttls', models.BooleanField(default=False)),
            ('ca_secret_ref', models.CharField(max_length=256, blank=True)),
        ], options={'default_permissions': ()}),
        migrations.CreateModel(name='DirectoryGroupRule', fields=identifier() + [
            ('source', fk('access.identitysource')), ('group_dn', models.CharField(max_length=1024)),
            ('project', fk('catalog.project', null=True)), ('all_projects', models.BooleanField(default=False)),
            ('permissions', models.ManyToManyField(to='auth.permission', blank=True)),
        ], options=options(models.UniqueConstraint(fields=('source', 'group_dn'), name='access_directory_rule_unique'))),
        migrations.CreateModel(name='DirectoryGroupOwnership', fields=identifier() + [
            ('source', fk('access.identitysource')), ('user', fk(settings.AUTH_USER_MODEL)),
            ('group', models.OneToOneField(to='auth.group', on_delete=deletion.PROTECT, related_name='directory_owner')),
        ], options=options(models.UniqueConstraint(fields=('source', 'user'), name='access_directory_group_unique'))),
        migrations.CreateModel(name='AuthAttempt', fields=[
            ('key', models.CharField(max_length=64, primary_key=True, serialize=False)),
            ('count', models.PositiveIntegerField(default=0)),
            ('window_started', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': ()}),
        migrations.CreateModel(name='CredentialEvent', fields=identifier() + [
            ('user', fk(settings.AUTH_USER_MODEL)), ('action', models.CharField(max_length=32)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options()),
    ]
