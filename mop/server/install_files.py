"""Protected install/backup artifacts: dirfd, no-follow и отказ от перезаписи."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('artifact directory must be absolute without parent traversal')
    descriptor = None
    try:
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (*path.parts[1:], None):
            info = os.fstat(descriptor)
            if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise RuntimeError('artifact directory permits untrusted replacement')
            if part is None:
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise RuntimeError('artifact directory must be private and owned by this user')
                break
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError:
        raise RuntimeError('protected artifact path unavailable; symlink refused') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_new(path, data):
    if type(data) is not bytes:
        raise TypeError('protected artifact requires bytes')
    path = Path(path)
    if path.name in ('', '.', '..'):
        raise RuntimeError('protected artifact requires a filename')
    with private_directory(path.parent) as directory:
        try:
            descriptor = os.open(path.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
        except OSError:
            raise RuntimeError('protected artifact requires a new file; no replacement or symlink') from None
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        except BaseException:
            os.unlink(path.name, dir_fd=directory)
            raise


def retain(directory, *, keep, apply=False):
    if type(keep) is not int or not 2 <= keep <= 10000 or type(apply) is not bool:
        raise ValueError('backup retention must keep 2..10000 native dumps')
    with private_directory(directory) as descriptor:
        dumps = []
        for name in os.listdir(descriptor):
            if not name.endswith('.dump'):
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise RuntimeError('backup retention requires private regular dumps owned by this user')
                if stream.read(5) != b'PGDMP':
                    raise RuntimeError('backup retention refuses non-PostgreSQL archive')
                dumps.append((info.st_mtime_ns, name, info.st_dev, info.st_ino))
        selected = sorted(dumps, reverse=True)[keep:]
        if apply:
            for _, name, device, inode in selected:
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (device, inode):
                    raise RuntimeError('backup changed during retention; maintenance required')
                os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
    return {'applied': apply, 'remove': sorted(item[1] for item in selected),
            'root_trust_included': False}
