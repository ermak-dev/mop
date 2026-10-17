"""Конечные physical ports HTTP executor; только доверенная composition даёт пути.

Не импортирует старый agent: тот связывает пригодные пробы с NATS и owner markers.
Git identity — внешний Git config, не локальная копия authority. Терминальные
user handles/PTY остаются отдельным private WS протоколом.
"""
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import signal
from typing import Protocol

from mop.node.http_execution import Work, Refused, Unavailable, EffectGuard


@dataclass(frozen=True)
class BoundTarget:
    target_id: str
    incarnation_id: str
    node_id: str
    executor_epoch: str
    generation: int
    role: str
    bundle_digest: str
    grant_id: str
    clone: Path
    tmux_socket: Path
    pane: str
    launcher: Path
    session_socket: Path | None = None

    def check(self, work):
        for name in ('target_id', 'incarnation_id', 'node_id', 'executor_epoch',
                     'generation', 'role', 'bundle_digest', 'grant_id'):
            if getattr(self, name) != getattr(work, name):
                raise Refused('physical binding does not match admitted work')
        if (any(not isinstance(p, Path) or not p.is_absolute() for p in
                (self.clone, self.tmux_socket, self.launcher))
                or type(self.pane) is not str or not re.fullmatch(r'%[0-9]+', self.pane)):
            raise Refused('absolute physical handles and exact pane required')


