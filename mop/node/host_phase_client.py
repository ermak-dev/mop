"""Node-only admitted bridge: bearer/MachineClient никогда не уходят в body/root."""
import asyncio
from dataclasses import dataclass

from mop.common.initialcreate import CONFIRMATIONS, PLANNED
from mop.node.host_backend_client import BackendClient
from mop.node.host_input_files import NodeWriter
from mop.node.host_inputs import admission_wire, canonical_json
from mop.node.host_phases import Projection, Submission, admitted
from mop.node.host_scope import Reference, Scope, canonical
from mop.node.http_execution import Admission, CommittedResult, MachineClient, Refused, Unavailable


@dataclass
class _Owned:
    admission: Admission
    scope: Scope
    descriptor: object = None
    phase_receipt: str | None = None
    succeeded: bool = False


class PlannedExecutor:
    """Только первые три planned actions, не complete NativeBindings capability."""
    def __init__(self, client, backend, writer):
        if type(client) is not MachineClient or type(backend) is not BackendClient or type(writer) is not NodeWriter:
            raise Refused('real MachineClient, installed backend and NodeWriter required')
        self.client, self.backend, self.writer = client, backend, writer
        self._owned, self._references = {}, {}

    async def reconcile(self, *args):
        raise Unavailable('planned executor requires the live physical backend binding')

    @staticmethod
    def address(owned, action='phase_status'):
        return dict(schema_version=1, action=action, scope=owned.scope.wire(),
                    attempt_id=owned.admission.attempt_id)

    @staticmethod
    def scope(work):
        return Scope(work.epoch, work.node_id, work.executor_epoch, work.target_id, work.incarnation_id, work.operation_id)

    @staticmethod
    def same_admission(phase, admission):
        if canonical_json(phase['admission']) != canonical_json(admission_wire(admission)):
            raise Refused('observed root phase differs from the original actual Admission')

    async def exchange(self, request, *, guard=None):
        async with self.backend.connect() as session:
            return await session.planned(request, guard=guard)

    async def observe(self, scope, attempt_id):
        """Read-only original attempt status; ни claim, ни fresh submission."""
        phase = await self.exchange(dict(schema_version=1, action='phase_status',
            scope=scope.wire(), attempt_id=canonical(attempt_id)))
        if phase['status'] == 'succeeded' and phase['acknowledged'] is True:
            reference = Reference.parse(phase['reference'])
            if reference.scope != scope:
                raise Refused('observed committed root reference scope changed')
            previous = self._references.get(scope)
            if previous is not None and previous != reference:
                raise Refused('observed root owner replaced the retained reference')
            self._references[scope] = reference
        return phase

    async def execute(self, work, guard):
        admission = admitted(guard, work)
        if admission.attempt_id in self._owned:
            raise Refused('planned execution replay forbidden; observe retained original attempt')
        await guard()
        value = await self.client.read_initial_launch(work.wire())
        guard.check()
        scope = self.scope(work)
        descriptor = self.writer.write(admission, scope, value)
        previous = None if work.action == 'prepare_body' else self._references.get(scope)
        if previous is None and work.action != 'prepare_body':
            # Frontend replacement наблюдает exact owner prepare attempt, но не
            # угадывает Reference из label/SQL marker.
            raise Unavailable('planned phase requires an explicitly observed retained root reference')
        submission = Submission(admission, descriptor, Projection.from_delivery(value), previous)
        owned = _Owned(admission, scope, descriptor)
        self._owned[admission.attempt_id] = owned
        phase = await self.exchange(submission)
        return await self._drive(owned, guard, phase)

    async def resume(self, work, guard):
        """Только retained original attempt, без read_initial_launch/write/submission."""
        admission = admitted(guard, work)
        owned = self._owned.get(admission.attempt_id)
        if owned is not None and (owned.admission is not admission or owned.succeeded):
            raise Refused('original planned owner cannot be replaced or reexecuted')
        if owned is None:
            owned = _Owned(admission, self.scope(work))
        phase = await self.exchange(self.address(owned))
        self.same_admission(phase, admission)
        self._owned[admission.attempt_id] = owned
        return await self._drive(owned, guard, phase)

    async def _drive(self, owned, guard, phase):
        work, scope = owned.admission.work, owned.scope
        try:
            while True:
                guard.check()
                if phase['status'] == 'succeeded':
                    reference = Reference.parse(phase['reference'])
                    if reference.scope != scope:
                        raise Refused('planned root response scope changed')
                    owned.phase_receipt, owned.succeeded = canonical(phase['receipt']), True
                    self._references[scope] = reference
                    if owned.descriptor is not None:
                        self.writer.forget(owned.descriptor)
                    return 'succeeded', CONFIRMATIONS[work.action]
                if phase['status'] == 'unknown':
                    raise Unavailable(phase['error'] or 'planned original phase outcome unknown')
                if phase['status'] != 'accepted':
                    raise Refused('unknown planned phase status')
                if phase['checkpoint'] is not None:
                    phase = await self.exchange(self.address(owned, 'phase_checkpoint'), guard=guard)
                else:
                    await asyncio.sleep(.03)
                    phase = await self.exchange(self.address(owned))
        except asyncio.CancelledError:
            # Frontend cancellation не отменяет root task; original cutoff и
            # retained cleanup owner продолжают действовать без новой admission.
            raise

    def result_identity(self, admission):
        owned = self._owned.get(admission.attempt_id)
        if owned is None or owned.admission is not admission or not owned.succeeded:
            raise Refused('result identity requires the actual completed original phase')
        return canonical(owned.phase_receipt)

    async def recover_result(self, admission):
        """Повторяет только original SQL receipt/Root ACK, не execution и не claim."""
        if type(admission) is not Admission or admission.work.action not in PLANNED:
            raise Refused('original planned Admission required for result recovery')
        owned = _Owned(admission, self.scope(admission.work))
        phase = await self.exchange(self.address(owned))
        self.same_admission(phase, admission)
        if phase['status'] != 'succeeded':
            raise Unavailable('only an actually completed original phase can recover its result')
        reference = Reference.parse(phase['reference'])
        if reference.scope != owned.scope:
            raise Refused('completed recovery root scope changed')
        owned.phase_receipt, owned.succeeded = canonical(phase['receipt']), True
        self._owned[admission.attempt_id] = owned
        self._references[owned.scope] = reference
        if not phase['acknowledged']:
            committed = await self.client.result(admission, 'succeeded', CONFIRMATIONS[admission.work.action],
                receipt_id=owned.phase_receipt)
            await self.acknowledge(committed)

    async def acknowledge(self, result):
        if type(result) is not CommittedResult:
            raise Refused('exact validated committed MachineClient result required')
        owned = self._owned.get(result.admission.attempt_id)
        if owned is None or result.admission is not owned.admission:
            raise Refused('committed planned result has no original local Admission')
        if result.outcome != 'succeeded':
            return
        if (not owned.succeeded or result.receipt_id != owned.phase_receipt
                or result.evidence != CONFIRMATIONS[result.admission.work.action]):
            raise Refused('committed result differs from the actual completed planned phase')
        request = dict(schema_version=1, action='phase_ack', scope=owned.scope.wire(),
            attempt_id=result.admission.attempt_id, receipt=owned.phase_receipt, committed=result.wire())
        phase = await self.exchange(request)
        if phase['acknowledged'] is not True or phase['receipt'] != owned.phase_receipt:
            raise Unavailable('planned exact result ACK outcome unknown')

    async def aclose(self):
        # Успешный passive broker остаётся root-owned, даже когда frontend ушёл.
        return None
