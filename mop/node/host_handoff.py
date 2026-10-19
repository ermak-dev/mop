"""Два fixed private handoff leaves; не issuer и не physical authority.

Root caller обязан bracket'ить операции exact retained attachment/current check.
Здесь только filesystem ownership: private staging вне body namespace, no-replace
publication и process-local receipt. Close не retire/cleanup living body.
"""
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import errno
import os
from pathlib import Path
import stat

from mop.common.management_context import BodyBootstrap, ManagementContext
from mop.node.host_input_files import _Directory, _fingerprint, _running_as
from mop.node.host_inputs import canonical_json
from mop.node.host_scope import Installed, Scope, Refused


def _now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LaunchWindow:
    launched_at: datetime
    intent_expiry: datetime

    def __post_init__(self):
        now = _now()
        if (any(type(value) is not datetime or value.tzinfo is not timezone.utc
                for value in (self.launched_at, self.intent_expiry))
                or self.launched_at > now or self.deadline <= now):
            raise Refused('original live UTC native launch window required')

    @property
    def deadline(self):
        return min(self.intent_expiry, self.launched_at+timedelta(seconds=300))

    def check(self):
        if _now() >= self.deadline:
            raise Refused('original native management handoff deadline expired')


def _rename_absent(source_fd, destination_fd, leaf):
    """Linux atomic move одного fixed inode, без overwrite и nlink=2 окна."""
    if leaf not in ('bootstrap', 'context'):
        raise Refused('fixed native handoff leaf required')
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, 'renameat2', None)
    if rename is None:
        raise Refused('atomic no-replace native handoff publication is unavailable')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    name = leaf.encode('ascii')
    if rename(source_fd, name, destination_fd, name, 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise Refused('unknown existing native handoff destination cannot be adopted or replaced')
        raise OSError(code, 'native handoff atomic publication failed')


@dataclass(frozen=True, repr=False)
class HandoffReceipt:
    scope: Scope
    leaf: str
    physical: tuple


@dataclass(repr=False)
class _Record:
    raw: bytes = field(repr=False)
    fd: int
    physical: tuple
    receipt: HandoffReceipt | None = None
    complete: bool = False
    failed: bool = False


class HandoffOwner:
    """Installed root composition, только bootstrap/context без path/raw RPC.

    Один owner на launch, не persistent authority. Не усыновляет old directories
    или неизвестные leaves; private staging остаётся retained при unknown cleanup.
    """
    def __init__(self, installed, scope, workspace, staging_root, bootstrap, window):
        _running_as(0)
        self.home = self.runtime = self.management = self.staging = self.stage_parent = None
        self.records = {}
        self.closed = False
        if (type(installed) is not Installed or type(scope) is not Scope
                or type(window) is not LaunchWindow or type(bootstrap) is not BodyBootstrap
                or type(workspace) is not tuple or len(workspace) != 2
                or any(type(value) is not int or value < 0 for value in workspace)
                or bootstrap.body_uid != installed.body_uid
                or any(str(getattr(bootstrap.request.scope, key)) != getattr(scope, key) for key in
                       ('epoch', 'node_id', 'executor_epoch', 'target_id', 'incarnation_id'))
                or bootstrap.expires_at > window.intent_expiry):
            raise Refused('exact installed native workspace/bootstrap/original window required')
        self.installed, self.scope, self.workspace = installed, scope, workspace
        self.bootstrap, self.window = bootstrap, window
        try:
            window.check()
            body = installed.body(scope)
            args = dict(uid=installed.body_uid, gid=installed.body_gid, mode=0o700,
                        ancestors=(0, installed.body_uid))
            self.home = _Directory(body, **args)
            if self.home.identity != workspace:
                raise Refused('native handoff workspace inode differs from retained body')
            self.runtime = _Directory(body/'r', **args)
            self.management = _Directory(body/'r/m', **args)
            # Staging parent — отдельный installed root-owned каталог. Никаких
            # private drafts в namespace body: даже body-owned 0400 inode скрыт.
            staging_root = Path(staging_root)
            mode = stat.S_IMODE(staging_root.lstat().st_mode)
            if mode not in (0o700, 0o750):
                raise Refused('private installed root handoff staging directory required')
            self.stage_parent = _Directory(staging_root, uid=0, gid=None, mode=mode, ancestors=(0,))
            name = scope.launch_id+'.handoff'
            os.mkdir(name, 0o700, dir_fd=self.stage_parent.fd)
            self.staging = _Directory(staging_root/name, uid=0, gid=0, mode=0o700, ancestors=(0,))
            if len({directory.identity[0] for directory in (self.home, self.management, self.staging)}) != 1:
                raise Refused('atomic native handoff requires the same installed filesystem')
            if any(self.management.stat(leaf) is not None for leaf in ('bootstrap', 'context')):
                raise Refused('native handoff requires absent fixed leaves; no adoption')
        except OSError:
            self.close()
            raise Refused('native handoff private directories unavailable') from None
        except BaseException:
            self.close()
            raise

    def _check(self):
        _running_as(0)
        if self.closed:
            raise Refused('native handoff owner is closed')
        self.window.check()
        for directory in (self.home, self.runtime, self.management, self.stage_parent, self.staging):
            directory.check()
        if self.home.identity != self.workspace:
            raise Refused('native handoff lost its retained workspace')

    def _record_bytes(self, record):
        before = os.fstat(record.fd)
        raw = os.pread(record.fd, len(record.raw)+1, 0)
        after = os.fstat(record.fd)
        if (_fingerprint(before) != _fingerprint(after) or raw != record.raw
                or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                or (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) !=
                   (self.installed.body_uid, self.installed.body_gid, 0o400)):
            raise Refused('retained native handoff inode content or private ownership changed')
        return _fingerprint(after)

    def _published(self, leaf, record):
        value = self.management.stat(leaf)
        if value is None:
            return False
        physical = self._record_bytes(record)
        if _fingerprint(value) != physical or physical[:2] != record.physical[:2]:
            raise Refused('native handoff destination differs from exact retained publication inode')
        if record.receipt is not None and record.receipt.physical != physical:
            raise Refused('native handoff published inode changed; rebind forbidden')
        # Сохраняется только exact inode, даже если syscall ACK был потерян.
        os.fsync(self.management.fd)
        os.fsync(self.staging.fd)
        if record.receipt is None:
            record.receipt = HandoffReceipt(self.scope, leaf, physical)
        return True

    def _publish(self, leaf, raw, maximum):
        self._check()
        if not 0 < len(raw) <= maximum:
            raise Refused('bounded native handoff value required')
        record = self.records.get(leaf)
        if record is not None and (record.raw != raw or record.failed):
            raise Refused('native handoff cannot replace its retained value or replay a partial write')
        try:
            if record is None:
                if self.management.stat(leaf) is not None:
                    raise Refused('unknown existing native handoff destination cannot be adopted or replaced')
                fd = os.open(leaf, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,
                             0o600, dir_fd=self.staging.fd)
                record = _Record(raw, fd, _fingerprint(os.fstat(fd)))
                self.records[leaf] = record
                try:
                    position = 0
                    while position < len(raw):
                        count = os.write(fd, raw[position:])
                        if count <= 0:
                            raise OSError('native handoff staging write did not progress')
                        position += count
                    os.fchown(fd, self.installed.body_uid, self.installed.body_gid)
                    os.fchmod(fd, 0o400)
                    os.fsync(fd)
                    record.physical = self._record_bytes(record)
                    record.complete = True
                except BaseException:
                    record.failed = True
                    raise
            if not record.complete:
                raise Refused('native handoff has no completed private staging receipt')
            if not self._published(leaf, record):
                if self.staging.stat(leaf) is None or _fingerprint(self.staging.stat(leaf)) != record.physical:
                    raise Refused('native handoff private staging inode changed')
                self._check()
                _rename_absent(self.staging.fd, self.management.fd, leaf)
                if not self._published(leaf, record):
                    raise Refused('native handoff publication has no exact destination receipt')
            self._check()
            return record.receipt
        except OSError:
            # Retained fd/value остаются владельцем ambiguous rename; retry
            # проверяет тот же inode, не переписывает и не выпускает новый token.
            raise Refused('native handoff publication unavailable; original ownership retained') from None

    def publish_bootstrap(self):
        return self._publish('bootstrap', canonical_json(self.bootstrap.wire()), 65536)

    def publish_context(self, context):
        self._check()
        if self.records.get('bootstrap') is None or self.records['bootstrap'].receipt is None:
            raise Refused('native context requires exact prior bootstrap publication')
        # DTO не permissions snapshot. Current server read и physical bracketing
        # остаются обязанностью caller; здесь не принимается arbitrary subject.
        if (type(context) is not ManagementContext or context.request != self.bootstrap.request
                or any(getattr(context, key) != getattr(self.bootstrap, key) for key in
                       ('installation_id', 'restore_generation', 'project_id', 'grant_id'))
                or context.expires_at <= _now()):
            raise Refused('native context differs from exact original bootstrap binding or expired')
        if not self._published('bootstrap', self.records['bootstrap']):
            raise Refused('native context lost its exact prior bootstrap publication')
        return self._publish('context', canonical_json(context.wire()), 16384)

    @property
    def retained(self):
        return tuple(record.receipt for record in self.records.values() if record.receipt is not None)

    def close(self):
        # Close не уничтожает living lifetime или его файлы и не выдаёт cleanup
        # ACK. Retire должен сначала доказать exact empty cgroup существующим owner.
        if self.closed:
            return
        self.closed = True
        for record in self.records.values():
            os.close(record.fd)
        for directory in (self.management, self.runtime, self.home, self.staging, self.stage_parent):
            if directory is not None:
                directory.close()
