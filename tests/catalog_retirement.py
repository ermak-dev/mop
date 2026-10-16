"""Чистая retirement policy: unknown никогда не означает free #634."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import uuid

import _lib
import hermetic


# HYPOTHESIS: непрозрачная self-assertion без адресного receipt не доказывает
# отсутствие stopped jobs/occupants и свежесть обоих источников.
# SPEC: finite typed proofs, nonfuture bounded observations, exact target/revision.
# SOLUTION: чистая finite policy проверяет typed receipt, адрес и оба источника.
# RESULT: malformed/unknown/stale evidence отказывает, только complete empty проходит.
# STATUS: FIXED — see #634
def main():
    name = 'mop.server.catalog.retirement'
    assert importlib.util.find_spec(name) is not None, 'missing pure safe-retirement proof policy'
    from mop.server.catalog.retirement import (SourceObservation, ProjectRemovalProof,
        NodeRemovalProof, ProjectRemovalReceipt, NodeRemovalReceipt, UnsafeRemoval,
        validate_project_receipt, validate_node_receipt)
    now = datetime.now(timezone.utc)
    proof = ProjectRemovalProof(uuid.uuid4(), 1, uuid.uuid4())
    observation = SourceObservation('scheduler', uuid.uuid4(), 1, 'receipt:1', now, now + timedelta(seconds=30))
    project = ProjectRemovalReceipt(proof, observation, ())
    validate_project_receipt(project, proof, now)

    def refused(operation):
        try:
            operation()
        except UnsafeRemoval:
            pass
        else:
            raise AssertionError('unsafe retirement evidence accepted')

    for bad in (None, {}, True, replace(project, remaining_jobs_including_stopped=None),
                replace(project, remaining_jobs_including_stopped=('stopped',)),
                replace(project, remaining_jobs_including_stopped=[]),
                replace(project, proof=replace(proof, target_id=uuid.uuid4())),
                replace(project, proof=replace(proof, revision=2)),
                replace(project, proof=replace(proof, receipt_id=uuid.uuid4()))):
        refused(lambda: validate_project_receipt(bad, proof, now))
    for bad in (replace(observation, source=''), replace(observation, source_incarnation=None),
                replace(observation, sequence=0), replace(observation, sequence=True),
                replace(observation, receipt=''), replace(observation, observed_at=None),
                replace(observation, observed_at=now.replace(tzinfo=None)),
                replace(observation, observed_at=now + timedelta(seconds=1)),
                replace(observation, expires_at=now),
                replace(observation, observed_at=now-timedelta(minutes=10)),
                replace(observation, expires_at=now+timedelta(minutes=6))):
        refused(lambda: validate_project_receipt(replace(project, scheduler=bad), proof, now))
    for bad in (None, {}, True, replace(proof, revision=True), replace(proof, revision=0),
                replace(proof, receipt_id=None), replace(proof, target_id=None)):
        refused(lambda: validate_project_receipt(project, bad, now))
    node_proof = NodeRemovalProof(uuid.uuid4(), 1, uuid.uuid4())
    node = NodeRemovalReceipt(node_proof, observation, replace(observation, source='node-executor'), (), ())
    validate_node_receipt(node, node_proof, now)
    for bad in (None, {}, True, replace(node_proof, revision=True), replace(node_proof, receipt_id=None)):
        refused(lambda: validate_node_receipt(node, bad, now))
    for bad in (None, project, replace(node, scheduler_occupants=None),
                replace(node, executor_occupants=None), replace(node, scheduler_occupants=('allocation',)),
                replace(node, executor_occupants=('stopped-body',)),
                replace(node, executor=replace(observation, expires_at=now)),
                replace(node, proof=replace(node_proof, target_id=uuid.uuid4()))):
        refused(lambda: validate_node_receipt(bad, node_proof, now))


if __name__ == '__main__':
    main()
