"""TerminalStream не EventPublisher. Только private bytes и конечные effects."""
import asyncio
import fcntl
import os
import struct
import termios
from dataclasses import dataclass
from uuid import uuid4
from typing import Protocol


class TerminalUnavailable(RuntimeError):
    """Физическая текущая сессия не установлена; fail closed без нового PTY."""


class TerminalStream(Protocol):
    async def start(self): ...
    async def read(self) -> bytes: ...
    async def write(self, data: bytes) -> int: ...
    async def resize(self, cols: int, rows: int): ...
    async def aclose(self): ...


def binding(context):
    from mop.server.sessions.terminal_control import BINDING
    return tuple(context[key] for key in (*BINDING,'executor_epoch'))


@dataclass(frozen=True)
class RetirementProof:
    """Opaque instance receipt; не wire DTO и не утверждение клиента."""
    root_id: str
    instance_id: str
    binding: tuple
    conversation_epoch: str
    terminal_epoch: str
    seal: str


class RootTerminalStream:
    """Trusted capability: preflight, физический resolver, inspection, retirement.

    Resolver обязан реализовать preflight(context), resolve(context), inspect(context).
    Он доказывает actual conversation/terminal epochs, clone safety, адресованный
    executor и node-side synchronous guard всех bindings непосредственно у syscall.
    LeaseExpired/ORM exists/JSON quiescent не реализация этого контракта. Default
    намеренно unbound до физической интеграции #643, не создаёт тело или PTY.
    """
    def __init__(self, resolver=None):
        self.resolver=resolver
        self.root_id=str(uuid4())
        self.instances={}
        self.proofs={}
        self.closings={}

    def _capabilities(self):
        if self.resolver is None or any(not callable(getattr(self.resolver,name,None))
                for name in ('preflight','resolve','inspect')):
            raise TerminalUnavailable('physical terminal fencing and retirement resolver is not bound')

    async def preflight(self, context):
        self._capabilities()
        if len(self.instances)>=64:
            raise TerminalUnavailable('unresolved physical terminal instance limit reached')
        await self.resolver.preflight(dict(context))

    async def open(self, context, *, current=None, pin=None):
        await self.preflight(context)
        if current is None or pin is None:
            raise TerminalUnavailable('current terminal authority and physical instance pin are not bound')
        stream=await self.resolver.resolve(dict(context))
        if not isinstance(stream,PtyTerminalStream):
            raise TerminalUnavailable('resolver must supply an instance-fenced physical terminal')
        stream.bind(context,current)
        key=binding(context)
        if key in self.instances:
            raise TerminalUnavailable('physical attachment cannot be reopened')
        self.instances[key]=stream
        try:
            # SQL pin завершается до dup/start и любого физического эффекта.
            # Recovery инспектирует именно этот instance, не новый closed fd.
            physical={'physical_instance_id':stream.instance_id,'conversation_epoch':stream.conversation_epoch,
                      'terminal_epoch':stream.terminal_epoch}
            pinned=await pin(physical)
            if binding(pinned)!=key or any(pinned[name]!=value for name,value in physical.items()):
                raise TerminalUnavailable('durable physical terminal pin does not match resolver instance')
            stream.context=dict(pinned)
            await stream.start()
            return stream
        except BaseException:
            await stream.aclose()
            raise

    async def inspect(self, context):
        self._capabilities()
        key=binding(context)
        stream=self.instances.get(key)
        if stream is None:
            # При утрате gateway нужна реальная node-side инспекция original
            # instance. Отсутствие записи/lease не является доказательством.
            stream=await self.resolver.inspect(dict(context))
            if (not isinstance(stream,PtyTerminalStream) or stream.context is None or
                    binding(stream.context)!=key):
                raise TerminalUnavailable('physical terminal instance cannot be inspected')
        if (context.get('physical_instance_id')!=stream.instance_id or
                context.get('conversation_epoch')!=stream.conversation_epoch or
                context.get('terminal_epoch')!=stream.terminal_epoch):
            raise TerminalUnavailable('inspected physical instance differs from durable terminal pin')
        self.instances[key]=stream
        return {'instance_id':stream.instance_id,'conversation_epoch':stream.conversation_epoch,
                'terminal_epoch':stream.terminal_epoch,'retired':stream.retired}

    async def retirement(self, context):
        await self.inspect(context)
        stream=self.instances[binding(context)]
        if not stream.retired:
            raise TerminalUnavailable('physical terminal retirement is not proven')
        for previous,physical in self.proofs.values():
            if physical is stream:
                return previous
        proof=RetirementProof(self.root_id,stream.instance_id,binding(context),
            stream.conversation_epoch,stream.terminal_epoch,str(uuid4()))
        self.proofs[proof.seal]=(proof,stream)
        return proof

    def verify_retirement(self, context, proof):
        entry=self.proofs.get(proof.seal) if type(proof) is RetirementProof else None
        if (entry is None or entry[0]!=proof or proof.root_id!=self.root_id or
                proof.binding!=binding(context) or not entry[1].retired or
                proof.instance_id!=entry[1].instance_id):
            raise TerminalUnavailable('trusted instance-scoped terminal retirement proof required')

    def forget(self, context, proof):
        self.verify_retirement(context,proof)
        self.instances.pop(binding(context),None)
        self.closings.pop(binding(context),None)
        self.proofs.pop(proof.seal,None)

    def require_retired(self):
        if (any(not stream.retired for stream in self.instances.values()) or
                any(not task.done() for task in self.closings.values())):
            raise TerminalUnavailable('gateway shutdown incomplete: physical terminal detach is unresolved')

    async def detach(self, context, *, timeout=2):
        key=binding(context)
        stream=self.instances.get(key)
        if stream is None:
            raise TerminalUnavailable('physical terminal instance is not known')
        task=self.closings.get(key)
        if task is None:
            task=asyncio.create_task(stream.aclose())
            # Задача остаётся owned при timeout/cancel, не мнимое завершение.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self.closings[key]=task
        done,_=await asyncio.wait((task,),timeout=timeout)
        if not done:
            raise TerminalUnavailable('physical terminal detach is still running')
        try:
            task.result()
        except Exception as exc:
            raise TerminalUnavailable(f'physical terminal detach failed ({type(exc).__name__})') from None
        return await self.retirement(context)

    async def reconcile(self, actor, attachment_id, *, runtime, terminals):
        # Current authority до ожидания инспектора и ещё раз в SQL после него.
        context=await runtime.database(terminals.inspect,actor,attachment_id)
        proof=await self.retirement(context)
        await runtime.database(terminals.reconcile,actor,context,proof)
        self.forget(context,proof)


