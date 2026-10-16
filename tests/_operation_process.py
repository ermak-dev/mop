"""Spawn targets: framework запускается после выбора isolated test database."""

def _configure(database, bootstrap):
    import os
    # hermetic в spawn main очищает MOP_*; путь fixture передаётся явно.
    os.environ['MOP_CONTROLLER_BOOTSTRAP'] = bootstrap
    from mop.server.controller.runtime import configure
    configure()
    from django.db import connections
    connections.databases['default']['NAME'] = database


def read(database, bootstrap, actor, operation_id, result):
    _configure(database, bootstrap)
    from django.db import connections
    from mop.server.controller.runtime import operations_service
    try:
        result.put(operations_service().status(actor, operation_id))
    finally:
        connections.close_all()


def worker(database, bootstrap, executor, ready, result):
    """Самостоятельный worker process: fixture effect, настоящий root и SQL."""
    _configure(database, bootstrap)
    import asyncio
    from mop.server.controller.runtime import Runtime, compose_worker, run_worker
    async def run():
        stop = asyncio.Event()
        class Resource:
            async def start(self):
                ready.set()
            async def aclose(self):
                pass
        class Node:
            async def set_eligibility(self, claim):
                stop.set()
                await asyncio.sleep(.02)
                return True
        runtime = Runtime(adapters=(Resource(),))
        consumer = compose_worker(runtime, node_executor=Node(), executor=executor)
        await asyncio.wait_for(run_worker(stop, runtime, worker=consumer, poll_interval=.01), 10)
        result.put(runtime.pending)
    asyncio.run(run())


def claim(database, bootstrap, operation_id, ready, result):
    _configure(database, bootstrap)
    from django.db import connections
    from mop.server.controller.runtime import operations_service
    try:
        ready.wait(timeout=10)
        result.put(operations_service().claim(operation_id))
    finally:
        connections.close_all()
