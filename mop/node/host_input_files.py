"""Pinned private ingress -> root snapshot -> body reader, без generic copy API.

Все paths/UID — installed constructor inputs, не wire. Store не запускает фазу,
не заменяет machine recheck/root backend proof и не retire'ит body. Retained
receipts process-local: неизвестный файл после потери owner не усыновляется.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import stat

from mop.node.host_inputs import (Descriptor, Envelope, Limits, MAX_FILE_BYTES, Refused,
                                  canonical, digest, pack, parse_body)

_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK = '.phase-input.lock'
_LOCK_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_STABLE = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
           'st_size', 'st_mtime_ns', 'st_ctime_ns')


def _fingerprint(value):
    return tuple(getattr(value, name) for name in _STABLE)


def _uid(value):
    if type(value) is not int or not 0 < value < 2**31:
        raise Refused('dedicated installed phase input UID/GID required')
    return value


def _running_as(uid):
    if os.geteuid() != uid:
        raise Refused('phase input operation requires its installed OS UID')


def _path(value):
    if (not isinstance(value, Path) or not value.is_absolute() or value == Path('/')
            or '..' in value.parts or '\x00' in str(value)):
        raise Refused('fixed installed phase input directory required')
    return value


class _Directory:
    def __init__(self, path, *, uid, gid, mode, ancestors, locking=False):
        self.path = _path(path)
        self.uid, self.gid, self.mode, self.ancestors = uid, gid, mode, ancestors
        self.fd = self.lock_fd = None
        self.lock_physical = None
        try:
            self.fd = self._open()
            self.identity = _fingerprint(os.fstat(self.fd))[:2]
            if locking:
                self._prepare_lock()
        except BaseException:
            self.close()
            raise

    def _prepare_lock(self):
        try:
            try:
                self.lock_fd = os.open(_LOCK, _LOCK_FLAGS | os.O_CREAT | os.O_EXCL,
                                       0o600, dir_fd=self.fd)
            except FileExistsError:
                self.lock_fd = os.open(_LOCK, _LOCK_FLAGS, dir_fd=self.fd)
            else:
                # Root может первым открыть ingress, но lock принадлежит node UID.
                # Existing inode никогда не chown/chmod и не усыновляется по замене.
                if os.geteuid() == 0 and self.uid != 0:
                    os.fchown(self.lock_fd, self.uid, -1)
            self.lock_physical = _fingerprint(os.fstat(self.lock_fd))
            self._check_lock(self.lock_fd)
        except OSError:
            raise Refused('phase input private lock unavailable') from None

    def _check_lock(self, fd):
        value, named = os.fstat(fd), self.stat(_LOCK)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != self.uid
                or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1
                or value.st_size != 0 or named is None
                or _fingerprint(value) != self.lock_physical
                or _fingerprint(named) != self.lock_physical):
            raise Refused('phase input private lock identity or mode mismatch')

    def _open(self):
        fd = None
        try:
            fd = os.open('/', _FLAGS)
            for name in self.path.parts[1:]:
                ancestor = os.fstat(fd)
                if (ancestor.st_uid not in self.ancestors
                        or ancestor.st_mode & 0o022 and not
                        (ancestor.st_uid == 0 and ancestor.st_mode & stat.S_ISVTX)):
                    raise Refused('phase input directory has untrusted ancestors')
                child = os.open(name, _FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            self._verify(os.fstat(fd))
            result, fd = fd, None
            return result
        except OSError:
            raise Refused('phase input pinned directory is unavailable') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _verify(self, value):
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid != self.uid
                or self.gid is not None and value.st_gid != self.gid
                or stat.S_IMODE(value.st_mode) != self.mode):
            raise Refused('phase input directory ownership or private mode mismatch')

    def check(self):
        if self.fd is None:
            raise Refused('phase input store is closed')
        self._verify(os.fstat(self.fd))
        current = self._open()
        try:
            if _fingerprint(os.fstat(current))[:2] != self.identity:
                raise Refused('phase input pinned directory identity changed')
        finally:
            os.close(current)

    @contextmanager
    def lock(self):
        self.check()
        if self.lock_fd is None:
            raise Refused('phase input directory has no private writer lock')
        fd = None
        try:
            self._check_lock(self.lock_fd)
            # Directory inode доступен body O_RDONLY и допускает LOCK_EX.
            # Отдельный 0600 inode закрыт body даже через его readonly dirfd.
            # Новый open description сохраняет process/thread serialization.
            fd = os.open(_LOCK, _LOCK_FLAGS, dir_fd=self.fd)
            self._check_lock(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused('phase input store busy with concurrent operation') from None
            self.check()
            self._check_lock(fd)
            yield
            self._check_lock(fd)
            self.check()
        except OSError:
            raise Refused('phase input filesystem operation unavailable') from None
        finally:
            if fd is not None:
                os.close(fd)

    def close(self):
        # Не unlink: waiter/new store должны видеть тот же lock inode. Anchor
        # держит inode живым до close и не даёт reuse скрыть unlink/replacement.
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def stat(self, name):
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def capacity(self, limits, extra):
        count = total = 0
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if entry.name == _LOCK:
                    # Один fixed infrastructure inode вне data budget; подмена
                    # не становится бесплатной unknown entry.
                    self._check_lock(self.lock_fd)
                    continue
                value = entry.stat(follow_symlinks=False)
                count += 1
                total += value.st_size
                if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                        or count >= limits.max_files or total+extra > limits.max_bytes
                        or value.st_size > limits.max_file_bytes):
                    raise Refused('phase input file or byte capacity bound reached')
        if count+1 > limits.max_files or total+extra > limits.max_bytes:
            raise Refused('phase input file or byte capacity bound reached')

    def read(self, name, *, uid, gid, mode, limit, expected=None):
        fd = None
        try:
            self.check()
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=self.fd)
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                    or gid is not None and before.st_gid != gid
                    or stat.S_IMODE(before.st_mode) != mode or before.st_nlink != 1
                    or not 0 < before.st_size <= limit
                    or expected is not None and _fingerprint(before) != expected):
                raise Refused('phase input private regular file identity or bound mismatch')
            chunks, remaining = [], limit+1
            while remaining:
                chunk = os.read(fd, min(remaining, 8192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b''.join(chunks)
            after, named = os.fstat(fd), self.stat(name)
            if (named is None or _fingerprint(before) != _fingerprint(after)
                    or _fingerprint(before) != _fingerprint(named)
                    or len(raw) != before.st_size or len(raw) > limit):
                raise Refused('phase input file changed while reading')
            self.check()
            return raw, _fingerprint(after)
        except OSError:
            raise Refused('phase input private file unavailable') from None
        finally:
            if fd is not None:
                os.close(fd)

    @contextmanager
    def pin(self, name, expected):
        """Удерживать inode через capture: unlink/recreate не переиспользует st_ino."""
        fd = None
        try:
            self.check()
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=self.fd)
            named = self.stat(name)
            if (named is None or _fingerprint(os.fstat(fd)) != expected
                    or _fingerprint(named) != expected):
                raise Refused('phase input cleanup pinned physical receipt mismatch')
            yield fd
        except OSError:
            raise Refused('phase input cleanup pinned file unavailable') from None
        finally:
            if fd is not None:
                os.close(fd)

    def create(self, name, raw, *, gid, mode, record):
        fd = None
        try:
            # Root не публикует partial bytes body group до последнего fchmod.
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.fd)
            record(_fingerprint(os.fstat(fd)))
            if gid is not None:
                os.fchown(fd, 0, gid)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise Refused('phase input exclusive write incomplete')
                view = view[written:]
            os.fchmod(fd, mode)
            os.fsync(fd)
            final = os.fstat(fd)
            named = self.stat(name)
            if (named is None or _fingerprint(final) != _fingerprint(named)
                    or final.st_nlink != 1 or final.st_size != len(raw)):
                raise Refused('phase input created file identity changed')
            os.fsync(self.fd)
            self.check()
            return _fingerprint(final)
        except OSError:
            raise Refused('phase input exclusive creation failed; retained outcome requires cleanup') from None
        finally:
            if fd is not None:
                record(_fingerprint(os.fstat(fd)))
                os.close(fd)


class _Closable:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@dataclass(eq=False)
class _Source:
    descriptor: Descriptor
    physical: tuple | None = field(default=None, repr=False)
    complete: bool = False


class NodeWriter(_Closable):
    """Только node UID, explicit Admission/Delivery; no overwrite/unknown adoption."""
    def __init__(self, ingress, *, node_uid, limits=Limits()):
        _running_as(_uid(node_uid))
        if type(limits) is not Limits:
            raise Refused('typed phase input limits required')
        self.node_uid, self.limits = node_uid, limits
        self.directory = _Directory(ingress, uid=node_uid, gid=None, mode=0o700, ancestors=(0, node_uid), locking=True)
        self._owned = {}

    @property
    def retained(self):
        return tuple(self._owned.values())

    def write(self, admission, scope, delivery):
        _running_as(self.node_uid)
        envelope = pack(admission, scope, delivery)
        if len(envelope.raw) > self.limits.max_file_bytes:
            raise Refused('phase input exceeds file byte limit')
        descriptor = envelope.descriptor
        name = descriptor.filename
        with self.directory.lock():
            previous = self._owned.get(name)
            if previous is not None:
                if not previous.complete or previous.descriptor != descriptor:
                    raise Refused('phase input retained descriptor conflict or incomplete outcome')
                raw, _ = self.directory.read(name, uid=self.node_uid, gid=None, mode=0o600,
                    limit=self.limits.max_file_bytes, expected=previous.physical)
                if digest(raw) != descriptor.sha256:
                    raise Refused('phase input retained digest conflict')
                return descriptor
            if self.directory.stat(name) is not None:
                raise Refused('unknown existing phase input cannot be overwritten or adopted')
            if len(self._owned) >= self.limits.max_files:
                raise Refused('phase input retained ownership capacity reached')
            self.directory.capacity(self.limits, len(envelope.raw))
            source = _Source(descriptor)
            self._owned[name] = source
            source.physical = self.directory.create(name, envelope.raw, gid=None, mode=0o600,
                record=lambda value: setattr(source, 'physical', value))
            source.complete = True
            return descriptor

    def forget(self, descriptor):
        """Освободить metadata slot лишь после уборки input; никаких file effects."""
        _running_as(self.node_uid)
        if type(descriptor) is not Descriptor:
            raise Refused('typed phase input descriptor required')
        with self.directory.lock():
            previous = self._owned.get(descriptor.filename)
            if previous is None or previous.descriptor != descriptor or self.directory.stat(descriptor.filename) is not None:
                raise Refused('phase input ownership can be forgotten only after exact file cleanup')
            del self._owned[descriptor.filename]

    def close(self):
        self.directory.close()


@dataclass(frozen=True, eq=False)
class Receipt:
    """Неподменяемый saved object; physical facts хранятся только внутри store."""
    descriptor: Descriptor


@dataclass(eq=False)
class _Record:
    receipt: Receipt
    source: tuple = field(repr=False)
    snapshot: tuple | None = field(default=None, repr=False)
    complete: bool = False
    input_cleaned: bool = False
    snapshot_cleaned: bool = False
    input_conflict: bool = False
    quarantine: tuple | None = field(default=None, repr=False)
    cleanup_directory: _Directory | None = field(default=None, repr=False)
    captured: bool = False
    unlinked: bool = False


class SnapshotStore(_Closable):
    """Root-owned transport, не business admission/backend verifier.

    backend_epoch/node/executor поступают из trusted installed composition, не
    из request или wire label. #745 должен связать их с verified root backend;
    phase runner/broker обязан отдельно выполнить fresh machine guard.
    """
    def __init__(self, ingress, snapshots, *, node_uid, body_uid, body_gid,
                 backend_epoch, node_id, executor_epoch, limits=Limits()):
        _running_as(0)
        self.node_uid, self.body_uid, self.body_gid = map(_uid, (node_uid, body_uid, body_gid))
        if node_uid == body_uid or type(limits) is not Limits:
            raise Refused('separate installed node/body identities and typed limits required')
        self.expected = dict(backend_epoch=canonical(backend_epoch), node_id=canonical(node_id),
                             executor_epoch=canonical(executor_epoch))
        ingress, snapshots = _path(ingress), _path(snapshots)
        if ingress == snapshots or ingress in snapshots.parents or snapshots in ingress.parents:
            raise Refused('separate installed phase input trees required')
        self.limits = limits
        self.ingress = _Directory(ingress, uid=node_uid, gid=None, mode=0o700, ancestors=(0, node_uid), locking=True)
        try:
            self.snapshots = _Directory(snapshots, uid=0, gid=body_gid, mode=0o750, ancestors=(0,), locking=True)
            if os.fstat(self.ingress.fd).st_dev != os.fstat(self.snapshots.fd).st_dev:
                raise Refused('phase input cleanup requires one installed filesystem')
        except BaseException:
            self.ingress.close()
            if hasattr(self, 'snapshots'):
                self.snapshots.close()
            raise
        self._owned = {}

    @property
    def retained(self):
        return tuple(record.receipt for record in self._owned.values())

    def snapshot(self, peer_uid, descriptor, admission):
        _running_as(0)
        if type(peer_uid) is not int or peer_uid != self.node_uid:
            raise Refused('phase input requires authenticated installed node peer UID')
        if type(descriptor) is not Descriptor:
            raise Refused('typed phase input descriptor required')
        name = descriptor.filename
        with self.ingress.lock(), self.snapshots.lock():
            raw, source = self.ingress.read(name, uid=self.node_uid, gid=None, mode=0o600,
                                           limit=self.limits.max_file_bytes)
            Envelope.parse(raw, descriptor, admission, **self.expected)
            previous = self._owned.get(name)
            if previous is not None:
                if (not previous.complete or previous.input_cleaned or previous.snapshot_cleaned
                        or previous.input_conflict or previous.receipt.descriptor != descriptor or previous.source != source):
                    raise Refused('phase input retained physical receipt conflict')
                copied, _ = self.snapshots.read(name, uid=0, gid=self.body_gid, mode=0o440,
                    limit=self.limits.max_file_bytes, expected=previous.snapshot)
                if copied != raw:
                    raise Refused('phase input retained snapshot digest conflict')
                return previous.receipt
            if self.snapshots.stat(name) is not None:
                raise Refused('unknown existing root snapshot cannot be overwritten or adopted')
            if len(self._owned) >= self.limits.max_files:
                raise Refused('root snapshot retained ownership capacity reached')
            self.snapshots.capacity(self.limits, len(raw))
            record = _Record(Receipt(descriptor), source)
            self._owned[name] = record
            record.snapshot = self.snapshots.create(name, raw, gid=self.body_gid, mode=0o440,
                record=lambda value: setattr(record, 'snapshot', value))
            # Проверка source после copy ловит mutation/substitution до публикации ACK.
            current, physical = self.ingress.read(name, uid=self.node_uid, gid=None, mode=0o600,
                limit=self.limits.max_file_bytes, expected=source)
            if current != raw or physical != source:
                raise Refused('phase input source changed during root snapshot creation')
            Envelope.parse(raw, descriptor, admission, **self.expected)
            record.complete = True
            return record.receipt

    def _receipt(self, receipt):
        _running_as(0)
        if type(receipt) is not Receipt:
            raise Refused('exact retained phase input physical receipt required')
        record = self._owned.get(receipt.descriptor.filename)
        if record is None or record.receipt is not receipt:
            raise Refused('exact retained phase input physical receipt required')
        return receipt.descriptor.filename, record

    def cleanup_input(self, receipt):
        """Убирает только captured inode, даже после expiry; не останавливает body.

        Между stat и unlink в node-owned namespace нет compare-and-delete syscall.
        Поэтому сначала rename в root-only namespace. Подменённый inode никогда
        не удаляется: безопасная попытка restore без overwrite, quarantine retained.
        """
        name, record = self._receipt(receipt)
        if record.input_cleaned:
            return True
        if record.input_conflict:
            raise Refused('phase input cleanup has retained foreign quarantine conflict')
        if record.captured:
            with self.snapshots.lock():
                self._receipt(receipt)
                self._finish_input_cleanup(name, record)
                return True
        with self.ingress.lock(), self.snapshots.lock(), self.ingress.pin(name, record.source):
            self._receipt(receipt)
            self.ingress.read(name, uid=self.node_uid, gid=None, mode=0o600,
                limit=self.limits.max_file_bytes, expected=record.source)
            quarantine = name+'.cleanup'
            if record.cleanup_directory is None:
                if self.snapshots.stat(quarantine) is not None:
                    raise Refused('unknown phase input cleanup destination exists')
                os.mkdir(quarantine, mode=0o700, dir_fd=self.snapshots.fd)
                os.chown(quarantine, 0, 0, dir_fd=self.snapshots.fd, follow_symlinks=False)
                record.cleanup_directory = _Directory(self.snapshots.path/quarantine,
                    uid=0, gid=0, mode=0o700, ancestors=(0,))
            private = record.cleanup_directory
            private.check()
            if private.stat(quarantine) is not None:
                raise Refused('unknown phase input quarantine entry exists')
            # Даже world-readable foreign replacement не виден body: capture
            # происходит внутрь root-only 0700, не в group-readable snapshot dir.
            # Перенос использует ingress allocation, а не новый file/byte budget.
            os.rename(name, quarantine, src_dir_fd=self.ingress.fd, dst_dir_fd=private.fd)
            record.captured = True
            captured = private.stat(quarantine)
            actual = _fingerprint(captured) if captured is not None else None
            # rename меняет ctime; pinned inode не допускает st_ino reuse в окне.
            if actual is None or actual[:-1] != record.source[:-1]:
                record.input_conflict = True
                try:
                    os.link(quarantine, name, src_dir_fd=private.fd,
                            dst_dir_fd=self.ingress.fd, follow_symlinks=False)
                except OSError:
                    raise Refused('phase input cleanup captured foreign replacement; restore unavailable, quarantine retained') from None
                raise Refused('phase input cleanup captured foreign replacement; restored without overwrite, quarantine retained')
            record.quarantine = actual
            self._finish_input_cleanup(name, record)
            return True

    def _finish_input_cleanup(self, name, record):
        quarantine = name+'.cleanup'
        private = record.cleanup_directory
        if private is None:
            raise Refused('phase input cleanup lost its private directory receipt')
        private.check()
        if not record.unlinked:
            current = private.stat(quarantine)
            physical = _fingerprint(current) if current is not None else None
            if record.quarantine is None and physical is not None and physical[:-1] == record.source[:-1]:
                record.quarantine = physical
            if physical is None or physical != record.quarantine:
                raise Refused('phase input captured cleanup physical receipt mismatch')
            os.unlink(quarantine, dir_fd=private.fd)
            record.unlinked = True
            record.quarantine = None
        # Путь root-owned и check закрепил тот же directory inode. Failed rmdir
        # оставляет receipt для повторной уборки, не ложный cleanup ACK.
        private.check()
        os.rmdir(quarantine, dir_fd=self.snapshots.fd)
        private.close()
        record.cleanup_directory = None
        record.input_cleaned = True

    def cleanup_snapshot(self, receipt):
        name, record = self._receipt(receipt)
        if record.snapshot_cleaned:
            return True
        with self.snapshots.lock():
            self._receipt(receipt)
            current = self.snapshots.stat(name)
            if record.snapshot is None or current is None or _fingerprint(current) != record.snapshot:
                raise Refused('phase input cleanup snapshot physical receipt mismatch')
            # Namespace root-owned, lock общий для всех store processes/threads.
            os.unlink(name, dir_fd=self.snapshots.fd)
            record.snapshot_cleaned = True
            return True

    def forget(self, receipt):
        """Отдельно освободить bounded retention slot после двух exact cleanup ACK."""
        name, record = self._receipt(receipt)
        with self.ingress.lock(), self.snapshots.lock():
            self._receipt(receipt)
            if not record.input_cleaned or not record.snapshot_cleaned or record.input_conflict:
                raise Refused('phase input retention requires completed physical cleanup')
            del self._owned[name]

    def close(self):
        for record in self._owned.values():
            if record.cleanup_directory is not None:
                record.cleanup_directory.close()
        self.ingress.close()
        self.snapshots.close()


class BodyReader(_Closable):
    """Строгий actual delivery parse после root ownership/digest checks в body UID."""
    def __init__(self, snapshots, *, body_uid, body_gid, backend_epoch, node_id, executor_epoch):
        self.body_uid, self.body_gid = _uid(body_uid), _uid(body_gid)
        _running_as(body_uid)
        self.expected = dict(backend_epoch=canonical(backend_epoch), node_id=canonical(node_id),
                             executor_epoch=canonical(executor_epoch))
        self.directory = _Directory(snapshots, uid=0, gid=body_gid, mode=0o750, ancestors=(0,))

    def read(self, descriptor, admission):
        _running_as(self.body_uid)
        if type(descriptor) is not Descriptor:
            raise Refused('typed phase input descriptor required')
        # Body не открывает private writer lock; readable directory не служит lock inode.
        raw, _ = self.directory.read(descriptor.filename, uid=0, gid=self.body_gid,
                                    mode=0o440, limit=MAX_FILE_BYTES)
        return parse_body(Envelope.parse(raw, descriptor, admission, **self.expected))

    def close(self):
        self.directory.close()