@dataclass(frozen=True)
class ActionInput:
    # У #639 v1 пока нет wire endpoint этих данных. Только typed composition port,
    # не owner_id как доказательство профиля и не произвольные JSON/argv/path.
    git_name: str
    git_email: str
    message: str = ''
    peer_token: str | None = field(default=None, repr=False)

    def check(self, action):
        for value in (self.git_name, self.git_email):
            if (type(value) is not str or not 1 <= len(value) <= 256
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise Refused('confirmed Git identity required')
        if (type(self.message) is not str or len(self.message.encode('utf-8')) > 16384
                or '\x00' in self.message):
            raise Refused('bounded message required')
        if action == 'send_message' and not self.message:
            raise Refused('message input unavailable')


class BindingResolver(Protocol):
    """Production binding обязан получить addressed input и проверить physical identity.

    resolve — bounded read, не admission. Каждый вызов проверяет реальный namespace,
    incarnation/session, immutable role/grant pin; имя папета не такой mapping.
    reconcile подтверждает конец старых effects; HTTP restarted ACK недостаточен.
    """
    async def resolve(self, work: Work) -> tuple[BoundTarget, ActionInput]: ...
    async def reconcile(self, node_id, previous_epoch, new_epoch) -> bool: ...


class ProcessRunner:
    """Async subprocess без shell, с bounded output и kill/reap на timeout/cancel.

    После отмены дочерний process group не продолжает менять Git config. У tmux
    клиент может умереть после передачи команды: вызывающий сохраняет unknown fence.
    """
    def __init__(self, *, timeout=10.0, output_limit=65536, concurrency=4):
        if (not .01 <= timeout <= 60 or not 128 <= output_limit <= 1048576
                or type(concurrency) is not int or not 1 <= concurrency <= 8):
            raise Refused('bounded subprocess configuration required')
        self.timeout, self.output_limit = timeout, output_limit
        self._slots = asyncio.Semaphore(concurrency)
        self._processes = set()
        self._spawns = set()
        self._closed = False

    @property
    def active(self):
        return len(self._processes)

    async def run(self, argv, *, before_spawn=None, final_check=None):
        if self._closed:
            raise Unavailable('subprocess executor closed')
        async with self._slots:
            if self._closed:
                raise Unavailable('subprocess executor closed')
            if before_spawn is not None:
                await before_spawn()
            if self._closed:
                raise Unavailable('subprocess executor closed')
            # create_subprocess_exec может уже породить child до своего return.
            # Отмена caller не должна потерять единственного владельца process handle.
            spawn = asyncio.create_task(self._spawn(argv, final_check))
            self._spawns.add(spawn)
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await spawn
                await self._reap(process)
                self._processes.discard(process)
                raise
            finally:
                self._spawns.discard(spawn)
            try:
                async with asyncio.timeout(self.timeout):
                    output = bytearray()
                    while chunk := await process.stdout.read(8192):
                        output.extend(chunk)
                        if len(output) > self.output_limit:
                            raise Unavailable('subprocess output exceeds limit; effect unknown')
                    code = await process.wait()
                    if code != 0:
                        raise Unavailable('subprocess failed; effect unknown')
                    return output.decode('utf-8', errors='replace')
            except TimeoutError as error:
                raise Unavailable('subprocess timed out; effect unknown') from error
            finally:
                # wait() подтверждает reap лидера. Descendants в той же группе
                # убиваются даже если лидер уже вышел, оставив stdout pipe открытым.
                await self._reap(process)
                self._processes.discard(process)

    async def _spawn(self, argv, final_check):
        # Admission/rebind уже завершились, но create_task уступил event loop.
        # Последний синхронный gate исполняется владельцем child, без нового yield.
        if self._closed:
            raise Unavailable('subprocess executor closed before spawn')
        if final_check is not None:
            final_check()
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        self._processes.add(process)
        if self._closed:
            await self._reap(process)
            self._processes.discard(process)
            raise Unavailable('subprocess executor closed during spawn')
        return process

    @staticmethod
    async def _reap(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def aclose(self):
        self._closed = True
        if self._spawns:
            await asyncio.gather(*tuple(self._spawns), return_exceptions=True)
        processes = tuple(self._processes)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(*(p.wait() for p in processes))


class UnixSessionSender:
    """Настоящий async session channel, без terminal keys и без shell.

    Повторно использует чистые framing/envelope session.py. Write/drain не является
    квитанцией принятия: до отдельного addressed ACK adapter результат unknown.
    """
    def __init__(self, *, timeout=5, concurrency=4):
        if not .1 <= timeout <= 30 or type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise Refused('bounded session channel required')
        self.timeout = timeout
        self._slots = asyncio.Semaphore(concurrency)
        self._writers = set()
        self._closed = False

    def preflight(self, target, inputs):
        if (self._closed or not isinstance(target.session_socket, Path)
                or not target.session_socket.is_absolute()
                or type(inputs.peer_token) is not str or not 1 <= len(inputs.peer_token) <= 4096):
            raise Unavailable('addressed session channel and peer credential unavailable')
        inputs.check('send_message')
        try:
            available = target.session_socket.is_socket()
        except OSError as error:
            raise Unavailable('addressed session channel unavailable') from error
        if not available:
            raise Unavailable('addressed session channel unavailable')

    async def send(self, target, inputs, before_send):
        from mop import session
        self.preflight(target, inputs)
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                async with self._slots:
                    if self._closed:
                        raise Unavailable('session sender closed')
                    # Connect сам по себе не доставляет сообщения. Перепроверяем
                    # physical binding/admission уже после connect, до writer.write.
                    _, writer = await asyncio.open_unix_connection(str(target.session_socket))
                    self._writers.add(writer)
                    await before_send()
                    if self._closed:
                        raise Unavailable('session sender closed')
                    frame = session.user_frame(session.envelope(inputs.message, from_name='mop'))
                    frames = ({'type':'auth', 'token':inputs.peer_token}, frame)
                    writer.write((''.join(json.dumps(f, ensure_ascii=False)+'\n' for f in frames)).encode())
                    await writer.drain()
                    return 'unknown', 'effect-running'
        except (OSError, TimeoutError) as error:
            raise Unavailable('session delivery uncertain') from error
        finally:
            if writer:
                self._writers.discard(writer)
                writer.close()
                await writer.wait_closed()

    async def aclose(self):
        self._closed = True
        writers = tuple(self._writers)
        for writer in writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)


class ReplacementPort(Protocol):
    # Проверяет конец старого process scope и новую session identity; ACK tmux
    # подтверждает только команду серверу, а не конец всех прежних subprocess.
    async def confirmed(self, work: Work, target: BoundTarget) -> bool: ...


class ReadinessPort(Protocol):
    # Только подтверждённая hook/session identity; живой tmux не означает ready.
    async def confirmed(self, work: Work, target: BoundTarget) -> bool: ...


class BodyPort(Protocol):
    # Что именно сносится, решает драйвер тела: у host — нажитое в клоне,
    # у контейнера — сам контейнер. quiescent доказывает, что живой сессии
    # цели нет; wipe обязан сам держать запрет запуска сессии до конца сноса
    # и вернуть True только после физической проверки результата.
    async def quiescent(self, work: Work, target: BoundTarget) -> bool: ...
    async def wipe(self, work: Work, target: BoundTarget) -> bool: ...


class LocalExecutor:
    """Host/body namespace adapter. Resolver обязателен; нет legacy fallback.

    Контейнерная composition запускает этот adapter внутри нужного namespace либо
    предоставляет другой конечный ExecutorPort. Слепое SSH по имени запрещено.
    """
    def __init__(self, runner: ProcessRunner, resolver: BindingResolver | None, *,
                 sender=None, readiness: ReadinessPort | None = None,
                 replacement: ReplacementPort | None = None, body: BodyPort | None = None):
        self.runner, self.resolver = runner, resolver
        self.sender = sender or UnixSessionSender()
        self.readiness = readiness
        self.replacement = replacement
        self.body = body

    async def reconcile(self, node_id, previous_epoch, new_epoch):
        if self.resolver is None:
            raise Unavailable('physical binding resolver unavailable')
        return await self.resolver.reconcile(node_id, previous_epoch, new_epoch)

    async def _bound(self, work):
        if self.resolver is None:
            raise Unavailable('action input and physical binding unavailable')
        async with asyncio.timeout(10):
            target, inputs = await self.resolver.resolve(work)
        if type(target) is not BoundTarget or type(inputs) is not ActionInput:
            raise Refused('typed physical target and input required')
        target.check(work)
        inputs.check(work.action)
        return target, inputs

    async def execute(self, work, guard):
        target, inputs = await self._bound(work)

        if type(guard) is not EffectGuard:
            raise Refused('effect guard required')

        # Заведомо неподдерживаемое действие не получает частичный внешний эффект.
        # Наличие port не обещает успех: его неподтверждённый ответ остаётся unknown.
        if work.action == 'restart_session' and self.replacement is None:
            raise Unavailable('session replacement confirmation source unavailable')
        if work.action == 'wipe_clone' and self.body is None:
            raise Unavailable('body wipe port unavailable')
        if work.action == 'send_message':
            preflight = getattr(self.sender, 'preflight', None)
            if preflight is not None:
                preflight(target, inputs)

        async def before_spawn():
            # Semaphore уже взят. После HTTP перечитываем physical mapping.
            # Отдельный final_check внутри spawn закрывает scheduling gap.
            await guard()
            current, current_input = await self._bound(work)
            if current != target or current_input != inputs:
                raise Refused('physical binding or action input changed')
            guard.check()

        async def effect(argv):
            return await self.runner.run(argv, before_spawn=before_spawn, final_check=guard.check)

        git = ('git', '-C', str(target.clone))
        tmux = ('tmux', '-S', str(target.tmux_socket))
        if work.action in ('apply_identity', 'send_message'):
            # Никакого rollback: поздний отказ не пишет identity новой generation.
            # --worktree совпадает с --local без extensions.worktreeConfig;
            # с extension меняет только адресованное дерево, не соседние worktrees.
            config = (*git, 'config', '--worktree')
            await effect((*config, 'user.name', inputs.git_name))
            await effect((*config, 'user.email', inputs.git_email))
            await effect((*config, 'user.useConfigOnly', 'true'))
            # Успех записи не доказывает effective identity (include/env/normalization).
            expected = f'{inputs.git_name} <{inputs.git_email}> '
            for variable in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'):
                identity = await effect((*git, 'var', variable))
                if not identity.startswith(expected):
                    raise Refused('effective Git identity does not match confirmed profile')
            if work.action == 'apply_identity':
                return 'succeeded', 'identity-applied'
            return await self.sender.send(target, inputs, before_spawn)
        if work.action == 'restart_session':
            # Только установленный composition launcher; не shell/argv из Work.
            # tmux shell-command имеет shell semantics, поэтому путь quoted.
            await effect((*tmux, 'respawn-pane', '-k', '-t', target.pane,
                          '-c', str(target.clone), shlex.quote(str(target.launcher))))
            if self.replacement is not None:
                async with asyncio.timeout(10):
                    confirmed = await self.replacement.confirmed(work, target)
                if confirmed is True:
                    return 'succeeded', 'session-replaced'
            return 'unknown', 'effect-running'
        if work.action == 'wipe_clone':
            await before_spawn()
            async with asyncio.timeout(10):
                quiet = await self.body.quiescent(work, target)
            guard.check()
            # Только явное True доказывает тишину; снос под живой сессией недопустим.
            if quiet is not True:
                return 'failed', 'failed-before-effect'
            await before_spawn()
            wiped = await self.body.wipe(work, target)
            return ('succeeded', 'clone-wiped') if wiped is True else ('unknown', 'effect-running')
        if work.action == 'confirm_ready':
            if self.readiness is None:
                raise Unavailable('confirmed session readiness source unavailable')
            await before_spawn()
            async with asyncio.timeout(10):
                confirmed = await self.readiness.confirmed(work, target)
            guard.check()
            if confirmed is not True:
                raise Unavailable('bound session readiness not confirmed')
            return 'succeeded', 'ready'
        raise Refused('unknown finite action')

    async def aclose(self):
        await self.sender.aclose()
        await self.runner.aclose()