class UnixTerminalStream:
    """Настоящий приватный Unix byte stream; resize без протокола запрещён."""
    def __init__(self, path):
        self.path=path
        self.reader=self.writer=None

    async def start(self):
        self.reader,self.writer=await asyncio.wait_for(asyncio.open_unix_connection(self.path,limit=16384),2)

    async def read(self):
        return await self.reader.read(4096)

    async def write(self, data):
        self.writer.write(data)
        await asyncio.wait_for(self.writer.drain(),2)
        return len(data)

    async def resize(self, cols, rows):
        raise TerminalUnavailable('Unix byte stream has no terminal resize capability')

    async def aclose(self):
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer=None


class PtyTerminalStream:
    """Attach к root-owned nonblocking master fd, не spawn/kill job.

    Дублирование fd делит open flags с владельцем. Не менять режим основного
    CLI при read-only attach: resolver обязан передать уже async-ready handle.
    """
    def __init__(self, fd, *, writable=False, guard=None, conversation_epoch=None, terminal_epoch=None):
        self.source_fd,self.writable=fd,writable
        self.guard=guard
        self.conversation_epoch,self.terminal_epoch=conversation_epoch,terminal_epoch
        self.instance_id=str(uuid4())
        self.context=self.current=None
        self.fd=None
        self.closed=False
        self.started=False
        self.waiters=set()

    def bind(self, context, current):
        if self.context is not None or self.started or self.closed:
            raise TerminalUnavailable('physical terminal instance cannot be rebound')
        if not callable(self.guard) or not self.conversation_epoch or not self.terminal_epoch:
            raise TerminalUnavailable('physical terminal identity and synchronous node guard required')
        if self.writable!=(context['mode']=='rw'):
            raise TerminalUnavailable('physical terminal mode does not match binding')
        self.context=dict(context)
        self.current=current

    @property
    def retired(self):
        # Не JSON-флаг: descriptor уже закрыт, instance не может start/rebind,
        # каждый продолжившийся waiter проверяет closed перед любым syscall.
        return self.closed and self.fd is None

    def _physical(self):
        if self.closed:
            raise TerminalUnavailable('physical terminal instance is retired')
        if self.guard is None:
            if self.writable:
                raise TerminalUnavailable('writable terminal requires a physical guard')
            return
        result=self.guard(self.context,self.conversation_epoch,self.terminal_epoch)
        if result is not None:
            raise TerminalUnavailable('physical terminal guard must synchronously validate or raise')

    async def _authority(self):
        if self.closed:
            raise TerminalUnavailable('physical terminal instance is retired')
        if self.current is not None:
            await self.current()
        # Именно после всех await, не перед ожиданием writable fd.
        self._physical()

    async def start(self):
        if self.started or self.closed:
            raise TerminalUnavailable('physical terminal instance cannot be reopened')
        self.started=True
        await self._authority()
        if not os.isatty(self.source_fd):
            raise TerminalUnavailable('current PTY is unavailable')
        if os.get_blocking(self.source_fd):
            raise TerminalUnavailable('physical resolver must supply a nonblocking terminal attachment')
        self.fd=os.dup(self.source_fd)

    async def _ready(self, *, writing=False):
        loop=asyncio.get_running_loop()
        future=loop.create_future()
        fd=self.fd
        def done():
            if not future.done():
                future.set_result(None)
        add=loop.add_writer if writing else loop.add_reader
        remove=loop.remove_writer if writing else loop.remove_reader
        add(fd,done)
        self.waiters.add(future)
        try:
            await future
        finally:
            self.waiters.discard(future)
            if self.fd==fd:
                remove(fd)

    async def read(self):
        while self.fd is not None:
            try:
                return os.read(self.fd,4096)
            except BlockingIOError:
                await self._ready()
            except OSError:
                return b''
        return b''

    async def write(self, data):
        if not self.writable:
            raise PermissionError('read-only terminal cannot send input')
        count=0
        async with asyncio.timeout(2):
            while count<len(data):
                await self._authority()
                try:
                    count+=os.write(self.fd,data[count:])
                except BlockingIOError:
                    await self._ready(writing=True)
        return count

    async def resize(self, cols, rows):
        if not self.writable:
            raise PermissionError('read-only terminal cannot resize')
        from mop.server.sessions.terminal_control import size
        size(cols,rows)
        await self._authority()
        fcntl.ioctl(self.fd,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))

    async def aclose(self):
        # Ни одного await между permanent revoke и close. Отмена cleanup не
        # может воскресить fd; borrowed body owner и его flags не меняются.
        self.closed=True
        if self.fd is not None:
            loop=asyncio.get_running_loop()
            loop.remove_reader(self.fd)
            loop.remove_writer(self.fd)
            os.close(self.fd)
            self.fd=None
        for waiter in tuple(self.waiters):
            if not waiter.done():
                waiter.set_result(None)
