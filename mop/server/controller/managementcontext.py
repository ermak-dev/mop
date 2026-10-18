"""Composition post-activation query: public Sessions и Access ports, без effects."""
from uuid import UUID
from datetime import timezone
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from mop.common import credentials as p
from mop.common.management_context import ManagementRequest, ManagementContext
from mop.server.access import services as access
from mop.server.access.delegation import RoleBinding
from mop.server.sessions.services import Assignments, Unavailable


class CurrentManagement:
    """Request-local proof: SQL snapshot повторяется после provider wait."""
    def __init__(self, app, actor, request):
        self.app, self.actor, self.request = app, actor, request
        self.proof = None

    def require(self):
        self.app.fence()
        if not self.app.snapshot['effects_enabled']:
            raise access.Denied('management context is disabled')
        proof = Assignments().management_proof(self.actor, self.request,
            epoch=UUID(self.app.snapshot['epoch']))
        if self.proof is not None and proof != self.proof:
            raise access.Denied('management runtime changed during read')
        self.proof = proof
        return proof


@sensitive_variables('credential')
def deliver(app, actor, metadata):
    request = ManagementRequest.parse(metadata)
    authority = CurrentManagement(app, actor, request)
    with transaction.atomic():
        proof = authority.require()
    binding = proof.binding
    role = RoleBinding(binding.project_id, binding.target_id, binding.incarnation_id,
        binding.role, binding.bundle_digest, binding.grant_id, binding.owner_id)
    credential = app.grants.read_management_credential(role, authority=authority)
    return ManagementContext(request, app.snapshot['identity'], UUID(app.generation),
        binding.project_id, binding.grant_id, credential.subject_id, credential.credential_id,
        binding.revision, min(proof.expires_at, credential.expires_at).astimezone(timezone.utc), credential.token).wire()


def read(credential, metadata):
    """Rootless web forwards original token; origin/CA здесь не выбираются."""
    from .adminservices import credential_client
    client = credential_client()
    if client is None:
        raise Unavailable('management context credential service is not bound')
    try:
        return client.read_management_context(credential, metadata).wire()
    except p.CredentialRefused as error:
        raise access.Denied(str(error)) from None
    except p.CredentialError as error:
        raise Unavailable(str(error)) from None
