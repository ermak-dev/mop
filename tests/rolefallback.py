"""NoResponders не превращает роль в legacy-вызывающего (#652).

HYPOTHESIS: without_caller удаляет машинную идентичность при retry.
SOLUTION: fallback разрешён только для прежнего human-qualified RPC.
"""
import asyncio
import hermetic
from _lib import Checks
from mop.common import bus,busnames


class Empty:
    def __init__(self):
        self.seen=[]
    async def request(self,subject,data,timeout):
        self.seen.append(subject)
        raise bus.NoRespondersError


async def call(subject):
    nc=Empty()
    try:
        await bus.arequest(nc,subject,b'{}',1)
    except bus.NoRespondersError:
        pass
    return nc.seen


def main():
    c=Checks()
    principal='grant-'+'a'*32
    subjects=[busnames.cluster('example',login=principal),
        busnames.node('example','node-a',login=principal),
        busnames.dispatch_subject('example','node-a',principal),
        busnames.server('example',caller='node-node-a'),
        busnames.startup_subject('example','node-a'),
        busnames.join_config('alice')]
    for subject in subjects:
        c.expect('#652 machines and non-RPC subjects never retry unsigned',
                 asyncio.run(call(subject)),[subject])
    for subject in (busnames.cluster('example',login='alice'),
                    busnames.node('example','node-a',login='alice')):
        c.expect('#652 the existing human RPC transition stays compatible',
                 asyncio.run(call(subject)),[subject,subject.rsplit('.',1)[0]])
    return c.report('rolefallback')


if __name__=='__main__':
    raise SystemExit(main())
