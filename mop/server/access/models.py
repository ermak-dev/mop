"""Связи стандартного Auth, provenance и credential generation, не новый User."""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Identifier(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True
        default_permissions = ()


class Principal(Identifier):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=8, choices=[('human', 'Human'), ('service', 'Service'), ('grant', 'Grant')])
    bound_node = models.ForeignKey('catalog.Node', null=True, on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta(Identifier.Meta):
        permissions = [('view_role', 'Can view role'), ('approve_role', 'Can approve role'),
                       ('issue_grant', 'Can issue grant'), ('revoke_grant', 'Can revoke grant')]
        constraints = [models.CheckConstraint(condition=Q(kind__in=('human', 'service', 'grant'), revision__gte=1), name='access_principal_valid')]


class IdentitySource(Identifier):
    name = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=8, choices=[('local', 'Local'), ('ldap', 'LDAP')])
    priority = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)
    revision = models.PositiveBigIntegerField(default=1)
    freshness_seconds = models.PositiveIntegerField(default=300)

    class Meta(Identifier.Meta):
        constraints = [models.CheckConstraint(condition=Q(kind__in=('local', 'ldap'), revision__gte=1,
            freshness_seconds__gte=1, freshness_seconds__lte=3600), name='access_source_valid')]


class LoginIdentity(Identifier):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    source = models.ForeignKey(IdentitySource, on_delete=models.PROTECT)
    external_subject = models.CharField(max_length=1024)
    authorized = models.BooleanField(default=True)
    validated_revision = models.PositiveBigIntegerField(default=0)
    validated_at = models.DateTimeField(null=True)

    class Meta(Identifier.Meta):
        constraints = [models.UniqueConstraint(fields=('source', 'external_subject'), name='access_source_subject_unique')]


class ProjectMembership(Identifier):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    source = models.ForeignKey(IdentitySource, on_delete=models.PROTECT)

    class Meta(Identifier.Meta):
        constraints = [models.UniqueConstraint(fields=('user', 'project', 'source'), name='access_project_source_unique')]


class AllProjectMembership(Identifier):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    source = models.ForeignKey(IdentitySource, on_delete=models.PROTECT)

    class Meta(Identifier.Meta):
        constraints = [models.UniqueConstraint(fields=('user', 'source'), name='access_all_source_unique')]


class ClientOwnership(Identifier):
    client = models.OneToOneField('catalog.ClientRegistration', on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class CredentialBinding(Identifier):
    principal = models.ForeignKey(Principal, on_delete=models.PROTECT)
    principal_revision = models.PositiveBigIntegerField()
    principal_kind = models.CharField(max_length=8)
    bound_node = models.ForeignKey('catalog.Node', null=True, on_delete=models.PROTECT)
    identity = models.ForeignKey(LoginIdentity, null=True, on_delete=models.PROTECT)
    identity_source = models.ForeignKey(IdentitySource, null=True, on_delete=models.PROTECT)
    token_user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE, related_name='+')
    token_digest = models.CharField(max_length=64, blank=True)
    session = models.OneToOneField('sessions.Session', null=True, on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Identifier.Meta):
        constraints = [models.CheckConstraint(condition=(Q(token_user__isnull=False, session__isnull=True) & ~Q(token_digest='')) |
            Q(token_user__isnull=True, session__isnull=False, token_digest=''), name='access_credential_exclusive')]


class DirectoryConfiguration(models.Model):
    source = models.OneToOneField(IdentitySource, primary_key=True, on_delete=models.PROTECT, related_name='directory')
    url = models.CharField(max_length=2048)
    bind_dn = models.CharField(max_length=1024)
    bind_secret_ref = models.CharField(max_length=256)
    user_base = models.CharField(max_length=1024)
    group_base = models.CharField(max_length=1024, blank=True)
    login_attribute = models.CharField(max_length=128, default='uid')
    nested_ad = models.BooleanField(default=False)
    starttls = models.BooleanField(default=False)
    ca_secret_ref = models.CharField(max_length=256, blank=True)

    class Meta:
        default_permissions = ()


class DirectoryGroupRule(Identifier):
    source = models.ForeignKey(IdentitySource, on_delete=models.PROTECT)
    group_dn = models.CharField(max_length=1024)
    project = models.ForeignKey('catalog.Project', null=True, on_delete=models.PROTECT)
    all_projects = models.BooleanField(default=False)
    permissions = models.ManyToManyField('auth.Permission', blank=True)

    class Meta(Identifier.Meta):
        constraints = [models.UniqueConstraint(fields=('source', 'group_dn'), name='access_directory_rule_unique')]


class DirectoryGroupOwnership(Identifier):
    source = models.ForeignKey(IdentitySource, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    group = models.OneToOneField('auth.Group', on_delete=models.PROTECT, related_name='directory_owner')

    class Meta(Identifier.Meta):
        constraints = [models.UniqueConstraint(fields=('source', 'user'), name='access_directory_group_unique')]


class AuthAttempt(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    window_started = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ()


class CredentialEvent(Identifier):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=32)
    created_at = models.DateTimeField(default=timezone.now)
