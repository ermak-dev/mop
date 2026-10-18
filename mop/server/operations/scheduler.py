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
import socket
import ssl
import threading
import time
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .puppet import RestartTarget
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

from mop.common.domain import Alloc


def _alloc(row):
    """Строка ответа /allocations -> domain.Alloc (#377); мусор -- None, то есть отказ."""
    if type(row) is not dict:
        return None
    try:
        return Alloc.from_dict(row)
    except ValueError:
        return None


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
                ('job-quiescent','job-purged','conditional-refusal','scheduler-uncertain')):
            raise ValueError('finite scheduler proof required')


@dataclass(frozen=True)
class RegisterTicket:
    """Условная регистрация job тела (#643): create (expected_index 0 -- job
    нет) или update (текущий индекс). job -- уже отрисованная spec, её Meta
    обязана нести адресные пины цели; иначе остановка не узнала бы job."""
    epoch: UUID
    target: 'RestartTarget'
    scheduler_id: UUID
    job_id: str
    namespace: str
    restore_generation: UUID
    expected_index: int
    job: dict
    operation_id: UUID
    token: UUID
    not_after: datetime
    predecessor: JobPin | None = None

    def __post_init__(self):
        if (any(type(value) is not UUID for value in (self.epoch, self.scheduler_id, self.restore_generation,
                                                       self.operation_id, self.token))
                or any(type(value) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is None
                       for value in (self.job_id, self.namespace))
                or type(self.expected_index) is not int or self.expected_index < 0
                or type(self.not_after) is not datetime or self.not_after.utcoffset() is None
                or type(self.job) is not dict or self.job.get('ID') != self.job_id
                or type(self.job.get('Meta')) is not dict
                or any(self.job['Meta'].get(key) != value for key, value in
                       addressed_metadata(self.epoch, self.restore_generation, self.target).items())
                or any(key.startswith('mop.stop_') for key in self.job['Meta'])
                or any(key in self.job for key in ('Stop', 'JobModifyIndex', 'Namespace'))):
            raise ValueError('typed addressed registration required')
        predecessor = self.predecessor
        if self.expected_index == 0:
            if predecessor is not None:
                raise ValueError('create registration cannot name a predecessor')
        elif (type(predecessor) is not JobPin or
                (predecessor.epoch, predecessor.restore_generation, predecessor.scheduler_id,
                 predecessor.namespace, predecessor.job_id, predecessor.modify_index, predecessor.target.target_id) !=
                (self.epoch, self.restore_generation, self.scheduler_id, self.namespace,
                 self.job_id, self.expected_index, self.target.target_id)):
            raise ValueError('exact enrolled registration predecessor required')


@dataclass(frozen=True)
class RegisterProof:
    outcome: str
    evidence: str
    modify_index: int | None = None

    def __post_init__(self):
        if (self.outcome not in ('succeeded','failed','unknown')
                or self.evidence not in ('job-registered','conditional-refusal','scheduler-uncertain')
                or (self.outcome == 'succeeded') != (type(self.modify_index) is int and self.modify_index >= 1)):
            raise ValueError('finite registration proof required')


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
    def delete_job(self, ticket: StopTicket) -> StopProof: ...
    def inspect_delete(self, ticket: StopTicket) -> StopProof: ...


ADDRESSED = ('target_id','incarnation_id','node_id','executor_epoch','owner_id',
             'generation','revision','role','bundle_digest','grant_id')


def addressed_metadata(epoch, restore_generation, target):
    """Несекретный точный authority binding внешнего job.

    Одна функция для регистрации нового тела и для его остановки/удаления
    (#643): иначе job, поднятый одним путём, читался бы другим как чужой."""
    return {'mop.epoch':str(epoch),'mop.restore_generation':str(restore_generation),
            **{'mop.'+name:str(getattr(target,name)) for name in ADDRESSED}}


