"""Связи стандартного Auth, provenance и credential generation, не новый User."""
import uuid
from django.conf import settings
from django.db import models, router, transaction
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
        # Владение action-правами переехало на живую модель Grant (#685);
        # Principal не регистрирует прав, строки не создаются (#684).
        # Удаление таблицы подготовлено в #645 после потребителей #688.
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
    """Только proven metadata, которых нет в стандартных Django credentials.

    Атрибуты Actor (kind) не дублируются: credential не вторая identity (#684).
    """
    bound_node = models.ForeignKey('catalog.Node', null=True, on_delete=models.PROTECT)
    identity = models.ForeignKey(LoginIdentity, null=True, on_delete=models.PROTECT)
    identity_source = models.ForeignKey(IdentitySource, null=True, on_delete=models.PROTECT)
    identity_digest = models.CharField(max_length=64, blank=True, default='')
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
    subject_attribute = models.CharField(max_length=16, default='entryUUID',
        choices=[('entryUUID', 'entryUUID'), ('objectGUID', 'objectGUID')])
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
    # Проекция правила — одна общая Group на правило, не контейнер на
    # пользователя (#685): участие add/remove, права зеркалит каталог правила.
    group = models.OneToOneField('auth.Group', null=True, on_delete=models.PROTECT,
                                 related_name='directory_rule')

    class Meta(Identifier.Meta):
        constraints = [models.UniqueConstraint(fields=('source', 'group_dn'), name='access_directory_rule_unique')]


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


class EvidenceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError('use grant lifecycle commands')

    def bulk_update(self, *args, **kwargs):
        raise ValueError('use grant lifecycle commands')

    def bulk_create(self, *args, **kwargs):
        raise ValueError('use grant lifecycle commands')

    def delete(self):
        raise ValueError('access evidence requires explicit retention review')


class Evidence(Identifier):
    objects = EvidenceQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('immutable access evidence')
        self.clean()
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('access evidence requires explicit retention review')

    class Meta(Identifier.Meta):
        abstract = True


class RoleBundle(Evidence):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    role = models.CharField(max_length=128)
    digest = models.CharField(max_length=64)
    content_version = models.OneToOneField('secrets.SecretVersion', on_delete=models.PROTECT)
    driver = models.CharField(max_length=16)
    receives_dispatch = models.BooleanField()
    dispatch = models.BooleanField()
    prompt_name = models.CharField(max_length=256)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        from mop.common import soul
        if (not soul.NAME.fullmatch(self.role) or not soul.HEX64.fullmatch(self.digest)
                or self.driver not in soul.DRIVERS):
            raise ValueError('invalid role bundle identity')

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('owner', 'project', 'digest'), name='access_bundle_digest'),
            models.CheckConstraint(condition=Q(driver__in=('any', 'isolating')), name='access_bundle_driver')]


class RoleBundleSeal(Evidence):
    """Доказательство проверки полного normalized set, не автоматическое доверие старым rows."""
    bundle = models.OneToOneField(RoleBundle, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    def save(self, *args, **kwargs):
        raise ValueError('use verified bundle publication')

    @classmethod
    def _record_verified(cls, bundle):
        # Только publisher: parent уже locked, все children сверены с actual bytes.
        seal = cls(bundle=bundle)
        Evidence.save(seal)
        return seal


class BundleChild(Evidence):
    """Обычный append сериализован с проверкой publisher и человеческим approval."""
    def save(self, *args, **kwargs):
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            RoleBundle.objects.using(using).select_for_update().get(pk=self.bundle_id)
            if RoleBundleSeal.objects.using(using).filter(bundle_id=self.bundle_id).exists():
                raise ValueError('role bundle normalized set is sealed')
            return super().save(*args, **kwargs)

    class Meta(Evidence.Meta):
        abstract = True


class RoleCapability(BundleChild):
    bundle = models.ForeignKey(RoleBundle, on_delete=models.PROTECT)
    scope = models.CharField(max_length=8)
    verb = models.CharField(max_length=32)

    def clean(self):
        from mop.common import soul
        if self.verb not in {'node': soul.NODE_VERBS, 'cluster': soul.CLUSTER_VERBS}.get(self.scope, ()):
            raise ValueError('unknown role capability')

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('bundle', 'scope', 'verb'), name='access_bundle_capability'),
            models.CheckConstraint(condition=Q(scope__in=('node', 'cluster')), name='access_capability_scope')]


class RoleSpawn(BundleChild):
    bundle = models.ForeignKey(RoleBundle, on_delete=models.PROTECT)
    role = models.CharField(max_length=128)

    def clean(self):
        from mop.common import soul
        if not soul.NAME.fullmatch(self.role):
            raise ValueError('invalid allowed spawn role')

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('bundle', 'role'), name='access_bundle_spawn')]


