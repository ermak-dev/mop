"""Общие fake OS fixtures для host_scope и host_backend, не containment proof.

HYPOTHESIS: импорт runnable host_scope ради Adapter запускает зависимость между
проверками и нарушает retained tests_lib gate (#675).
SOLUTION: прежние api/scope/installed/Adapter вынесены без изменения поведения;
оба consumer импортируют только этот helper, а не другую проверку.
RESULT: tests_lib и оба consumer GREEN; actual private systemd probe accepted.
STATUS: FIXED — see #675
"""
import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path
from uuid import uuid4


def api():
    assert importlib.util.find_spec('mop.node.host_scope') is not None, \
        'finite HOST containment boundary is missing (#719)'
    from mop.node import host_scope
    return host_scope


def scope():
    m = api()
    return m.Scope(*(str(uuid4()) for _ in range(6)))


def installed():
    m = api()
    return m.Installed(node_uid=1101, body_uid=1102, body_gid=1102,
                       runner=Path('/opt/mop/libexec/host-runner'),
                       body_root=Path('/var/lib/mop/host-bodies'),
                       node_private=Path('/var/lib/mop/node-service'))


class Adapter:
    """Только fake OS port: никогда не доказательство containment."""
    def __init__(self):
        self.units = {}
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None
        self.mismatch = False
        self.kill_error = False

    async def inspect(self, address):
        self.calls.append(('inspect', address))
        return self.units.get(address)

    async def start(self, address):
        m = api()
        self.calls.append(('start', address))
        self.entered.set()
        if self.release:
            await self.release.wait()
        facts = m.Facts(address, uuid4().hex, str(uuid4()), 1102, 1102,
                        str(installed().runner), (str(installed().runner), '--scope', address.unit),
                        address.cgroup, True, True)
        self.units[address] = facts
        return facts

    async def retire(self, reference):
        m = api()
        self.calls.append(('retire', reference))
        if self.kill_error:
            raise m.Unknown('cgroup read unavailable; outcome unknown')
        self.units[reference.scope] = replace(self.units[reference.scope], populated=False)
        return self.units[reference.scope]
