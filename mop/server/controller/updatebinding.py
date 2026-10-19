"""Composition update (#625): SQL execution pins и protected credential client.

Root обязан передать ReplacementSupport явно. Текущие runtime/native observer
его не реализуют: не имитируем continuation и не открываем физические эффекты.
"""
from mop.common.credentials import GrantReceipt
from mop.server.operations.replacement import LaunchPins, UpdatePuppets
from mop.server.operations.service import Refused
from .machinebinding import Targets, local_grants
from .runtime import operations_service


class ReplacementTargets(Targets):
    def launch_pins(self, source):
        from mop.server.access.models import Grant
        from mop.server.sessions.models import Assignment, IncarnationLaunch
        assignment = Assignment.objects.get(pk=source.target_id)
        launch = IncarnationLaunch.objects.filter(incarnation_id=source.incarnation_id).first()
        grant = Grant.objects.select_related('intent').get(pk=source.grant_id)
        intent = grant.intent
        if intent.parent_id is not None:
            raise Refused('controller_unavailable', safe_reason='delegated issuer replacement is unavailable')
        if (launch is None or launch.image_revision_id is None
                or (intent.target_id, intent.incarnation, intent.project_id, intent.owner_id) !=
                   (source.target_id, source.incarnation_id, source.project_id, source.owner_id)):
            raise Refused('revision_conflict', safe_reason='exact source launch pins are unavailable')
        return LaunchPins(assignment.expires_at, intent.expires_at, intent.definition_id,
            intent.policy_approval_id, intent.policy_version_id, intent.policy_digest,
            launch.bootstrap_revision, launch.bootstrap_digest, launch.image_revision_id, launch.image_digest)

    def prepared(self, row):
        """SQL metadata proof, не crypto и не будущая activation authority."""
        from mop.server.access.models import Grant
        from .replacement import validate_plan
        binding, plan = row.replacementbinding, row.replacementplan
        if not validate_plan(row):
            return None
        source = Grant.objects.select_related('intent').filter(pk=binding.source_grant_id).first()
        grant = Grant.objects.select_related('intent').filter(pk=plan.destination_grant_id).first()
        if source is None or grant is None or grant.state != 'reserved' or grant.credential_version_id is None:
            return None
        old, new = source.intent, grant.intent
        from uuid import UUID
        if (grant.subject_id != source.subject_id or new.parent_id is not None
                or (new.owner_id, new.actor_id, new.actor_credential_id, new.project_id, new.target_id,
                    new.incarnation, new.definition_id, new.policy_approval_id, new.policy_version_id,
                    new.policy_digest, new.expires_at, new.request_id) !=
                   (plan.owner_id, row.actor_id, UUID(row.actor_binding['credential_id']), plan.project_id, row.target_id,
                    plan.destination_incarnation_id, binding.definition_id, binding.policy_approval_id,
                    binding.policy_version_id, binding.policy_digest, binding.expires_at, row.id)):
            return None
        if (old.policy_approval_id, old.policy_version_id, old.policy_digest, old.definition_id,
                min(binding.source_expires_at, old.expires_at)) != (binding.policy_approval_id,
                binding.policy_version_id, binding.policy_digest, binding.definition_id, binding.expires_at):
            return None
        return GrantReceipt(grant.id, new.project_id, new.target_id, new.incarnation,
            plan.role, plan.bundle_digest, grant.state, grant.revision, grant.credential_version_id)


def service(*, grants=None, issuer=None, scheduler=None, support=None):
    """Публичная точка связывания SQL-only ReplacementSupport из execution.

    Не переиспользует create как replacement, не регистрирует агента, не
    выбирает latest transcript. Worker получает только общий Operation журнал;
    prepare доступен лишь request-local submit с current human credential.
    """
    from .adminservices import credential_client
    grants = grants or local_grants()
    targets = ReplacementTargets(grants)
    ops = operations_service(grants=grants.local_authority(), targets=targets)
    ops.updates = UpdatePuppets(ops, targets=targets,
        issuer=issuer if issuer is not None else credential_client(), scheduler=scheduler, support=support)
    return ops
