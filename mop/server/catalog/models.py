"""ORM каталог: intent отдельно от внешних фактов и receipts."""
import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone
from .definitions import DEFINITIONS


class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=Q(revision__gte=1), name='%(class)s_revision_positive')]


class Project(Record):
    origin = models.CharField(max_length=2048, unique=True)
    name = models.CharField(max_length=128, unique=True)
    puppet_limit = models.BigIntegerField(null=True)

    class Meta(Record.Meta):
        permissions = (
            ('configure_project', 'Can configure project'),
            ('set_project_limit', 'Can set project limit'),
        )
        constraints = Record.Meta.constraints + [models.CheckConstraint(
            condition=Q(puppet_limit__isnull=True) | Q(puppet_limit__gte=0), name='project_limit_nonnegative')]


class Node(Record):
    name = models.CharField(max_length=128, unique=True)
    driver = models.CharField(max_length=16)
    ssh_host = models.CharField(max_length=253)
    ssh_port = models.PositiveIntegerField(default=22)
    control_origin = models.URLField(max_length=2048)
    lan_address = models.CharField(max_length=253)
    bootstrap_address = models.CharField(max_length=253)
    deployment_member = models.BooleanField(default=False)
    desired_eligible = models.BooleanField(default=True)
    applied_revision = models.PositiveBigIntegerField(null=True)
    applied_receipt = models.CharField(max_length=512, blank=True)
    applied_at = models.DateTimeField(null=True)
    observed_eligible = models.BooleanField(null=True)
    observed_source = models.CharField(max_length=256, blank=True)
    observed_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(null=True)
    settings_revision = models.PositiveBigIntegerField(default=1)
    settings_applied_revision = models.PositiveBigIntegerField(null=True)
    settings_receipt = models.CharField(max_length=512, blank=True)
    settings_applied_at = models.DateTimeField(null=True)

    class Meta(Record.Meta):
        permissions = (
            ('configure_node', 'Can configure node'),
            ('record_node_application', 'Can record node application'),
            ('observe_node', 'Can observe node'),
        )
        constraints = Record.Meta.constraints + [
            models.CheckConstraint(condition=Q(driver__in=('host', 'pve')), name='node_known_driver'),
            models.CheckConstraint(condition=Q(ssh_port__gte=1, ssh_port__lte=65535), name='node_ssh_port'),
            models.CheckConstraint(condition=Q(settings_revision__gte=1), name='node_settings_revision'),
            models.CheckConstraint(condition=Q(applied_revision__isnull=True) | Q(applied_revision__lte=models.F('revision')), name='node_application_not_future'),
        ]


class NodeProject(Record):
    node = models.ForeignKey(Node, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.UniqueConstraint(fields=('node', 'project'), name='node_project_unique')]


class InstallationConfiguration(Record):
    installation = models.OneToOneField('controller.Installation', on_delete=models.PROTECT)
    applied_revision = models.PositiveBigIntegerField(null=True)
    applied_receipt = models.CharField(max_length=512, blank=True)
    applied_at = models.DateTimeField(null=True)

    class Meta(Record.Meta):
        permissions = (
            ('record_settings_application', 'Can record settings application'),
        )


def setting_constraint(node):
    valid = Q(pk__isnull=True)
    for key, definition in DEFINITIONS.items():
        if node and not definition.node_override:
            continue
        clause = Q(key=key, value_kind=definition.kind)
        for kind in ('text', 'int', 'bool'):
            clause &= Q(**{kind + '_value__isnull': kind != definition.kind})
        if definition.kind == 'int':
            clause &= Q(int_value__gte=definition.minimum, int_value__lte=definition.maximum)
        if definition.choices:
            clause &= Q(**{definition.kind + '_value__in': definition.choices})
        valid |= clause
    return valid


