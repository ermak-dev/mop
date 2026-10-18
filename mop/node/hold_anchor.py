#!/usr/bin/python3 -ISB
"""Пассивный installed anchor #643: только проверка себя и ожидание сигнала.

Этот файл устанавливается целиком, отдельно от пакета, без подстановок в bytes.
Родитель executable — sha256-<hash этих bytes>. Interpreter — системный Python
в isolated/no-site/no-bytecode режиме: cwd, user site и PYTHON* не участвуют
в импортах; даже root не создаёт __pycache__.
Аргументы — лишь scheduler metadata, не authority и не readiness. Ни root,
ни nonroot не запускают здесь native/bootstrap/clone/model или credential I/O.
Установщик и runtime binding намеренно находятся вне этого artifact.
"""
from collections import namedtuple
import hashlib
import os
from pathlib import PurePosixPath
import re
import signal
import stat
import sys
from uuid import UUID


Metadata = namedtuple('Metadata', 'sha256 target incarnation executor_epoch generation bootstrap_revision')
OPTIONS = ('--sha256', '--target', '--incarnation', '--executor-epoch',
           '--generation', '--bootstrap-revision')
STOP_SIGNALS = frozenset((signal.SIGTERM, signal.SIGINT, signal.SIGHUP))
MAX_BYTES = 1024 * 1024


def _digest(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('canonical SHA256 digest required')


def parse(argv):
    """Точный конечный протокол hold; ни defaults из env, ни лишних ключей."""
    if (type(argv) not in (list, tuple) or len(argv) != 13
            or any(type(item) is not str for item in argv) or argv[0] != 'hold'):
        raise ValueError('hold with exactly six public metadata options required')
    keys, values = argv[1::2], argv[2::2]
    if set(keys) != set(OPTIONS) or len(set(keys)) != len(keys):
        raise ValueError('unknown, duplicate or missing hold metadata option')
    fields = dict(zip(keys, values))
    _digest(fields['--sha256'])
    for name in ('--target', '--incarnation', '--executor-epoch', '--bootstrap-revision'):
        value = fields[name]
        if len(value) != 36 or str(UUID(value)) != value:
            raise ValueError('canonical hold metadata UUID required')
    generation = fields['--generation']
    if (re.fullmatch('[1-9][0-9]{0,18}', generation) is None
            or int(generation) >= 2**63):
        raise ValueError('positive canonical hold generation below 2**63 required')
    return Metadata(fields['--sha256'], fields['--target'], fields['--incarnation'],
                    fields['--executor-epoch'], int(generation), fields['--bootstrap-revision'])


def installed_path(executable, digest):
    """Лексический public pin; root trust и bytes проверяются отдельно."""
    _digest(digest)
    if (type(executable) is not str or len(executable) > 512
            or re.fullmatch(r'/[A-Za-z0-9_./-]+', executable) is None):
        raise ValueError('absolute canonical installed anchor path required')
    path = PurePosixPath(executable)
    if ('..' in path.parts or path.as_posix() != executable or executable.startswith('//')
            or path.parent.name != 'sha256-'+digest):
        raise ValueError('absolute content-addressed installed anchor path required')
    return path


def same_file(before, after):
    """Identity и содержимое metadata, но не atime от нашего же чтения."""
    return all(getattr(before, field) == getattr(after, field) for field in (
        'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size',
        'st_mtime_ns', 'st_ctime_ns'))


def read_bytes(descriptor):
    """Ограниченное чтение regular fd, без гонки подмены bytes при hash."""
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
        raise ValueError('bounded regular anchor file required')
    content = bytearray()
    while True:
        block = os.read(descriptor, 65536)
        if not block:
            break
        content.extend(block)
        if len(content) > MAX_BYTES:
            raise ValueError('anchor file exceeds the byte limit')
    if len(content) != before.st_size or not same_file(before, os.fstat(descriptor)):
        raise ValueError('anchor file identity or bytes changed during verification')
    return bytes(content)


def _trusted_directory(descriptor):
    info = os.fstat(descriptor)
    # Root вправе менять установку; ни группа, ни прочие — даже со sticky bit.
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('anchor ancestor must be root-owned and not group/other writable')


def verify_installed(executable, digest):
    """Вернуть pinned read-only fd; caller закрывает его после passive hold.

    Каждый компонент открывается относительно уже доверенного dirfd, без
    symlink-follow. Удерживаем именно проверенный regular fd, не повторный open
    по строке. Доверие root — граница установки, не полномочия argv metadata.
    """
    path = installed_path(executable, digest)
    directory, descriptor = None, None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open('/', flags | os.O_DIRECTORY)
        _trusted_directory(directory)
        for part in path.parts[1:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
            _trusted_directory(directory)
        descriptor = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or before.st_mode & 0o7222 or not before.st_mode & 0o111):
            raise ValueError('anchor must be a root-owned nonwritable regular executable')
        if not same_file(before, os.stat(path.name, dir_fd=directory, follow_symlinks=False)):
            raise ValueError('anchor path and opened file identity differ')
        content = read_bytes(descriptor)
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('installed anchor bytes do not match the pinned SHA256')
        if (not same_file(before, os.fstat(descriptor))
                or not same_file(before, os.stat(path.name, dir_fd=directory, follow_symlinks=False))
                or not same_file(before, os.stat(executable, follow_symlinks=False))):
            raise ValueError('installed anchor file identity changed during verification')
        result, descriptor = descriptor, None
        return result
    except OSError:
        raise ValueError('installed anchor cannot be opened with nonsymlink root trust') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def hold(argv, executable):
    """Без вывода/детей/записей; pending stop не теряется между verify и wait."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    descriptor = None
    try:
        metadata = parse(argv)
        descriptor = verify_installed(executable, metadata.sha256)
        signal.sigwait(STOP_SIGNALS)
        return 0
    finally:
        if descriptor is not None:
            os.close(descriptor)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _entry():
    """Standalone frontend: отказ только в stderr, успешный hold всегда молчит."""
    try:
        if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
            raise ValueError('isolated no-site no-bytecode system Python invocation required')
        if sys.argv[0] != __file__:
            raise ValueError('installed anchor must be invoked by its absolute path')
        return hold(sys.argv[1:], sys.argv[0])
    except ValueError as error:
        sys.stderr.write('hold anchor refused: '+str(error)+'\n')
        return 2


if __name__ == '__main__':
    sys.exit(_entry())
