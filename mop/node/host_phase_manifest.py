"""Root-installed runtime content и честные HOST resource units, не image UUID."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from decimal import Decimal, InvalidOperation

from mop.node.host_inputs import _json, _sha, canonical_json
from mop.node.host_scope import canonical, exact
from mop.node.http_execution import Refused


def duration_usec(value):
    """Точный finite systemd readback без float округления единиц."""
    if type(value) is not str or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:us|ms|s)', value):
        raise Refused('finite exact systemd CPU duration required')
    unit = next(u for u in ('us', 'ms', 's') if value.endswith(u))
    try:
        amount = Decimal(value[:-len(unit)]) * {'us': 1, 'ms': 1000, 's': 1000000}[unit]
        if not 0 < amount < 2**63 or amount != amount.to_integral_value():
            raise ValueError
        return int(amount)
    except (ValueError, InvalidOperation):
        raise Refused('finite exact systemd CPU duration required') from None


@dataclass(frozen=True)
class ResourceLimits:
    memory_max: int
    cpu_quota_usec: int
    cpu_weight: int
    tasks_max: int
    disk_reservation_bytes: int
    disk_mode: str = 'shared-reservation'
    disk_isolation: bool = False

    def properties(self):
        # CPUQuotaPerSecUSec — readback; systemd-run принимает проценты.
        percent = format(Decimal(self.cpu_quota_usec)/10000, 'f').rstrip('0').rstrip('.') if self.cpu_quota_usec % 10000 else str(self.cpu_quota_usec//10000)
        return dict(MemoryMax=str(self.memory_max), TasksMax=str(self.tasks_max),
            CPUWeight=str(self.cpu_weight), CPUQuota=percent+'%', CPUQuotaPeriodSec='100ms')

    def verify(self, values):
        exact(values, ('MemoryMax', 'TasksMax', 'CPUWeight', 'CPUQuotaPerSecUSec', 'CPUQuotaPeriodUSec'))
        if (values['MemoryMax'] != str(self.memory_max) or values['TasksMax'] != str(self.tasks_max)
                or values['CPUWeight'] != str(self.cpu_weight)
                or duration_usec(values['CPUQuotaPerSecUSec']) != self.cpu_quota_usec
                or duration_usec(values['CPUQuotaPeriodUSec']) != 100000):
            raise Refused('actual HOST memory/tasks/CPU limits differ from installed capacity projection')

    def wire(self): return dict(self.__dict__)


@dataclass(frozen=True)
class Capacity:
    cpu_mhz_per_core: int
    cpu_weight: int
    tasks_max: int
    memory_mb_max: int
    cpu_mhz_max: int
    disk_mode: str

    def __post_init__(self):
        if (any(type(v) is not int or not 0 < v < 2**31 for v in
                (self.cpu_mhz_per_core, self.cpu_weight, self.tasks_max, self.memory_mb_max, self.cpu_mhz_max))
                or not 1 <= self.cpu_weight <= 10000 or not 8 <= self.tasks_max <= 128
                or self.disk_mode != 'shared-reservation'):
            raise Refused('explicit installed CPU capacity and shared-reservation disk profile required; isolation unavailable')

    def project(self, policy):
        exact(policy, ('policy_id', 'cpu', 'memory_mb', 'disk_mb'))
        canonical(policy['policy_id'])
        if (any(type(policy[k]) is not int or not 1 <= policy[k] < 2**31 for k in ('cpu', 'memory_mb', 'disk_mb'))
                or policy['cpu'] > self.cpu_mhz_max or policy['memory_mb'] > self.memory_mb_max):
            raise Refused('planned resources exceed installed capacity profile')
        # systemd parse_permyriad: две десятичные цифры процента. Округление
        # только вниз до 100us/sec, никогда не обещать больше выбранного CPU.
        quota = (policy['cpu']*1000000//self.cpu_mhz_per_core)//100*100
        if quota < 10000:
            raise Refused('planned CPU is below supported systemd quota resolution')
        return ResourceLimits(policy['memory_mb']*1048576, quota, self.cpu_weight,
                              self.tasks_max, policy['disk_mb']*1048576)


def path(value):
    if (type(value) is not str or not value.startswith('/') or '..' in Path(value).parts
            or len(value) > 240 or not value.isascii() or any(c.isspace() for c in value)
            or Path(value) == Path('/')):
        raise Refused('fixed absolute installed runtime path required')
    return Path(value)


def verify_artifact(filename, expected, *, root_owned=True, executable=False):
    """Не-follow всей цепочки, hash actual bytes и stability на том же inode."""
    from mop.node.host_scope_systemd import open_directory
    filename = path(str(filename))
    _sha(expected)
    directory = open_directory(filename.parent, owner=0 if root_owned else None, immutable=root_owned)
    try:
        fd = os.open(filename.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or root_owned and (before.st_uid != 0 or before.st_mode & 0o022)
                    or before.st_mode & 0o6000 or executable and not before.st_mode & 0o111
                    or not 0 <= before.st_size <= 67108864):
                raise Refused('immutable bounded single-link installed runtime artifact required')
            h = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                raw = os.read(fd, min(65536, remaining))
                if not raw: raise Refused('installed runtime artifact truncated')
                h.update(raw)
                remaining -= len(raw)
            after = os.fstat(fd)
            keys = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if any(getattr(before, k) != getattr(after, k) for k in keys) or os.read(fd, 1) or h.hexdigest() != expected:
                raise Refused('installed runtime artifact content changed')
            return expected
        finally:
            os.close(fd)
    except OSError as error:
        raise Refused('installed runtime artifact unavailable: '+type(error).__name__) from None
    finally:
        os.close(directory)


TOOLS = ('python', 'git', 'ssh', 'ansible', 'runner', 'ca', 'ansible_config')
DIRECTORIES = ('empty', 'git_exec', 'python_root', 'ansible_plugins')


@dataclass(frozen=True)
class RuntimeManifest:
    capacity: Capacity
    images: tuple
    tools: dict
    directories: dict
    artifacts: tuple
    content_digest: str

    @classmethod
    def parse(cls, raw):
        value = _json(raw)
        exact(value, ('schema_version', 'capacity', 'images', 'tools', 'directories', 'artifacts'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('unsupported installed runtime manifest')
        exact(value['capacity'], Capacity.__dataclass_fields__)
        capacity = Capacity(**value['capacity'])
        exact(value['tools'], TOOLS)
        exact(value['directories'], DIRECTORIES)
        tools = {k: path(v) for k, v in value['tools'].items()}
        directories = {k: path(v) for k, v in value['directories'].items()}
        artifacts = value['artifacts']
        if type(artifacts) is not list or not 1 <= len(artifacts) <= 16384:
            raise Refused('explicit bounded runtime content closure required')
        names = set()
        for item in artifacts:
            exact(item, ('path', 'sha256', 'executable'))
            filename = path(item['path'])
            _sha(item['sha256'])
            if type(item['executable']) is not bool or filename in names:
                raise Refused('unique installed runtime artifact required')
            names.add(filename)
        if not set(tools.values()) <= names:
            raise Refused('every fixed runtime executable/input requires an actual content hash')
        executables = {item['path'] for item in artifacts if item['executable']}
        if any(str(tools[key]) not in executables for key in TOOLS if key not in ('ca', 'ansible_config')):
            raise Refused('every fixed runtime executable requires executable content proof')
        images = value['images']
        if type(images) is not list or not 1 <= len(images) <= 256:
            raise Refused('explicit installed image mapping required')
        seen = set()
        for image in images:
            exact(image, ('image_revision', 'logical_pin', 'policy_id'))
            key = (canonical(image['image_revision']), canonical(image['policy_id']))
            if (key in seen or image['logical_pin'] != hashlib.sha256(('image-revision:'+key[0]).encode()).hexdigest()):
                raise Refused('installed logical image mapping mismatch')
            seen.add(key)
        return cls(capacity, tuple(images), tools, directories, tuple(artifacts), hashlib.sha256(canonical_json(value)).hexdigest())

    @classmethod
    def load(cls, filename):
        from mop.node.host_root import _read
        try:
            result = cls.parse(_read(filename, limit=4194304))
            result.verify()
            return result
        except (OSError, RuntimeError) as error:
            raise Refused('installed runtime manifest unavailable: '+type(error).__name__) from None

    def verify(self):
        from mop.node.host_scope_systemd import open_directory
        names = {item['path'] for item in self.artifacts}
        for item in self.artifacts:
            verify_artifact(item['path'], item['sha256'], executable=item['executable'])
        for key, directory in self.directories.items():
            fd = open_directory(directory, owner=0, immutable=True)
            os.close(fd)
            if key == 'empty':
                if any(directory.iterdir()): raise Refused('installed Git templates/hooks directory must be empty')
                continue
            # Только root-owned installed trees, никогда project-controlled workspace.
            pending, count = [directory], 0
            while pending:
                current = pending.pop()
                fd = open_directory(current, owner=0, immutable=True)
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            count += 1
                            if count > 20000: raise Refused('installed runtime closure exceeds bound')
                            filename = current/entry.name
                            if entry.is_dir(follow_symlinks=False): pending.append(filename)
                            elif str(filename) not in names:
                                raise Refused('unhashed installed runtime entry is unavailable')
                finally:
                    os.close(fd)

    def require_release(self, release):
        """Receipt включает весь fixed product closure, не только сторонние tools."""
        from mop.node.host_body import BODY_SOURCES
        from mop.node.host_root import ROOT_SOURCES, PHASE_ROOT_SOURCES
        required = {str(release/name) for name in set(ROOT_SOURCES) | set(PHASE_ROOT_SOURCES) | set(BODY_SOURCES)}
        if (self.tools['runner'] != release/'mop/node/host_body.py'
                or not required <= {item['path'] for item in self.artifacts}):
            raise Refused('installed runtime must bind the exact lifecycle broker and complete root/body source closure')

    def project(self, projection):
        if not any((item['image_revision'], item['logical_pin'], item['policy_id']) ==
                   (projection.image_revision, projection.image_digest, projection.image_policy['policy_id']) for item in self.images):
            raise Refused('selected logical image/policy has no immutable installed runtime mapping')
        return self.capacity.project(projection.image_policy)
