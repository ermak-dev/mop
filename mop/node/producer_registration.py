"""MachineClient adapter для prepared runtime ProducerRegistration port.

Только отчёт доверенного physical observer; нет hook/registry discovery, mint
producer epoch или disk authority. Transport/credential lifetime владеет root.
"""
import asyncio
from uuid import UUID

from mop.common.producer import ProducerAck, ProducerReport, predecessor
from mop.driver.launch import Registration
from mop.node.execution_adapters import SessionProof
from mop.node.http_execution import MachineClient, Refused, Work


class ProducerRegistrationClient:
    def __init__(self, client: MachineClient, registration: Registration, *, expected_prior, expected_revision):
        if (type(client) is not MachineClient or type(registration) is not Registration
                or type(registration.work) is not Work):
            raise Refused('trusted machine client and exact current launch registration required')
        predecessor(expected_prior, expected_revision)
        self.client, self.registration = client, registration
        self.expected_prior, self.expected_revision = expected_prior, expected_revision
        self.acknowledgement = None
        self._proof = None

    async def register(self, proof: SessionProof):
        if type(proof) is not SessionProof or proof.registration != self.registration:
            raise Refused('producer proof differs from exact registered launch work')
        if self._proof is not None and proof != self._proof:
            raise Refused('producer registration adapter cannot change its physical proof')
        w, r = self.registration.work, self.registration
        w.gate(node_id=w.node_id, epoch=w.epoch, executor_epoch=w.executor_epoch)
        try:
            report = ProducerReport(proof.scope, UUID(r.bootstrap_revision), r.bootstrap_digest,
                r.image_digest, r.display_name, proof.pane, proof.pid, proof.process_start)
        except ValueError as error:
            raise Refused(str(error)) from None
        self._proof = proof
        return await self.register_report(report)

    async def register_report(self, report: ProducerReport):
        """Fixed HOST proxy принимает только root-verified typed report, не fake proof."""
        if type(report) is not ProducerReport:
            raise Refused('typed root-verified producer report required')
        w, r = self.registration.work, self.registration
        w.gate(node_id=w.node_id, epoch=w.epoch, executor_epoch=w.executor_epoch)
        if (tuple(str(getattr(report.scope, k)) for k in
                ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')) != (
                w.epoch, w.target_id, w.incarnation_id, w.node_id, w.executor_epoch)
                or (str(report.bootstrap_revision), report.bootstrap_digest, report.image_digest, report.display_name)
                != (r.bootstrap_revision, r.bootstrap_digest, r.image_digest, r.display_name)):
            raise Refused('producer report differs from exact registered launch work')
        previous = getattr(self, '_report', None)
        if previous is not None and previous != report:
            raise Refused('producer registration adapter cannot change its physical report')
        self._report = report
        if self.expected_prior is not None and any(getattr(self.expected_prior, key) != getattr(report.scope, key)
                for key in ('epoch','target_id','incarnation_id','node_id','executor_epoch')):
            raise Refused('producer predecessor belongs to a different launch scope')
        payload = dict(schema_version=1, work=w.wire(), **report.wire(),
            expected_prior=self.expected_prior.wire() if self.expected_prior is not None else None,
            expected_revision=str(self.expected_revision))
        # Даже потерянный ответ мог COMMIT-нуть регистрацию. Этот экземпляр
        # повторяет только те же work/proof/CAS, не выбирает другое прошлое.
        async with asyncio.timeout(5):
            value = await self.client.transport.request('POST', 'session-producers', payload=payload)
        try:
            ack = ProducerAck.parse(value)
        except ValueError as error:
            raise Refused(str(error)) from None
        revision = self.expected_revision if self.expected_prior == report.scope else self.expected_revision+1
        if ack.scope != report.scope or ack.revision != revision:
            raise Refused('producer acknowledgement changed exact scope or CAS revision')
        w.gate(node_id=w.node_id, epoch=w.epoch, executor_epoch=w.executor_epoch)
        self.acknowledgement = ack
        return ack.scope
