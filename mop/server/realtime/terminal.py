"""TerminalStream не EventPublisher. Только private bytes и конечные effects."""
import asyncio
import fcntl
import os
import struct
import termios
from typing import Protocol


class TerminalUnavailable(RuntimeError):
    """Физическая текущая сессия не установлена; fail closed без нового PTY."""


class TerminalStream(Protocol):
    async def start(self): ...
    async def read(self) -> bytes: ...
    async def write(self, data: bytes) -> int: ...
    async def resize(self, cols: int, rows: int): ...
    async def aclose(self): ...


class RootTerminalStream:
    """Root связывает адресованный node/local resolver, не browser path/command."""
    def __init__(self, resolver=None):
        self.resolver=resolver

    async def open(self, context):
        if self.resolver is None:
            raise TerminalUnavailable('current physical terminal resolver is not bound')
        # Resolver проверяет свежую incarnation/executor и clone safety до RW.
        # Не импортирует controller и не создаёт process при reconnect.
        stream=await self.resolver(context)
        try:
            await stream.start()
            return stream
        except BaseException:
            await stream.aclose()
            raise


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
    def __init__(self, fd, *, writable=False):
        self.source_fd,self.writable=fd,writable
        self.fd=None

    async def start(self):
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
        try:
            await future
        finally:
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
        fcntl.ioctl(self.fd,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))

    async def aclose(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd=None
