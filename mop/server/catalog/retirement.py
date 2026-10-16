"""Узкие доказательства metadata retirement, не разрешение внешнего эффекта."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID


class UnsafeRemoval(ValueError):
    """Нет актуального адресного доказательства безопасного retirement."""


@dataclass(frozen=True)
class ProjectRemovalProof:
    target_id: UUID
    revision: int
    receipt_id: UUID


@dataclass(frozen=True)
class NodeRemovalProof:
    target_id: UUID
    revision: int
    receipt_id: UUID


@dataclass(frozen=True)
class PreparedProjectRetirement:
    target_id: UUID
    expected_revision: int
    proof: ProjectRemovalProof


@dataclass(frozen=True)
class PreparedNodeRetirement:
    target_id: UUID
    expected_revision: int
    proof: NodeRemovalProof


@dataclass(frozen=True)
class SourceObservation:
    source: str
    source_incarnation: UUID
    sequence: int
    receipt: str
    observed_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ProjectRemovalReceipt:
    proof: ProjectRemovalProof
    scheduler: SourceObservation
    remaining_jobs_including_stopped: tuple[str, ...] | None


@dataclass(frozen=True)
class NodeRemovalReceipt:
    proof: NodeRemovalProof
    scheduler: SourceObservation
    executor: SourceObservation
    scheduler_occupants: tuple[str, ...] | None
    executor_occupants: tuple[str, ...] | None


class RemovalGuard(Protocol):
    """Trusted root port, не callable из ActorContext или JSON.

    inspect_* вызываются вне SQL и могут ждать внешние источники. Proof
    ссылается на retained адресный receipt, но сам не является authority.
    validate_* выполняют только SQL: повторно читают настоящий receipt и
    актуальные source generations/coordination records под target lock.
    Отсутствующий, изменённый, неполный либо superseded receipt — отказ.
    Вернуть копию полей proof без чтения authority недостаточно. Writers
    поздних Sessions/Operations должны брать тот же parent lock до допуска
    нового job/body/build и запрещать retired target. Внешний scheduler без
    такого fencing не может выдать безопасный proof; тогда guard отказывает.
    """
    def inspect_project(self, target) -> ProjectRemovalProof: ...
    def inspect_node(self, target) -> NodeRemovalProof: ...
    def validate_project(self, proof: ProjectRemovalProof, target) -> ProjectRemovalReceipt: ...
    def validate_node(self, proof: NodeRemovalProof, target) -> NodeRemovalReceipt: ...


def validate_proof(proof, proof_type, target_id, revision):
    if (type(proof) is not proof_type or not isinstance(proof.target_id, UUID)
            or not isinstance(proof.receipt_id, UUID) or type(proof.revision) is not int
            or proof.revision < 1 or proof.target_id != target_id or proof.revision != revision):
        raise UnsafeRemoval('retirement proof does not match target identity and revision')


def _observation(value, now):
    if type(value) is not SourceObservation:
        raise UnsafeRemoval('fresh source observation is required')
    if (not isinstance(value.source, str) or not value.source.strip()
            or not isinstance(value.source_incarnation, UUID)
            or type(value.sequence) is not int or value.sequence < 1
            or not isinstance(value.receipt, str) or not value.receipt.strip()):
        raise UnsafeRemoval('source identity, sequence and actual receipt are required')
    start, end = value.observed_at, value.expires_at
    if (not isinstance(start, datetime) or start.utcoffset() is None
            or not isinstance(end, datetime) or end.utcoffset() is None
            or not start <= now < end or end - start > timedelta(minutes=5)):
        raise UnsafeRemoval('fresh bounded source observation is required')


def validate_project_receipt(receipt, proof, now):
    if type(proof) is not ProjectRemovalProof:
        raise UnsafeRemoval('typed project removal proof is required')
    validate_proof(proof, ProjectRemovalProof, proof.target_id, proof.revision)
    if type(receipt) is not ProjectRemovalReceipt or receipt.proof != proof:
        raise UnsafeRemoval('actual project receipt does not match prepared proof')
    _observation(receipt.scheduler, now)
    if type(receipt.remaining_jobs_including_stopped) is not tuple or receipt.remaining_jobs_including_stopped:
        raise UnsafeRemoval('fresh complete scheduler inspection must show no jobs, including stopped jobs')


def validate_node_receipt(receipt, proof, now):
    if type(proof) is not NodeRemovalProof:
        raise UnsafeRemoval('typed node removal proof is required')
    validate_proof(proof, NodeRemovalProof, proof.target_id, proof.revision)
    if type(receipt) is not NodeRemovalReceipt or receipt.proof != proof:
        raise UnsafeRemoval('actual node receipt does not match prepared proof')
    _observation(receipt.scheduler, now)
    _observation(receipt.executor, now)
    if (type(receipt.scheduler_occupants) is not tuple or receipt.scheduler_occupants
            or type(receipt.executor_occupants) is not tuple or receipt.executor_occupants):
        raise UnsafeRemoval('fresh complete scheduler and executor inspection must show no occupants')
