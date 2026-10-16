"""Узкий scheduler port stop-job: conditional register, никогда blind DELETE.

Job identity задаёт trusted enrollment. Секрет — runtime dependency адаптера,
не durable ticket, spec или audit. Unknown допускает только inspect.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import http.client
import json
import re
import ssl
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .puppet import RestartTarget
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID


class SchedulerUnavailable(ValueError):
    def __init__(self):
        super().__init__('conditional scheduler proof is unavailable')


@dataclass(frozen=True)
class JobPin:
    epoch: UUID
    target: 'RestartTarget'
    scheduler_id: UUID
    job_id: str
    namespace: str
    modify_index: int
    restore_generation: UUID

    def __post_init__(self):
        if (type(self.restore_generation) is not UUID or type(self.epoch) is not UUID or type(self.scheduler_id) is not UUID
                or any(type(value) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is None
                       for value in (self.job_id,self.namespace))
                or type(self.modify_index) is not int or self.modify_index < 1):
            raise ValueError('typed addressed scheduler job required')


@dataclass(frozen=True)
class StopTicket:
    job: JobPin
    operation_id: UUID
    token: UUID
    not_after: datetime


@dataclass(frozen=True)
class StopProof:
    outcome: str
    evidence: str

    def __post_init__(self):
        if (self.outcome not in ('succeeded','failed','unknown') or self.evidence not in
                ('job-quiescent','conditional-refusal','scheduler-uncertain')):
            raise ValueError('finite scheduler proof required')


@dataclass(frozen=True)
class BodyStopProof:
    operation_id: UUID
    token: UUID
    target_id: UUID
    incarnation_id: UUID
    node_id: UUID
    executor_epoch: UUID
    allocation_ids: tuple[str, ...]
    observed_at: datetime
    stopped: bool


class StopBodyInspector(Protocol):
    def inspect_stopped(self, ticket: StopTicket, allocation_ids: tuple[str, ...]) -> BodyStopProof: ...


class StopScheduler(Protocol):
    scheduler_id: UUID
    capabilities: frozenset[str]
    def observe(self, job: JobPin) -> int: ...
    def stop_job(self, ticket: StopTicket) -> StopProof: ...
    def inspect_stop(self, ticket: StopTicket) -> StopProof: ...


def job_metadata(pin):
    """Внешняя spec содержит только несекретный точный authority binding."""
    target = pin.target
    fields = ('target_id','incarnation_id','node_id','executor_epoch','owner_id',
              'generation','revision','role','bundle_digest','grant_id')
    return {'mop.epoch':str(pin.epoch),'mop.restore_generation':str(pin.restore_generation),
            **{'mop.'+name:str(getattr(target,name)) for name in fields}}


class NomadStopScheduler:
    def __init__(self, *, origin, scheduler_id, token, body_inspector=None, timeout=3, ssl_context=None):
        value = urlsplit(origin)
        if (value.scheme not in ('http','https') or value.username or value.password or
                value.path not in ('','/') or value.query or value.fragment or not value.hostname
                or value.scheme=='http' and value.hostname not in ('localhost','127.0.0.1','::1')
                or type(scheduler_id) is not UUID or type(token) is not str or not token
                or any(ord(char)<32 or ord(char)==127 for char in token) or not 0 < timeout <= 5):
            raise ValueError('trusted scheduler origin, identity and bounded transport required')
        self.origin, self.scheduler_id, self.timeout = value, scheduler_id, timeout
        self._token = token
        self._ssl = ssl_context or ssl.create_default_context()
        self.body_inspector = body_inspector

    @property
    def capabilities(self):
        return (frozenset({'conditional_stop_job','inspect_stop_job'}) if self.body_inspector is not None else frozenset())

    def _request(self, method, path, namespace, body=None, *, all_allocations=False):
        # Без redirects/proxy/env credentials; один bounded socket на вызов.
        query = urlencode({'namespace':namespace,'consistent':'true', **({'all':'true'} if all_allocations else {})})
        cls = http.client.HTTPSConnection if self.origin.scheme=='https' else http.client.HTTPConnection
        options = {'context':self._ssl} if cls is http.client.HTTPSConnection else {}
        connection = cls(self.origin.hostname,self.origin.port,timeout=self.timeout,**options)
        try:
            raw = None if body is None else json.dumps(body,separators=(',',':')).encode()
            connection.request(method,path+'?'+query,body=raw,headers={'X-Nomad-Token':self._token,'Content-Type':'application/json'})
            response = connection.getresponse()
            raw = response.read(1048577)
            if len(raw)>1048576:
                raise SchedulerUnavailable()
            if response.status!=200:
                return response.status, None
            return response.status, json.loads(raw)
        finally:
            connection.close()

    def _job(self, job):
        if type(job) is not JobPin or job.scheduler_id!=self.scheduler_id:
            raise SchedulerUnavailable()
        status, value = self._request('GET','/v1/job/'+quote(job.job_id,safe=''),job.namespace)
        if (status!=200 or type(value) is not dict or value.get('ID')!=job.job_id
                or value.get('Namespace')!=job.namespace or type(value.get('JobModifyIndex')) is not int
                or type(value.get('Meta')) is not dict or type(value.get('Stop')) is not bool
                or any(value['Meta'].get(key)!=item for key,item in job_metadata(job).items())):
            raise SchedulerUnavailable()
        return value

    def observe(self, job):
        try:
            value = self._job(job)
            if value['Stop'] or 'mop.stop_token' in value['Meta']:
                raise SchedulerUnavailable()
            return value['JobModifyIndex']
        except Exception:
            raise SchedulerUnavailable() from None

    def stop_job(self, ticket):
        if (not self.capabilities or type(ticket) is not StopTicket
                or ticket.not_after <= datetime.now(timezone.utc)):
            return StopProof('failed','conditional-refusal')
        try:
            value = self._job(ticket.job)
            if value['JobModifyIndex']!=ticket.job.modify_index or value['Stop']:
                return StopProof('failed','conditional-refusal')
            job = copy.deepcopy(value)
            job['Stop'] = True
            job['Meta'].update({'mop.stop_operation':str(ticket.operation_id),'mop.stop_token':str(ticket.token)})
            # Preflight GET занял живое wall time: intent перепроверяется
            # немедленно перед эффектом, не только на входе (#679). Истёкший
            # intent теряет силу оператора -- POST не уходит.
            if ticket.not_after <= datetime.now(timezone.utc):
                return StopProof('failed','conditional-refusal')
            status, _ = self._request('POST','/v1/jobs',ticket.job.namespace,
                {'Job':job,'EnforceIndex':True,'JobModifyIndex':ticket.job.modify_index})
            # Nomad index conflict: атомарно отвергнут именно conditional write.
            if status==409:
                return StopProof('failed','conditional-refusal')
            if status!=200:
                return StopProof('unknown','scheduler-uncertain')
            return self.inspect_stop(ticket)
        except Exception:
            # Ответ мог потеряться после внешнего COMMIT; нельзя повторять POST.
            return StopProof('unknown','scheduler-uncertain')

    def inspect_stop(self, ticket):
        try:
            value = self._job(ticket.job)
            if (value['Stop'] is not True or value['JobModifyIndex']<=ticket.job.modify_index
                    or value['Meta'].get('mop.stop_operation')!=str(ticket.operation_id)
                    or value['Meta'].get('mop.stop_token')!=str(ticket.token)):
                raise SchedulerUnavailable()
            status, rows = self._request('GET','/v1/job/'+quote(ticket.job.job_id,safe='')+'/allocations',ticket.job.namespace,all_allocations=True)
            # Нет allocations не является physical proof: GC/неполный ответ
            # не доказывает завершение процесса. all=true добавляется ниже.
            if status!=200 or type(rows) is not list or not rows:
                raise SchedulerUnavailable()
            for row in rows:
                if (type(row) is not dict or row.get('JobID')!=ticket.job.job_id
                        or row.get('ClientStatus') not in ('complete','failed')
                        or row.get('DesiredStatus') not in ('stop','evict')):
                    raise SchedulerUnavailable()
            allocation_ids = tuple(sorted(row['ID'] for row in rows))
            if len(set(allocation_ids))!=len(rows) or any(type(pk) is not str or not pk for pk in allocation_ids):
                raise SchedulerUnavailable()
            # Scheduler terminal status не доказывает конец вынесенной host session.
            # Узкий physical port должен подтвердить тот же node/incarnation/token.
            proof = self.body_inspector.inspect_stopped(ticket,allocation_ids)
            target = ticket.job.target
            now = datetime.now(timezone.utc)
            if (type(proof) is not BodyStopProof or proof.stopped is not True
                    or proof.operation_id!=ticket.operation_id or proof.token!=ticket.token
                    or proof.target_id!=target.target_id or proof.incarnation_id!=target.incarnation_id
                    or proof.node_id!=target.node_id or proof.executor_epoch!=target.executor_epoch
                    or proof.allocation_ids!=allocation_ids or type(proof.observed_at) is not datetime
                    or proof.observed_at.utcoffset() is None or not 0 <= (now-proof.observed_at).total_seconds() <= 10):
                raise SchedulerUnavailable()
            # Повторный consistent job read не позволяет принять смешанную spec.
            if self._job(ticket.job)!=value:
                raise SchedulerUnavailable()
            return StopProof('succeeded','job-quiescent')
        except Exception:
            return StopProof('unknown','scheduler-uncertain')
