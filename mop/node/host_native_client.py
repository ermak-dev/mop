"""NodeUID initial-HOST executor и независимый finite lifetime telemetry worker.

Не complete NativeBindings; generic machine serve остаётся закрытым. aclose
закрывает только frontend HTTP/socket tasks, никогда root/body/native lifetime.
"""
import asyncio

from mop.node.host_native import ModelInputs, NativeSubmission, ProviderProfile
from mop.node.host_native_lifetime import execute_proxy
from mop.node.host_phase_client import PlannedExecutor, _Owned
from mop.node.host_phases import Projection, admitted
from mop.node.host_scope import Reference, canonical
from mop.node.http_execution import Admission, Refused, Unavailable


class LifetimeWorker:
    def __init__(self, client, backend, scope, attempt):
        self.client, self.backend, self.scope, self.attempt = client, backend, scope, canonical(attempt)
        self.task = self.cleanup = None
        self.closed = False
        self.last_error = None

    async def exchange(self, request):
        async with self.backend.connect() as session:
            return await session.native_mailbox(request)

    def address(self, action):
        return dict(schema_version=1, action=action, scope=self.scope.wire(), attempt_id=self.attempt)

    async def run(self):
        while not self.closed:
            try:
                value = await self.exchange(self.address('native_poll'))
                operation = value['operation']
                if operation is None:
                    await asyncio.sleep(.025)
                    continue
                answer = error = None
                try:
                    answer = await execute_proxy(self.client, operation)
                except (Refused, Unavailable, OSError, TimeoutError, ValueError) as failure:
                    error = str(failure) if isinstance(failure, (Refused, Unavailable)) else type(failure).__name__
                await self.exchange(dict(**self.address('native_complete'), id=operation['id'],
                    ticket=operation['ticket'], answer=answer, error=error))
                self.last_error = error
            except (Refused, Unavailable, OSError, TimeoutError, ValueError) as error:
                # Root/cgroup не принадлежит этому frontend. Retry только finite
                # observation/оригинальной записи, не native exec/producer mint.
                self.last_error = str(error) if isinstance(error, (Refused, Unavailable)) else type(error).__name__
                await asyncio.sleep(.1)

    def start(self):
        if self.task is not None or self.closed:
            raise Refused('native lifetime worker cannot be restarted in place')
        self.task = asyncio.create_task(self.run())

    async def _close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def aclose(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        await asyncio.shield(self.cleanup)


class InitialHostExecutor(PlannedExecutor):
    """Три existing planned phases плюс fixed initial native; не все ACTIONS."""
    def __init__(self, client, backend, writer, *, profile):
        super().__init__(client, backend, writer)
        if type(profile) is not ProviderProfile:
            raise Refused('explicit installed native provider profile required')
        self.profile = profile
        self.workers = {}

    def start_worker(self, owned):
        key = owned.scope, owned.admission.attempt_id
        worker = self.workers.get(key)
        if worker is None:
            if len(self.workers) >= 256:
                raise Refused('native lifetime worker capacity exhausted')
            worker = LifetimeWorker(self.client, self.backend, *key)
            self.workers[key] = worker
            worker.start()
        return worker

    async def execute(self, work, guard):
        if work.action != 'native_exec':
            return await super().execute(work, guard)
        admission = admitted(guard, work)
        if admission.attempt_id in self._owned:
            raise Refused('native execution replay forbidden; recover original retained attempt')
        scope = self.scope(work)
        previous = self._references.get(scope)
        if type(previous) is not Reference or previous.scope != scope:
            raise Unavailable('native requires explicitly observed original committed body reference')
        await guard()
        value = await self.client.read_initial_launch(work.wire())
        guard.check()
        models = ModelInputs.parse(value, self.profile)
        staged = self.writer.stage(admission, scope, value)
        owned = _Owned(admission, scope, staged.descriptor, staged=staged)
        self._owned[admission.attempt_id] = owned
        try:
            submission = NativeSubmission(admission, staged.descriptor, Projection.from_delivery(value), previous, models.pins())
            submission.encode()
            async with self.backend.connect() as session:
                guard.check()
                owned.possible_dispatch = True
                phase = await session.planned(submission)
        except BaseException:
            if not owned.possible_dispatch:
                owned.definite_failure = True
                self.writer.cleanup(staged)
                self._owned.pop(admission.attempt_id)
            raise
        self.start_worker(owned)
        return await self._drive(owned, guard, phase)

    async def resume(self, work, guard):
        if work.action != 'native_exec':
            return await super().resume(work, guard)
        admission = admitted(guard, work)
        owned = self._owned.get(admission.attempt_id)
        if owned is not None and (owned.admission is not admission or owned.succeeded):
            raise Refused('native original Admission cannot be replaced or reexecuted')
        if owned is None:
            owned = _Owned(admission, self.scope(work))
        phase = await self.exchange(self.address(owned))
        self.same_admission(phase, admission)
        self._owned[admission.attempt_id] = owned
        self.start_worker(owned)
        return await self._drive(owned, guard, phase)

    async def recover_result(self, admission):
        if type(admission) is not Admission:
            raise Refused('original Admission required for result recovery')
        if admission.work.action == 'native_exec':
            return await self.recover_native_result(admission)
        return await super().recover_result(admission)

    async def recover_native_result(self, admission):
        if admission.work.action != 'native_exec':
            raise Refused('original native Admission required for result recovery')
        owned = _Owned(admission, self.scope(admission.work))
        phase = await self.exchange(self.address(owned))
        self.same_admission(phase, admission)
        if phase['status'] != 'succeeded':
            raise Unavailable('original native effect has not actually completed')
        owned.phase_receipt, owned.succeeded = canonical(phase['receipt']), True
        reference = Reference.parse(phase['reference'])
        if reference.scope != owned.scope:
            raise Refused('native recovery original physical reference changed')
        self._owned[admission.attempt_id] = owned
        self._references[owned.scope] = reference
        self.start_worker(owned)
        if not phase['acknowledged']:
            committed = await self.client.result(admission, 'succeeded', 'native-started', receipt_id=owned.phase_receipt)
            await self.acknowledge(committed)

    async def aclose(self):
        await asyncio.gather(*(worker.aclose() for worker in self.workers.values()))
        await super().aclose()
