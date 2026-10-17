"""Stop-job process manager: общий журнал/claims, один conditional внешний write."""
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from mop.server.access.services import current_actor, Denied
from . import models as m
from .commands import StopJobCommand
from .lifecycle import State
from .puppet import RestartTarget
from .requests import resolve_retry, RequestRefused, Retry
from .scheduler import JobPin, StopTicket, StopProof, SchedulerUnavailable
from .service import Refused, _independent, _binding, _actor, _snapshot, _event, _move, _ACTIVE


def digest(command):
    value = {key:str(item) for key,item in asdict(command.intent).items() if key!='request_id'}
    value['action'] = 'stop_job'
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class StopJobs:
    def __init__(self, operations, *, scheduler=None, targets=None):
        self.ops, self.scheduler, self.targets = operations, scheduler, targets

    def _ports(self):
        if (self.scheduler is None or self.targets is None or type(self.scheduler.scheduler_id) is not UUID
                or not {'conditional_stop_job','inspect_stop_job'} <= self.scheduler.capabilities):
            raise Refused('controller_unavailable',safe_reason='conditional stop-job scheduler is unavailable')

    def _authorize(self, actor, target):
        self.ops.access.authorize(actor,'operations.stop_puppet_job',project_ids=(target.project_id,))
        if actor.human_owner_id!=target.owner_id:
            raise Denied('stop job belongs to another human assignment')
        self.targets.authorize(target)

    def _target(self, target_id):
        value = self.targets.locked(target_id)
        if type(value) is not RestartTarget:
            raise Denied('current assignment is unavailable')
        return value

    def _enrollment(self, installation, target):
        row = m.ScheduledPuppet.objects.select_for_update().filter(target_id=target.target_id).first()
        if (row is None or row.epoch!=installation.epoch or row.restore_generation!=installation.generation
                or row.incarnation_id!=target.incarnation_id
                or row.scheduler_id!=self.scheduler.scheduler_id):
            raise Refused('controller_unavailable',safe_reason='current scheduler job is not enrolled')
        return row

    def enroll(self, actor, target_id, *, job_id, namespace):
        """Trusted Root enrollment; внешний complete read до SQL, второй auth после."""
        _independent()
        self._ports()
        with self.ops._transaction():
            installation = self.ops._installation()
            target = self._target(target_id)
            self._authorize(actor,target)
            pin = JobPin(installation.epoch,target,self.scheduler.scheduler_id,job_id,namespace,1,installation.generation)
        index = self.scheduler.observe(pin)
        with self.ops._transaction():
            installation = self.ops._installation(pin.epoch)
            current = self._target(target_id)
            self._authorize(actor,current)
            if current!=target or installation.generation!=pin.restore_generation:
                raise Refused('revision_conflict')
            if m.ScheduledPuppet.objects.filter(target_id=target_id).exists():
                raise Refused('revision_conflict')
            m.ScheduledPuppet.objects.create(target_id=target_id,incarnation_id=target.incarnation_id,
                epoch=installation.epoch,restore_generation=installation.generation,
                scheduler_id=pin.scheduler_id,job_id=job_id,namespace=namespace,modify_index=index)

    def submit(self, actor, command):
        if type(command) is not StopJobCommand:
            raise ValueError('typed stop-job command required')
        _independent()
        intent = command.intent
        with self.ops._transaction():
            installation = self.ops._installation(intent.epoch)
            current_actor(actor,grants=self.ops.grants)
            # Replay не требует всё ещё запущенного job, но требует актуальных read rights.
            target = self.targets.locked(intent.target_id) if self.targets is not None else None
            old = m.Operation.objects.filter(actor_id=actor.user_id,request_id=intent.request_id).first()
            try:
                retry = resolve_retry(expected_epoch=intent.epoch,current_epoch=installation.epoch,
                    submitted_digest=digest(command),stored_digest=old.digest if old else None,
                    result_available=not old.result_pruned if old else True)
            except RequestRefused as error:
                raise Refused(error.code) from None
            if retry is Retry.EXPIRED:
                raise Refused('result_expired')
            if retry is Retry.REPLAY:
                return self.ops._disclose(actor,old)
            self._ports()
            if type(target) is not RestartTarget:
                raise Denied('current assignment is unavailable')
            self.ops.access.authorize(actor,'operations.view_operation',project_ids=(target.project_id,))
            self._authorize(actor,target)
            enrollment = self._enrollment(installation,target)
            if not installation.effects_enabled:
                raise Refused('effects_disabled')
            now = timezone.now()
            if intent.not_after<=now:
                raise Refused('deadline_expired')
            if intent.not_after>now+timedelta(minutes=5):
                raise Refused('deadline_horizon')
            if intent.expected_revision!=target.revision or intent.incarnation!=target.incarnation_id:
                raise Refused('revision_conflict')
            active = m.Operation.objects.filter(epoch=installation.epoch,state__in=_ACTIVE)
            if active.count()>=self.ops.queue_limit or active.filter(projects=target.project_id).count()>=self.ops.project_limit:
                raise Refused('admission_limit')
            row = m.Operation.objects.create(actor_id=actor.user_id,actor_binding=_binding(actor),request_id=intent.request_id,
                digest=digest(command),epoch=intent.epoch,restore_generation=installation.generation,action='stop_job',
                node_id=target.node_id,target_kind='puppet',target_id=target.target_id,incarnation_id=target.incarnation_id,
                expected_revision=target.revision,eligible=None,not_after=intent.not_after)
            row.projects.set((target.project_id,))
            values = {key:getattr(target,key) for key in ('project_id','executor_epoch','owner_id','generation','role','bundle_digest','grant_id')}
            m.StopBinding.objects.create(operation=row,**values,scheduler_id=enrollment.scheduler_id,
                job_id=enrollment.job_id,namespace=enrollment.namespace,modify_index=enrollment.modify_index)
            m.Step.objects.create(operation=row,name='scheduler_stop')
            _event(row)
            result = _snapshot(row)
        return result

    def _ticket(self, row, step):
        pin = row.stopbinding
        target = RestartTarget(row.target_id,pin.project_id,row.node_id,row.incarnation_id,pin.executor_epoch,
            pin.owner_id,pin.role,pin.bundle_digest,pin.grant_id,pin.generation,row.expected_revision)
        return StopTicket(JobPin(row.epoch,target,pin.scheduler_id,pin.job_id,pin.namespace,pin.modify_index,row.restore_generation),
            row.id,step.token,row.not_after)

    def _locked(self, operation_id):
        # Совпадает с machine execution: restore -> Assignment -> Operation -> Step.
        installation = self.ops._installation()
        target_id = m.Operation.objects.values_list('target_id',flat=True).get(pk=operation_id,action='stop_job')
        target = self.targets.locked(target_id)
        row = m.Operation.objects.select_for_update().get(pk=operation_id,action='stop_job')
        step = m.Step.objects.select_for_update().get(operation=row)
        if row.epoch!=installation.epoch or row.restore_generation!=installation.generation:
            raise Refused('epoch_conflict')
        return installation,target,row,step

    def _validate(self, installation, target, row, step):
        if not installation.effects_enabled:
            raise Refused('effects_disabled')
        if row.not_after<=timezone.now():
            raise Refused('deadline_expired')
        pin = self._ticket(row,step).job
        if target!=pin.target:
            raise Refused('revision_conflict')
        self._authorize(_actor(row),target)
        enrollment = self._enrollment(installation,target)
        if (enrollment.job_id,enrollment.namespace,enrollment.modify_index)!=(pin.job_id,pin.namespace,pin.modify_index):
            raise Refused('revision_conflict')

    def claim(self, operation_id):
        _independent()
        self._ports()
        with self.ops._transaction():
            installation,target,row,step = self._locked(operation_id)
            if row.state!='accepted' or step.token is not None:
                return None
            try:
                self._validate(installation,target,row,step)
            except (Denied,Refused) as error:
                _move(row,State.REFUSED,'execution_refused',expected_revision=row.revision,safe_reason=str(error))
                return None
            keys = sorted(('node:'+str(row.node_id),'project:'+str(row.stopbinding.project_id),
                'puppet:'+str(row.target_id),'target:'+str(row.target_id)))
            for key in keys:
                m.Resource.objects.get_or_create(key=key)
            tuple(m.Resource.objects.select_for_update().filter(pk__in=keys).order_by('pk'))
            if m.ResourceClaim.objects.filter(resource_id__in=keys,released_at=None).exists():
                return None
            if m.Operation.objects.filter(state__in=('running','reconciling','unknown'),resourceclaim__released_at=None).distinct().count()>=self.ops.execution_limit:
                return None
            step.token = step.attempt_id = uuid4()
            step.started_at = timezone.now()
            step.lease_until = min(row.not_after,timezone.now()+timedelta(seconds=30))
            step.save(update_fields=('token','attempt_id','started_at','lease_until'))
            for key in keys:
                m.ResourceClaim.objects.create(resource_id=key,operation=row,step=step,token=step.token)
            _move(row,State.RUNNING,'effect_claimed',expected_revision=row.revision)
            return self._ticket(row,step)

    def prepare(self, ticket):
        _independent()
        with self.ops._transaction():
            installation,target,row,step = self._locked(ticket.operation_id)
            if self._ticket(row,step)!=ticket or row.state!='running' or step.dispatched_at is not None:
                return False
            try:
                self._validate(installation,target,row,step)
            except (Denied,Refused) as error:
                _move(row,State.REFUSED,'execution_refused',expected_revision=row.revision,safe_reason=str(error))
                m.ResourceClaim.objects.filter(operation=row,released_at=None).update(released_at=timezone.now())
                return False
            step.dispatched_at = timezone.now()
            step.save(update_fields=('dispatched_at',))
            return True

    def record(self, ticket, proof):
        _independent()
        if type(proof) is not StopProof:
            raise ValueError('typed physical stop proof required')
        with self.ops._transaction():
            _,target,row,step = self._locked(ticket.operation_id)
            if self._ticket(row,step)!=ticket:
                raise Refused('claim_conflict')
            if step.quiescent_at is not None:
                if step.outcome==proof.outcome and step.evidence==proof.evidence:
                    return _snapshot(row)
                raise Refused('claim_conflict')
            if row.state not in ('running','unknown','reconciling'):
                raise Refused('operation_conflict')
            if proof.outcome=='unknown':
                step.outcome,step.evidence = proof.outcome,proof.evidence
                step.save(update_fields=('outcome','evidence'))
                if row.state=='running':
                    _move(row,State.RECONCILING,'effect_uncertain',expected_revision=row.revision)
                    _move(row,State.UNKNOWN,'outcome_unknown',expected_revision=row.revision)
                return _snapshot(row)
            if row.state=='unknown':
                _move(row,State.RECONCILING,'late_confirmation',expected_revision=row.revision)
            applied = proof.outcome=='succeeded'
            if applied:
                # Не отзывать другое назначение при позднем historical receipt.
                pin = row.stopbinding
                pin.applied_revision = self.targets.stopped(ticket.job.target)
                pin.save(update_fields=('applied_revision',))
            step.outcome,step.evidence,step.quiescent_at = proof.outcome,proof.evidence,timezone.now()
            step.save(update_fields=('outcome','evidence','quiescent_at'))
            m.EffectReceipt.objects.create(step=step,token=ticket.token,epoch=row.epoch,node_id=row.node_id,
                source_revision=row.expected_revision,decision_revision=row.revision,applied=applied)
            _move(row,State.SUCCEEDED if applied else State.FAILED,
                'effect_confirmed' if applied else 'conditional_refusal',expected_revision=row.revision)
            m.ResourceClaim.objects.filter(operation=row,released_at=None).update(released_at=step.quiescent_at)
            return _snapshot(row)

    def run(self, operation_id):
        """Blocking worker entry; Root вызывает в Runtime.blocking, не HTTP handler."""
        ticket = self.claim(operation_id)
        if ticket is None:
            return None
        # Read-only preflight перед последней актуальной SQL проверкой.
        try:
            index = self.scheduler.observe(ticket.job)
        except SchedulerUnavailable:
            return self.record(ticket,StopProof('unknown','scheduler-uncertain'))
        if index!=ticket.job.modify_index:
            return self.record(ticket,StopProof('failed','conditional-refusal'))
        if not self.prepare(ticket):
            return None
        return self.record(ticket,self.scheduler.stop_job(ticket))

    def reconcile(self, actor, operation_id):
        """Только актуальное recovery право; expired intent не допускает новый write."""
        _independent()
        self._ports()
        with self.ops._transaction():
            _,_,row,step = self._locked(operation_id)
            self.ops.access.authorize(actor,'operations.recover_operation',project_ids=tuple(row.projects.values_list('pk',flat=True)))
            if row.state not in ('running','unknown','reconciling') or step.token is None:
                raise Refused('operation_conflict')
            ticket = self._ticket(row,step)
        return self.record(ticket,self.scheduler.inspect_stop(ticket))
