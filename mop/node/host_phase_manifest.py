"""Root-installed runtime content и честные HOST resource units, не image UUID."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from decimal import Decimal, InvalidOperation

from mop.common import installed_content
from mop.common.installed_content import TOOLS, DIRECTORIES, NATIVE_TOOLS, RuntimeContent
from mop.node.host_inputs import _json, canonical_json
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


def verify_artifact(filename, expected, *, root_owned=True, executable=False, native_size=None):
    """Один read-only verifier общий с isolated body MCP; node сохраняет Refused."""
    try:
        return installed_content.verify_artifact(filename, expected, root_owned=root_owned,
            executable=executable, native_size=native_size)
    except ValueError as error:
        raise Refused(str(error)) from None


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
        try:
            content = RuntimeContent.parse(value)
        except ValueError as error:
            raise Refused(str(error)) from None
        exact(value['capacity'], Capacity.__dataclass_fields__)
        capacity = Capacity(**value['capacity'])
        tools, directories, artifacts = content.tools, content.directories, content.artifacts
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
        try:
            RuntimeContent(self.tools, self.directories, self.artifacts).verify()
        except ValueError as error:
            raise Refused(str(error)) from None

    def require_release(self, release):
        """Receipt включает весь fixed product closure, не только сторонние tools."""
        from mop.node.host_body import BODY_SOURCES
        from mop.node.host_root import ROOT_SOURCES, PHASE_ROOT_SOURCES
        required = {str(release/name) for name in set(ROOT_SOURCES) | set(PHASE_ROOT_SOURCES) | set(BODY_SOURCES)}
        if (self.tools['runner'] != release/'mop/node/host_body.py'
                or not required <= {item['path'] for item in self.artifacts}):
            raise Refused('installed runtime must bind the exact lifecycle broker and complete root/body source closure')

    def require_native_release(self, release):
        """Native profile привязан к fixed hook/MCP entry и его product imports."""
        self.require_release(release)
        try:
            RuntimeContent(self.tools, self.directories, self.artifacts).require_mcp(release)
        except ValueError as error:
            raise Refused(str(error)) from None

    def verify_mcp_declarations(self, release):
        """CLI discovery читает AST, но каждый discovered file тоже hashed input."""
        self.require_native_release(release)
        try:
            RuntimeContent(self.tools, self.directories, self.artifacts).verify_tree(release/'mop/cli', maximum=4096)
        except ValueError as error:
            raise Refused(str(error)) from None

    def project(self, projection):
        if not any((item['image_revision'], item['logical_pin'], item['policy_id']) ==
                   (projection.image_revision, projection.image_digest, projection.image_policy['policy_id']) for item in self.images):
            raise Refused('selected logical image/policy has no immutable installed runtime mapping')
        return self.capacity.project(projection.image_policy)