class Setting(Record):
    key = models.CharField(max_length=64)
    value_kind = models.CharField(max_length=8)
    text_value = models.TextField(null=True)
    int_value = models.BigIntegerField(null=True)
    bool_value = models.BooleanField(null=True)

    class Meta(Record.Meta):
        abstract = True


class InstallationSetting(Setting):
    installation = models.ForeignKey('controller.Installation', on_delete=models.PROTECT)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [
            models.UniqueConstraint(fields=('installation', 'key'), name='installation_setting_unique'),
            models.CheckConstraint(condition=setting_constraint(False), name='installation_setting_typed'),
        ]


class NodeSetting(Setting):
    node = models.ForeignKey(Node, on_delete=models.PROTECT)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [
            models.UniqueConstraint(fields=('node', 'key'), name='node_setting_unique'),
            models.CheckConstraint(condition=setting_constraint(True), name='node_setting_typed'),
        ]


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError('immutable catalogue evidence')

    def delete(self):
        raise ValueError('immutable catalogue evidence')


class ImmutableRecord(Record):
    objects = ImmutableQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('immutable catalogue evidence')
        # force_insert запрещает подмену существующего pk новым instance.
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('immutable catalogue evidence')

    class Meta(Record.Meta):
        abstract = True


class ProjectPolicySnapshot(ImmutableRecord):
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    sequence = models.PositiveBigIntegerField()
    content = models.BinaryField()
    digest = models.CharField(max_length=64)
    source_commit = models.CharField(max_length=64)
    source_ref = models.CharField(max_length=256)
    provenance = models.CharField(max_length=256)
    confidential = models.BooleanField(default=False)
    protection_reference = models.CharField(max_length=256, blank=True)
    cpu = models.PositiveIntegerField()
    memory_mb = models.PositiveIntegerField()
    disk_mb = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(Record.Meta):
        permissions = (
            ('pin_project_policy', 'Can pin project policy'),
        )
        constraints = Record.Meta.constraints + [
            models.UniqueConstraint(fields=('project', 'sequence'), name='policy_project_sequence'),
            models.CheckConstraint(condition=Q(sequence__gte=1, cpu__gte=1, memory_mb__gte=1, disk_mb__gte=1), name='policy_positive_resources'),
            models.CheckConstraint(condition=Q(confidential=False, protection_reference='') | (Q(confidential=True) & ~Q(protection_reference='')), name='policy_protection_reference'),
        ]


class ImageRevision(ImmutableRecord):
    policy = models.ForeignKey(ProjectPolicySnapshot, on_delete=models.PROTECT)
    source = models.CharField(max_length=256)
    created_at = models.DateTimeField(default=timezone.now)


class NodeImage(Record):
    node = models.ForeignKey(Node, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    desired_image = models.ForeignKey(ImageRevision, null=True, on_delete=models.PROTECT, related_name='+')
    observed_image = models.ForeignKey(ImageRevision, null=True, on_delete=models.PROTECT, related_name='+')
    available = models.BooleanField(null=True)
    source = models.CharField(max_length=256, blank=True)
    observed_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(null=True)
    receipt = models.CharField(max_length=512, blank=True)

    def clean(self):
        # Межтабличный инвариант: Django CheckConstraint не допускает JOIN.
        for image in (self.desired_image, self.observed_image):
            if image is not None and image.policy.project_id != self.project_id:
                raise ValueError('image belongs to another project')

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.UniqueConstraint(fields=('node', 'project'), name='node_image_unique')]


class ClientRegistration(Record):
    installation = models.ForeignKey('controller.Installation', on_delete=models.PROTECT)
    label = models.CharField(max_length=128)
    registered_at = models.DateTimeField(default=timezone.now)


class WorkspaceBinding(Record):
    client = models.ForeignKey(ClientRegistration, on_delete=models.PROTECT)
    workspace_key = models.CharField(max_length=256)
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    integration_branch = models.CharField(max_length=256)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [models.UniqueConstraint(fields=('client', 'workspace_key'), name='workspace_client_key_unique')]
