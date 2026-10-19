"""Общие startup unit ports, без подмены настоящего TLS transport.

HYPOTHESIS: host_backend импортировал runnable native_startup ради двух ports,
нарушая retained tests_lib gate (#675).
SOLUTION: общая discovery fixture и ports перенесены без изменения; оба
consumer используют helper, все TLS/cancellation assertions остаются на месте.
RESULT: tests_lib и оба consumer GREEN; PG17/HTTPS startup boundary GREEN.
STATUS: FIXED — see #675
"""
import asyncio
from uuid import uuid4

from mop.node.http_execution import Unavailable

NODE, EPOCH, PREVIOUS = (str(uuid4()) for _ in range(3))


def state(**change):
    return dict(schema_version=1, installation_id='fixture', epoch=EPOCH,
                node_id=NODE, executor_epoch=PREVIOUS, **change)


class Client:
    """Unit port; TLS checks не подменяют MachineClient/transport."""
    def __init__(self, value=None):
        self.value = state() if value is None else value
        self.calls = []
        self.closed = False
        self.fail_restart = False

    async def executor_state(self, node):
        self.calls.append(('discover', node))
        return self.value

    async def restarted(self, node, previous, new):
        self.calls.append(('restart', node, previous, new))
        if self.fail_restart:
            raise Unavailable('restart reply lost')

    async def poll(self, node, executor, limit):
        self.calls.append(('poll', node, executor, limit))
        return ()

    async def aclose(self):
        self.closed = True


class Executor:
    def __init__(self, calls):
        self.calls = calls
        self.quiet = True
        self.closed = False
        self.block = False

    async def reconcile(self, node, previous, new):
        self.calls.append(('reconcile', node, previous, new))
        if self.block:
            await asyncio.Event().wait()
        return self.quiet

    async def execute(self, *args):
        raise AssertionError('no effect in a startup test')

    async def aclose(self):
        self.closed = True