class RoleSecretName(BundleChild):
    bundle = models.ForeignKey(RoleBundle, on_delete=models.PROTECT)
    kind = models.CharField(max_length=8)
    name = models.CharField(max_length=256)

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('bundle', 'kind', 'name'), name='access_bundle_secret'),
            models.CheckConstraint(condition=Q(kind__in=('files', 'vars')), name='access_secret_kind')]


class RoleBundleApproval(Evidence):
    bundle = models.OneToOneField(RoleBundle, on_delete=models.PROTECT)
    human = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            RoleBundle.objects.using(using).select_for_update().get(pk=self.bundle_id)
            if not RoleBundleSeal.objects.using(using).filter(bundle_id=self.bundle_id).exists():
                raise ValueError('role bundle is not verified and sealed')
            return super().save(*args, **kwargs)

    def clean(self):
        if self.human_id != self.bundle.owner_id:
            raise ValueError('bundle approval requires its human owner')


class CreationIntent(Evidence):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    # Actor сам credential-holder #684:Principal больше не альтернативная identity.
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    # UUID, не FK: logout удаляет credential, но не immutable намерение.
    actor_credential_id = models.UUIDField()
    project = models.ForeignKey('catalog.Project', on_delete=models.PROTECT)
    bundle = models.ForeignKey(RoleBundle, on_delete=models.PROTECT)
    parent = models.ForeignKey('Grant', null=True, on_delete=models.PROTECT, related_name='+')
    request_id = models.UUIDField()
    target_id = models.UUIDField()
    incarnation = models.UUIDField()
    policy_approval_id = models.UUIDField()
    policy_version = models.ForeignKey('secrets.SecretVersion', on_delete=models.PROTECT)
    policy_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        from .delegation import ApprovedPolicy
        ApprovedPolicy(self.policy_approval_id, self.policy_version_id, self.policy_digest, self.owner_id, self.project_id)
        if self.bundle.owner_id != self.owner_id or self.bundle.project_id != self.project_id:
            raise ValueError('creation bundle owner or project mismatch')
        if self.parent_id and (self.parent.intent.owner_id, self.parent.intent.project_id) != (self.owner_id, self.project_id):
            raise ValueError('creation parent scope mismatch')

    class Meta(Evidence.Meta):
        constraints = [models.UniqueConstraint(fields=('owner', 'actor', 'project', 'request_id'), name='access_creation_request'),
            models.UniqueConstraint(fields=('target_id', 'incarnation'), name='access_creation_incarnation')]


class Grant(Evidence):
    intent = models.OneToOneField(CreationIntent, on_delete=models.PROTECT)
    # Машина делегирования — service Actor (#684); Principal-строка уходит в #688.
    actor = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='grant_machine')
    state = models.CharField(max_length=8, default='reserved')
    revision = models.PositiveBigIntegerField(default=1)
    credential_version = models.OneToOneField('secrets.SecretVersion', null=True, on_delete=models.PROTECT)
    retirement = models.CharField(max_length=8, default='pending')
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        if self.actor.kind != 'service':
            raise ValueError('grant requires its own delegated service actor')
        user = self.actor
        if user.is_staff or user.is_superuser or user.has_usable_password():
            raise ValueError('grant requires a restricted service user')

    def next_state(self, event):
        from .delegation import next_state
        return next_state(self.state, event)

    class Meta(Evidence.Meta):
        # Владелец регистрации access-прав жизненного цикла делегирования (#685):
        # ровно один ContentType на codename; deprecated Principal их не держит.
        permissions = [('view_role', 'Can view role'), ('approve_role', 'Can approve role'),
                       ('issue_grant', 'Can issue grant'), ('revoke_grant', 'Can revoke grant')]
        constraints = [models.CheckConstraint(condition=Q(state__in=('reserved', 'active', 'revoked', 'expired'), revision__gte=1), name='access_grant_lifecycle'),
            models.CheckConstraint(condition=Q(retirement__in=('pending', 'applied', 'refused', 'unknown')), name='access_grant_retirement'),
            models.CheckConstraint(condition=~Q(state='active') | Q(credential_version__isnull=False), name='access_active_credential')]


class GrantEvent(Evidence):
    grant = models.ForeignKey(Grant, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='+')
    action = models.CharField(max_length=16)
    revision = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Evidence.Meta):
        constraints = [models.CheckConstraint(condition=Q(action__in=('reserve', 'provision', 'activate', 'revoke', 'expire', 'retirement')), name='access_grant_event_action')]
