"""Конечная адресная выдача запуска: web rootless, provider только в службе.

Work -- locator, не authority. Gate повторяет публичные execution/access порты
и пины до/после provider; owner credential никогда не восстанавливается.
"""
import json

from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist
from mop.server.operations.service import Refused
from django.views.decorators.debug import sensitive_variables

from mop.common import credentials as p
from mop.server.access import services as access
from mop.server.access.delegation import RoleBinding
from mop.server.sessions import protocol
from mop.server.sessions.services import Assignments, Conflict, Unavailable


class LaunchAuthority:
    """Один запрос, один immutable proof. Никакого кэша доступа между вызовами."""
    def __init__(self, application, actor, work):
        from .createbinding import LaunchSupport
        from .machinebinding import compose
        self.application, self.actor, self.work = application, actor, work
        self.execution = compose(grants=application.grants, launch_support=LaunchSupport())
        self.proof = None

    def selection(self):
        from mop.server.sessions.models import IncarnationLaunch
        from mop.server.preparation.models import BootstrapApproval
        from mop.server.operations.launch import image_pin
        app, actor, work = self.application, self.actor, self.work
        with transaction.atomic():
            app.fence()
            access.node_executor(actor, work.node_id)
            current, attempt_id = self.execution.action_admission(actor,
                operation_id=work.operation_id, step_id=work.step_id)
            if current != work:
                raise Conflict('addressed launch work changed')
            binding = Assignments().target(work.target_id, lock=True)
            if binding is None:
                raise access.Denied('current launch assignment required')
            access.Access().authorize(actor, 'execution.read_launch', project_ids=(binding.project_id,),
                node_ids=(binding.node_id,))
            definition = self.execution.work.launch_definition(work, attempt_id)
            launch = IncarnationLaunch.objects.select_related('image_revision__policy').get(incarnation_id=work.incarnation_id)
            approved = BootstrapApproval.objects.filter(revision_id=launch.bootstrap_revision,
                human_id=binding.owner_id, revision__owner_id=binding.owner_id,
                revision__project_id=binding.project_id, revision__digest=launch.bootstrap_digest,
                approvalrevocation__isnull=True).exists()
            if not approved:
                raise access.Denied('pinned launch bootstrap approval is not current')
            if (launch.image_revision_id is None or launch.image_revision.policy.project_id != binding.project_id
                    or image_pin(launch.image_revision_id) != launch.image_digest):
                raise access.Denied('pinned launch image changed')
            from .machinebinding import owner_identity
            identity = owner_identity(binding.owner_id)
            pins = (str(launch.bootstrap_revision), launch.bootstrap_digest, launch.image_digest)
            proof = (current, attempt_id, binding, definition, pins, identity)
            if self.proof is not None and proof != self.proof:
                raise Conflict('addressed launch authority or pins changed')
            self.proof = proof
            role = RoleBinding(binding.project_id, binding.target_id, binding.incarnation_id,
                binding.role, binding.bundle_digest, binding.grant_id, binding.owner_id)
            return role, definition, pins, identity

    def check(self, actor, scope):
        if actor != self.actor:
            raise access.Denied('addressed launch actor changed')
        role, definition, _, _ = self.selection()
        from mop.server.secrets.services import RetentionScope
        if scope != RetentionScope(role.project_id, role.human_owner_id, definition, 'policy'):
            raise access.Denied('addressed launch definition scope changed')


@sensitive_variables('content')
def deliver(application, actor, metadata):
    try:
        work = protocol.work_from_wire(metadata)
    except ValueError:
        raise p.CredentialError('invalid addressed launch work') from None
    gate = LaunchAuthority(application, actor, work)
    try:
        role, definition, pins, identity = gate.selection()
        content = application.grants.read_launch_definition(actor, role, definition, authority=gate)
        gate.selection()
    except ObjectDoesNotExist:
        raise access.Denied('current addressed launch work is unavailable') from None
    except (Conflict, Refused) as error:
        raise p.CredentialRefused(str(error)) from None
    return dict(schema_version=1, work=protocol.work_to_wire(work), bootstrap_revision=pins[0],
        bootstrap_digest=pins[1], image_digest=pins[2], manifest=json.loads(content.manifest),
        prompt=p.encoded(content.prompt), git_name=identity.name, git_email=identity.email,
        message='', peer_token=None)


def read(credential, metadata):
    """Rootless HTTP adapter. Socket видит исходный machine token, не web owner."""
    from .adminservices import credential_client
    client = credential_client()
    if client is None:
        raise Unavailable('addressed launch credential service is not bound')
    try:
        return client.read_launch(credential, metadata)
    except p.CredentialRefused as error:
        raise access.Denied(str(error)) from None
    except p.CredentialError as error:
        raise Unavailable(str(error)) from None
