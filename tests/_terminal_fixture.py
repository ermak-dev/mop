"""Отдельный interpreter gateway fence; framework включается только в worker."""
import os
import time


def claim(bootstrap, actor, target, incarnation, start):
    os.environ['MOP_CONTROLLER_BOOTSTRAP']=bootstrap
    from mop.server.controller.runtime import configure, close_pools
    configure()
    from mop.server.sessions.terminal_control import Terminals, Conflict
    while time.time()<start:
        time.sleep(.01)
    try:
        try:
            return Terminals().open(actor,target_id=target,incarnation_id=incarnation,mode='rw',cols=100,rows=30)['id']
        except Conflict:
            return None
    finally:
        close_pools()


class NativePtyResolver:
    """Только fixture: владелец реального PTY и текущей physical identity.

    Не production binding #643. Node-side guard сравнивает все pinned bindings,
    actual fd и независимые conversation/terminal epochs после каждого await.
    """
    def __init__(self, fd, expected):
        from uuid import uuid4
        self.fd=fd
        self.expected=dict(expected)
        self.conversation_epoch=str(uuid4())
        self.terminal_epoch=str(uuid4())
        self.stream=None

    async def preflight(self, context):
        from mop.server.realtime.terminal import TerminalUnavailable
        for key in ('id','target_id','incarnation_id','credential_id','user_id','executor_epoch','generation','mode'):
            if context[key]!=self.expected[key]:
                raise TerminalUnavailable('fixture physical binding changed')
        if not os.isatty(self.fd) or os.get_blocking(self.fd):
            raise TerminalUnavailable('fixture physical PTY unavailable')

    async def resolve(self, context):
        from mop.server.realtime.terminal import PtyTerminalStream, TerminalUnavailable, binding
        await self.preflight(context)
        pinned=binding(context)
        device=os.fstat(self.fd)
        def guard(actual, conversation, terminal):
            current=os.fstat(self.fd)
            if (binding(actual)!=pinned or conversation!=self.conversation_epoch or
                    terminal!=self.terminal_epoch or (current.st_dev,current.st_ino)!=(device.st_dev,device.st_ino)):
                raise TerminalUnavailable('fixture physical identity changed')
        self.stream=PtyTerminalStream(self.fd,writable=context['mode']=='rw',guard=guard,
            conversation_epoch=self.conversation_epoch,terminal_epoch=self.terminal_epoch)
        return self.stream

    async def inspect(self, context):
        from mop.server.realtime.terminal import binding, TerminalUnavailable
        if self.stream is None or self.stream.context is None or binding(context)!=binding(self.stream.context):
            raise TerminalUnavailable('fixture original instance unavailable')
        return self.stream
