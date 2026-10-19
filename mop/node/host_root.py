"""Изолированный bootstrap installed HOST root, до первого импорта пакета.

Запуск только immutable installed bin/mop host-root. Пользовательский checkout
не release; здесь нет .env, credentials, policy или файла executor epoch.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys

# Полный импортируемый root graph; никакого glob всего checkout/site-packages.
ROOT_SOURCES = (
    'bin/mop', 'mop/__init__.py', 'mop/node/__init__.py',
    'mop/node/host_root.py', 'mop/node/host_backend.py',
    'mop/node/host_scope.py', 'mop/node/host_scope_supervisor.py',
    'mop/node/host_scope_systemd.py', 'mop/node/http_execution.py',
    'mop/common/__init__.py', 'mop/common/initialcreate.py',
    'mop/cli/__init__.py', 'mop/cli/service/__init__.py', 'mop/cli/service/host-root.py',
)


PHASE_ROOT_SOURCES = (
    'mop/node/host_phase_supervisor.py', 'mop/node/host_phases.py',
    'mop/node/host_phase_source.py', 'mop/node/host_phase_manifest.py',
    'mop/node/host_inputs.py', 'mop/node/host_input_files.py',
    'mop/common/credentials.py', 'mop/common/installed_content.py',
)


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('absolute immutable root path required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('root-owned immutable HOST ancestors required')
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _read(path, *, limit):
    path = Path(path)
    directory = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or info.st_nlink != 1 or not 0 <= info.st_size <= limit):
                raise RuntimeError('bounded single-link root-owned immutable HOST file required')
            raw = os.read(fd, limit+1)
            after = os.fstat(fd)
            fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_size', 'st_nlink', 'st_mtime_ns', 'st_ctime_ns')
            if len(raw) != info.st_size or any(getattr(info, key) != getattr(after, key) for key in fields):
                raise RuntimeError('installed HOST file changed during bounded read')
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def trusted_release():
    """-I/-S предшествуют import; путь release выводится только из entrypoint."""
    if os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError('HOST root requires the isolated installed release entrypoint')
    release = Path(__file__).absolute().parents[2]
    if os.path.lexists(release/'.git'):
        raise RuntimeError('HOST root requires an immutable installed release, not a checkout')
    try:
        for name in ROOT_SOURCES:
            _read(release/name, limit=1048576)
        _read(Path(sys.executable).resolve(), limit=33554432)
    except OSError as error:
        raise RuntimeError('HOST immutable installed release is unavailable: '+type(error).__name__) from None
    return release


@dataclass(frozen=True, slots=True)
class PhysicalConfig:
    identity: object
    installed: object
    socket: Path
    phase_config: Path | None = None

    @classmethod
    def parse(cls, value):
        from mop.node.host_backend import Identity
        from mop.node.host_scope import Installed, Refused, exact
        fields = ('schema_version', 'installation_id', 'node_id', 'node_uid', 'body_uid',
            'body_gid', 'runner', 'body_root', 'node_private', 'socket')
        exact(value, fields + (('phase_config',) if 'phase_config' in value else ()))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('unsupported physical HOST config schema')
        paths = ('runner', 'body_root', 'node_private', 'socket')
        if any(type(value[key]) is not str for key in paths):
            raise Refused('physical installed HOST paths required')
        identity = Identity(value['installation_id'], value['node_id'])
        installed = Installed(*(value[key] for key in ('node_uid', 'body_uid', 'body_gid')),
            *(Path(value[key]) for key in ('runner', 'body_root', 'node_private')))
        path = Path(value['socket'])
        if (not path.is_absolute() or '..' in path.parts or path == Path('/')
                or len(os.fsencode(path)) > 100 or any(path == p or p in path.parents
                    for p in (installed.runner, installed.body_root, installed.node_private))):
            raise Refused('separate bounded installed HOST socket required')
        phase = value.get('phase_config')
        if phase is not None:
            if type(phase) is not str or Path(phase) != Path(__file__).absolute().parents[2]/'host-phase.json':
                raise Refused('fixed installed HOST phase config required')
            phase = Path(phase)
        elif 'phase_config' in value:
            raise Refused('HOST phase config cannot be null')
        return cls(identity, installed, path, phase)


def load(path):
    if os.geteuid() != 0:
        raise RuntimeError('physical HOST config requires the root installed process')
    try:
        from mop.node.host_scope import decode
        return PhysicalConfig.parse(decode(_read(path, limit=4096)))
    except OSError as error:
        raise RuntimeError('physical root HOST config unavailable: '+type(error).__name__) from None


async def run(config):
    import asyncio
    import signal
    from mop.node.host_scope_supervisor import serve
    if config.phase_config is not None:
        release = Path(__file__).absolute().parents[2]
        for name in PHASE_ROOT_SOURCES:
            _read(release/name, limit=1048576)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await serve(config.installed, config.socket, stop, identity=config.identity, phase_config=config.phase_config)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == '__main__':
    # Никакого пользовательского PYTHONPATH/site/.pth/pyc до trusted imports.
    try:
        release = trusted_release()
        os.chdir('/')
        os.environ.clear()
        sys.dont_write_bytecode = True
        sys.pycache_prefix = '/dev/null/mop-no-bytecode'
        sys.path.insert(0, str(release))
        import importlib
        command = importlib.import_module('mop.cli.service.host-root')
        raise SystemExit(command.main(sys.argv[1:]))
    except (RuntimeError, OSError) as error:
        # Печать принадлежит commandlet, до его импорта — только fixed refusal.
        sys.stderr.write('HOST root installed entry refused: '+str(error)+'\n')
        raise SystemExit(1)
