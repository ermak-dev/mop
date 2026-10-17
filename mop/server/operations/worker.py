"""Runtime владеет всем эффектом до durable результата, не HTTP caller."""
class Worker:
    """Порт NodeExecutor: managed async set_eligibility(Claim) -> bool confirmation.

    Production adapter не задан по умолчанию: #639 связывает receipt с настоящим
    node/incarnation fence. Fake доступен только composition теста, не endpoint.
    """
    def __init__(self, runtime, operations, node_executor, *, image_builds=None, node_id=None):
        self.runtime, self.operations, self.node_executor = runtime, operations, node_executor
        self.image_builds, self.node_id = image_builds, node_id
        # scheduler_stop входит в capability tuple только с bound stops портом;
        # прочие lane конфигурируются своими портами, как в #678.
        self.handlers = tuple(name for name, port in (
            ('node_eligibility', node_executor), ('image_build', image_builds),
            ('scheduler_stop', getattr(operations, 'stops', None) or False),
            ('scheduler_delete', getattr(operations, 'deletes', None) or False)) if port is not None)

    async def run_one(self, operation_id):
        if not self.runtime.accepting:
            return None
        return await self.runtime.effect(self._execute, operation_id)

    async def _execute(self, operation_id):
        if not self.runtime.accepting:
            return None
        handler = await self.runtime.database(self.operations.handler, operation_id)
        if handler not in self.handlers:
            raise RuntimeError(f'controller worker handler {handler} is not configured')
        if handler in ('scheduler_stop', 'scheduler_delete'):
            lane = self.operations.stops if handler == 'scheduler_stop' else self.operations.deletes
            if lane is None:
                return None
            return await self.runtime.blocking(self._scheduler, lane, operation_id)
        if handler == 'image_build':
            return await self.runtime.blocking(self._build, operation_id)
        claim = await self.runtime.database(self.operations.claim, operation_id)
        if claim is None:
            return None
        claim = await self.runtime.database(self.operations.prepare_effect, claim)
        if claim is None:
            return None
        try:
            applied = await self.node_executor.set_eligibility(claim)
            if type(applied) is not bool:
                raise TypeError('node executor must return typed confirmation')
        except Exception:
            # Raw adapter exceptions не попадают в durable metadata.
            return await self.runtime.database(self.operations.uncertain, claim, expected_revision=claim.revision)
        return await self.runtime.database(self.operations.complete, claim,
            expected_revision=claim.revision, applied=applied)

    def _scheduler(self, lane, operation_id):
        from django.db import connections
        try:
            return lane.run(operation_id)
        finally:
            connections.close_all()

    def _build(self, operation_id):
        from django.db import connections
        try:
            return self.image_builds.run(operation_id)
        finally:
            connections.close_all()

    async def run_pending(self, *, limit=8):
        """Один bounded проход; процессный lifecycle/poll interval задаёт root."""
        if not 1 <= limit <= 8:
            raise ValueError('worker batch exceeds controller safety caps')
        import asyncio
        if not self.handlers:
            raise RuntimeError('controller worker capabilities are not configured')
        if not self.runtime.accepting:
            return []
        ids = await self.runtime.database(self.operations.pending, limit=limit,
            handlers=self.handlers, node_id=self.node_id)
        return await asyncio.gather(*(self.run_one(operation_id) for operation_id in ids))
