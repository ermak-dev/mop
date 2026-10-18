"""#625: SQL authority только claimed подготовки, не физический lifecycle.

Нет fallback на create или latest policy. Отсутствующие continuation/cutover/
retirement ports не подменяются этой выдачей prospective credential.
"""
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from mop.server.access import services as access
from mop.server.access.delegation import ApprovedPolicy, ReplacementIntent, RoleBinding
from mop.server.operations import models as operations
from mop.server.operations.service import _binding
from mop.server.sessions.models import Assignment, ExecutionIncarnation
from mop.server.sessions.services import Assignments
from .runtime import operation_installation_state


STAGES = ('prepare_launch', 'scheduler_register', 'apply_identity', 'confirm_ready', 'activate_grant')


class ReplacementPreparation:
    """Request-local locator proof, перечитываемый до и после provider wait.

    Human credential приходит только в защищённый credential endpoint.
    Никакой worker discovery этот шаг не исполняет.
    """
    def __init__(self, application, operation_id):
        self.application, self.operation_id, self.proof = application, operation_id, None

    def require(self, actor):
        app = self.application
        with transaction.atomic():
            # Тот же порядок, что у Operations: restore -> Assignment ->
            # Operation -> Steps -> отсортированные Resource -> Access grants.
            installation = operation_installation_state()
            app.fence()
            access.current_actor(actor, grants=app.local_grants)
            if actor.kind is not access.ActorKind.HUMAN or actor.grant_id is not None:
                raise access.Denied('authenticated human required for replacement preparation')
            if not installation.effects_enabled:
                raise access.Denied('installation effects are disabled')
            target_id = operations.Operation.objects.filter(pk=self.operation_id).values_list('target_id', flat=True).first()
            source = None if target_id is None else Assignments().target(target_id, lock=True)
            row = operations.Operation.objects.select_for_update().filter(pk=self.operation_id).first()
            if (source is None or row is None or row.action != 'update' or row.state != 'running'
                    or row.epoch != installation.epoch or row.restore_generation != installation.generation
                    or row.epoch != UUID(app.snapshot['epoch']) or row.not_after <= timezone.now()
                    or row.actor_id != actor.user_id or row.actor_binding != _binding(actor)
                    or row.target_kind != 'puppet' or row.target_id != source.target_id or row.node_id != source.node_id
                    or source.owner_id != actor.user_id
                    or set(row.projects.values_list('pk', flat=True)) != {source.project_id}):
                raise access.Denied('matching claimed update preparation required')
            for action in ('operations.view_operation', 'execution.update_puppet', 'execution.create_puppet', 'access.issue_grant'):
                access.Access().authorize(actor, action, project_ids=(source.project_id,))
            old = operations.ReplacementBinding.objects.filter(operation=row).first()
            new = operations.RestartBinding.objects.filter(operation=row).first()
            assignment = Assignment.objects.get(pk=source.target_id)
            if (old is None or new is None
                    or (old.source_incarnation_id, old.source_grant_id, old.source_revision, old.source_generation,
                        old.source_expires_at) != (source.incarnation_id, source.grant_id, source.revision,
                                                source.generation, assignment.expires_at)
                    or (new.project_id, new.executor_epoch, new.owner_id, new.generation, new.role, new.bundle_digest) !=
                       (source.project_id, source.executor_epoch, source.owner_id, source.generation+1, source.role, source.bundle_digest)
                    or row.expected_revision != source.revision+1 or old.expires_at <= timezone.now()
                    or old.expires_at > old.source_expires_at
                    or ExecutionIncarnation.objects.filter(pk=row.incarnation_id).exists()):
                raise access.Denied('replacement source or destination pins changed')
            stages = tuple(operations.Step.objects.select_for_update().filter(operation=row).order_by('ordinal'))
            if (tuple(step.name for step in stages) != STAGES
                    or tuple(step.ordinal for step in stages) != tuple(range(1, len(STAGES)+1))
                    or any(step.token is not None or step.started_at is not None or step.outcome
                           or step.dispatched_at is not None or step.quiescent_at is not None for step in stages[1:])):
                raise access.Denied('finite claimed replacement preparation required')
            step = stages[0]
            if (step.token is None or step.attempt_id != step.token or step.started_at is None or step.dispatched_at is None
                    or step.quiescent_at is not None or step.outcome or step.lease_until is None or step.lease_until <= timezone.now()):
                raise access.Denied('live claimed replacement preparation required')
            keys = {'node:'+str(source.node_id), 'project:'+str(source.project_id),
                    'puppet:'+str(source.target_id), 'target:'+str(source.target_id)}
            resources = tuple(operations.Resource.objects.select_for_update().filter(pk__in=keys)
                              .order_by('pk').values_list('pk', flat=True))
            claims = tuple(operations.ResourceClaim.objects.filter(resource_id__in=keys, released_at=None)
                           .order_by('resource_id').values_list('resource_id', 'operation_id', 'step_id', 'token'))
            if (set(resources) != keys or {claim[0] for claim in claims} != keys
                    or any(claim[1:] != (row.id, step.id, step.token) for claim in claims)):
                raise access.Denied('exact replacement preparation resource fences required')
            source_role = RoleBinding(source.project_id, source.target_id, source.incarnation_id, source.role,
                                      source.bundle_digest, source.grant_id, source.owner_id)
            destination = RoleBinding(new.project_id, row.target_id, row.incarnation_id, new.role,
                                      new.bundle_digest, new.grant_id, new.owner_id)
            intent = ReplacementIntent(row.id, source_role, destination, old.definition_id,
                ApprovedPolicy(old.policy_approval_id, old.policy_version_id, old.policy_digest, source.owner_id, source.project_id),
                old.expires_at)
            proof = (intent, source, row.revision, row.not_after, step.id, step.token, step.lease_until, claims)
            if self.proof is not None and self.proof != proof:
                raise access.Denied('claimed replacement preparation authority changed')
            self.proof = proof
            return intent