def job_metadata(pin):
    """Внешняя spec содержит только несекретный точный authority binding."""
    return addressed_metadata(pin.epoch, pin.restore_generation, pin.target)


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
        # Регистрации инспектор тела не нужен: готовность тела подтверждает узел
        # своим шагом, планировщик доказывает только запись spec (#643).
        register = frozenset({'conditional_register_job','inspect_register_job'})
        return (register | frozenset({'conditional_stop_job','inspect_stop_job','conditional_delete_job'})
                if self.body_inspector is not None else register)

    def _request(self, method, path, namespace, body=None, *, all_allocations=False, purge=False, deadline=None):
        # Без redirects/proxy/env credentials; один bounded socket на вызов.
        # Budget ПУБЛИЧНОГО вызова (#680): deadline — monotonic предел всего
        # observe/stop_job/inspect_stop, а не каждого _request. Каждый новый
        # вложенный запрос получает только остаток, drip-ответ (каждое socket
        # ожидание под timeout) прерывается Timer+shutdown, финальная
        # monotonic проверка закрывает гонку shutdown-против-возврата.
        query = urlencode({'namespace':namespace,'consistent':'true', **({'all':'true'} if all_allocations else {}),
                           **({'purge':'true'} if purge else {})})
        cls = http.client.HTTPSConnection if self.origin.scheme=='https' else http.client.HTTPConnection
        options = {'context':self._ssl} if cls is http.client.HTTPSConnection else {}
        remaining = self.timeout if deadline is None else deadline-time.monotonic()
        if remaining <= 0:
            raise SchedulerUnavailable()
        connection = cls(self.origin.hostname,self.origin.port,timeout=min(self.timeout,remaining),**options)
        timer = None
        try:
            # DNS bounded: резолв hostname вне C-резолвера вызывающего потока.
            # IP-literal пропускает резолв (getaddrinfo всё равно лезет в файлы),
            # loopback/localhost валидированы конструктором. connection.host
            # остаётся hostname: SNI, Host header и проверка сертификата HTTPS
            # идут по нему; резолв подменяется только в адресе соединения.
            if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', self.origin.hostname):
                resolved = None
            else:
                resolved = self._resolve(self.origin.hostname, remaining)
            if resolved is not None:
                def dial(address, timeout, source_address=None, _resolved=resolved):
                    # connect стартует ПОСЛЕ резолва: остаток бюджета, а не
                    # срезанный на входе timeout, иначе connect жил бы за
                    # пределами бюджета, пока Timer ещё не заведён.
                    # #701: адреса имени -- по порядку; недоступный первый
                    # (::1 при IPv4-only peer) не прячет рабочий второй.
                    failure = SchedulerUnavailable()
                    for host in _resolved:
                        budget = timeout
                        if deadline is not None:
                            budget = min(timeout, deadline - time.monotonic())
                            if budget <= 0:
                                raise SchedulerUnavailable()
                        try:
                            return socket.create_connection((host, address[1]), budget, source_address)
                        except OSError as error:
                            failure = error
                    raise failure
                connection._create_connection = dial
            raw = None if body is None else json.dumps(body,separators=(',',':')).encode()
            connection.request(method,path+'?'+query,body=raw,
                headers={'X-Nomad-Token':self._token,'Content-Type':'application/json'})
            def abort():
                # shutdown будит getresponse/read даже при медленном drip body.
                sock = connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            timer = threading.Timer(max(.001, deadline-time.monotonic() if deadline is not None
                                         else self.timeout), abort)
            timer.daemon = True
            timer.start()
            response = connection.getresponse()
            raw = response.read(1048577)
            if len(raw)>1048576 or (deadline is not None and time.monotonic() >= deadline):
                raise SchedulerUnavailable()
            if response.status!=200:
                return response.status, None
            return response.status, json.loads(raw)
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            connection.close()

    def _resolve(self, hostname, remaining):
        """getaddrinfo в bounded потоке: медленный резолвер не держит вызов.
        -> все адреса имени по порядку резолвера, без повторов (#701)."""
        outcome = []
        def run():
            try:
                found = tuple(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(hostname, None)))
                outcome.append(found or None)
            except Exception:
                outcome.append(None)
        worker = threading.Thread(target=run,daemon=True)
        worker.start()
        worker.join(max(.001,remaining))
        if worker.is_alive() or not outcome or outcome[0] is None:
            raise SchedulerUnavailable()
        return outcome[0]

    def _job(self, job, *, deadline=None):
        if type(job) is not JobPin or job.scheduler_id!=self.scheduler_id:
            raise SchedulerUnavailable()
        status, value = self._request('GET','/v1/job/'+quote(job.job_id,safe=''),job.namespace,deadline=deadline)
        if (status!=200 or type(value) is not dict or value.get('ID')!=job.job_id
                or value.get('Namespace')!=job.namespace or type(value.get('JobModifyIndex')) is not int
                or type(value.get('Meta')) is not dict or type(value.get('Stop')) is not bool
                or any(value['Meta'].get(key)!=item for key,item in job_metadata(job).items())):
            raise SchedulerUnavailable()
        return value

    def observe(self, job):
        # Один monotonic budget на весь публичный вызов (#680).
        budget = time.monotonic() + self.timeout
        try:
            value = self._job(job, deadline=budget)
            if value['Stop'] or 'mop.stop_token' in value['Meta']:
                raise SchedulerUnavailable()
            return value['JobModifyIndex']
        except Exception:
            raise SchedulerUnavailable() from None

    def stop_job(self, ticket):
        if ('conditional_stop_job' not in self.capabilities or type(ticket) is not StopTicket
                or ticket.not_after <= datetime.now(timezone.utc)):
            return StopProof('failed','conditional-refusal')
        try:
            budget = time.monotonic() + self.timeout
            value = self._job(ticket.job, deadline=budget)
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
                {'Job':job,'EnforceIndex':True,'JobModifyIndex':ticket.job.modify_index},deadline=budget)
            # Nomad index conflict: атомарно отвергнут именно conditional write.
            if status==409:
                return StopProof('failed','conditional-refusal')
            if status!=200:
                return StopProof('unknown','scheduler-uncertain')
            return self.inspect_stop(ticket, deadline=budget)
        except Exception:
            # Ответ мог потеряться после внешнего COMMIT; нельзя повторять POST.
            return StopProof('unknown','scheduler-uncertain')

    def inspect_stop(self, ticket, *, deadline=None):
        # deadline не задан (reconcile) -- свой бюджет этого вызова (#680).
        try:
            budget = deadline or time.monotonic() + self.timeout
            value = self._job(ticket.job, deadline=budget)
            if (value['Stop'] is not True or value['JobModifyIndex']<=ticket.job.modify_index
                    or value['Meta'].get('mop.stop_operation')!=str(ticket.operation_id)
                    or value['Meta'].get('mop.stop_token')!=str(ticket.token)):
                raise SchedulerUnavailable()
            status, rows = self._request('GET','/v1/job/'+quote(ticket.job.job_id,safe='')+'/allocations',ticket.job.namespace,all_allocations=True,deadline=budget)
            # Нет allocations не является physical proof: GC/неполный ответ
            # не доказывает завершение процесса. all=true добавляется ниже.
            if status!=200 or type(rows) is not list or not rows:
                raise SchedulerUnavailable()
            allocs = [_alloc(row) for row in rows]
            for alloc in allocs:
                if (alloc is None or alloc.job!=ticket.job.job_id
                        or alloc.client_status not in ('complete','failed')
                        or alloc.desired_status not in ('stop','evict')):
                    raise SchedulerUnavailable()
            allocation_ids = tuple(sorted(alloc.id for alloc in allocs))
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
            if self._job(ticket.job,deadline=budget)!=value:
                raise SchedulerUnavailable()
            return StopProof('succeeded','job-quiescent')
        except Exception:
            return StopProof('unknown','scheduler-uncertain')

    def _absent(self, job, *, deadline):
        """Доказательство удаления: consistent GET того же job -- ровно 404."""
        status, _ = self._request('GET','/v1/job/'+quote(job.job_id,safe=''),job.namespace,deadline=deadline)
        return status == 404

    def delete_job(self, ticket):
        """#625 delete: условная остановка нашим токеном, затем purge только её.

        У Nomad нет условного deregister: purge отправляется лишь после нашей
        подтверждённой остановки и повторного consistent чтения, что job всё ещё
        помечен нашими operation/token. Остаётся окно между этим чтением и
        DELETE -- чужая перерегистрация в нём была бы снята; это документировано,
        а не скрыто. Потерянный ответ -- unknown; повтор DELETE не делается.
        """
        if ('conditional_delete_job' not in self.capabilities or type(ticket) is not StopTicket
                or ticket.not_after <= datetime.now(timezone.utc)):
            return StopProof('failed','conditional-refusal')
        try:
            budget = time.monotonic() + self.timeout
            if self._absent(ticket.job, deadline=budget):
                return StopProof('unknown','scheduler-uncertain')
            value = self._job(ticket.job, deadline=budget)
            ours = (value['Stop'] is True and value['Meta'].get('mop.stop_operation')==str(ticket.operation_id)
                    and value['Meta'].get('mop.stop_token')==str(ticket.token))
            if not ours:
                stopped = self.stop_job(ticket)
                if stopped.outcome != 'succeeded':
                    return stopped
                budget = time.monotonic() + self.timeout
                value = self._job(ticket.job, deadline=budget)
                if (value['Stop'] is not True or value['Meta'].get('mop.stop_operation')!=str(ticket.operation_id)
                        or value['Meta'].get('mop.stop_token')!=str(ticket.token)):
                    return StopProof('unknown','scheduler-uncertain')
            if ticket.not_after <= datetime.now(timezone.utc):
                return StopProof('failed','conditional-refusal')
            status, _ = self._request('DELETE','/v1/job/'+quote(ticket.job.job_id,safe=''),ticket.job.namespace,
                                      purge=True,deadline=budget)
            if status != 200:
                return StopProof('unknown','scheduler-uncertain')
            return (StopProof('succeeded','job-purged') if self._absent(ticket.job, deadline=budget)
                    else StopProof('unknown','scheduler-uncertain'))
        except Exception:
            # Ответ мог потеряться после внешнего purge; повтор DELETE запрещён.
            return StopProof('unknown','scheduler-uncertain')

    def inspect_delete(self, ticket):
        """Сверка только чтением: job снят -- succeeded; иначе неизвестно."""
        try:
            budget = time.monotonic() + self.timeout
            if self._absent(ticket.job, deadline=budget):
                return StopProof('succeeded','job-purged')
        except Exception:
            pass
        return StopProof('unknown','scheduler-uncertain')

    def _registered(self, ticket, *, deadline):
        """Consistent чтение job регистрации -> (status, value | None)."""
        status, value = self._request('GET','/v1/job/'+quote(ticket.job_id,safe=''),ticket.namespace,deadline=deadline)
        if status == 404:
            return 404, None
        if (status != 200 or type(value) is not dict or value.get('ID') != ticket.job_id
                or value.get('Namespace') != ticket.namespace or type(value.get('JobModifyIndex')) is not int
                or type(value.get('Meta')) is not dict):
            raise SchedulerUnavailable()
        return 200, value

    def register_job(self, ticket):
        """#643: одна условная запись spec тела и readback её точной Meta.

        expected_index 0 -- «job нет» (create никогда не перезаписывает),
        иначе -- текущий индекс (update). 409 -- атомарный отказ условия.
        Потерянный ответ после возможного COMMIT -- unknown, второй POST
        не делается; сверка -- inspect_register, только чтение.
        """
        if ('conditional_register_job' not in self.capabilities or type(ticket) is not RegisterTicket
                or ticket.scheduler_id != self.scheduler_id or ticket.not_after <= datetime.now(timezone.utc)):
            return RegisterProof('failed','conditional-refusal')
        try:
            budget = time.monotonic() + self.timeout
            status, value = self._registered(ticket, deadline=budget)
            current = 0 if status == 404 else value['JobModifyIndex']
            if current != ticket.expected_index:
                return RegisterProof('failed','conditional-refusal')
            if ticket.predecessor is not None and (
                    value.get('Stop') is not False
                    or any(value['Meta'].get(key) != item for key, item in job_metadata(ticket.predecessor).items())
                    or any(key.startswith('mop.stop_') for key in value['Meta'])):
                return RegisterProof('failed','conditional-refusal')
            # Preflight занял wall time: intent перепроверяется перед эффектом (#679).
            if ticket.not_after <= datetime.now(timezone.utc):
                return RegisterProof('failed','conditional-refusal')
            status, _ = self._request('POST','/v1/jobs',ticket.namespace,
                {'Job':{**copy.deepcopy(ticket.job),'Namespace':ticket.namespace},'EnforceIndex':True,
                 'JobModifyIndex':ticket.expected_index},deadline=budget)
            if status == 409:
                return RegisterProof('failed','conditional-refusal')
            if status != 200:
                return RegisterProof('unknown','scheduler-uncertain')
            return self.inspect_register(ticket, deadline=budget)
        except Exception:
            # Ответ мог потеряться после внешнего COMMIT; повтор POST запрещён.
            return RegisterProof('unknown','scheduler-uncertain')

    def inspect_register(self, ticket, *, deadline=None):
        """Сверка только чтением: наша точная Meta -- succeeded, иначе unknown.

        Даже старый/чужой job не доказывает отказ после возможного dispatch:
        исходный conditional POST ещё может прийти поздно. Только preflight
        или атомарный 409 доказывают отсутствие эффекта, не такое чтение."""
        try:
            budget = deadline if deadline is not None else time.monotonic() + self.timeout
            status, value = self._registered(ticket, deadline=budget)
            if status == 404:
                return RegisterProof('unknown','scheduler-uncertain')
            if (value['Meta'] == ticket.job['Meta'] and value.get('Stop') is not True
                    and value['JobModifyIndex'] > ticket.expected_index):
                return RegisterProof('succeeded','job-registered',value['JobModifyIndex'])
            return RegisterProof('unknown','scheduler-uncertain')
        except Exception:
            return RegisterProof('unknown','scheduler-uncertain')
