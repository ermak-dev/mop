"""Единый byte profile finite initial launch (#751), без ORM/node imports.

256 KiB остаются обычным phase budget. Только bootstrap_body получает до
1 MiB: maximum retained canonical 537974 -> base64 717300 bytes, плюс
конкретные selected metadata/consumers и private framing. Это не обещание,
что любые 32 секрета по 1 MiB поместятся: admission проверяет весь aggregate.
HTTP/private hard ceilings не меняются, credential frame по-прежнему 2 MiB.
"""
from dataclasses import dataclass
import hashlib
import json

from .credentials import MAX_FRAME
from .initialcreate import MACHINE

DEFAULT_BYTES = 262144
BOOTSTRAP_BYTES = 1048576
# То же finite purpose имя, которым root связывает opaque input с admission.
PURPOSES = {action: 'initial_'+action+'_v1' for action in
            ('prepare_body', 'bootstrap_body', 'initial_identity', 'native_exec')}
_UUID = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
_COUNTER = str(2**63-1)
# Production Work/Admission сериализуются из UTC datetime, максимум 32 ASCII.
_STAMP = '9999-12-31T23:59:59.999999+00:00'


class TransferError(ValueError):
    """Конечный безопасный отказ, не private content или частичный response."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')


def base64_size(size):
    if type(size) is not int or not 0 <= size <= 1048576:
        raise TransferError('initial launch transfer requires a bounded retained size')
    return 4*((size+2)//3)


def phase_limit(phase):
    if type(phase) is not str or phase not in MACHINE:
        raise TransferError('initial launch transfer requires a finite phase')
    return BOOTSTRAP_BYTES if phase == 'bootstrap_body' else DEFAULT_BYTES


def bound_work(role, digest, phase):
    """UUID=36, positive int64=19, UTC datetime=32; не guessed overhead."""
    phase_limit(phase)
    return dict(schema_version=1, epoch=_UUID, operation_id=_UUID, step_id=_UUID,
        target_id=_UUID, incarnation_id=_UUID, node_id=_UUID, executor_epoch=_UUID,
        owner_id=_COUNTER, generation=_COUNTER, revision=_COUNTER, role=role,
        bundle_digest=digest, grant_id=_UUID, action=phase, not_after=_STAMP)


def private_frame(payload, *, phase, scope, admission):
    """Exact outer framing; root видит payload только как opaque ASCII string."""
    return canonical_json(dict(schema_version=1, purpose=PURPOSES[phase], scope=scope,
        admission=admission, payload_digest=hashlib.sha256(payload).hexdigest(),
        payload=payload.decode('ascii')))


@dataclass(frozen=True)
class ByteProfile:
    http: int
    credential: int
    private: int

    def add_binary(self, size):
        # Base64 alphabet не содержит JSON quote/backslash: оба JSON layers
        # добавляют ровно столько же bytes. Ключи/quotes уже посчитаны measure.
        extra = base64_size(size)
        return ByteProfile(self.http+extra, self.credential+extra,
            self.private+extra if self.private else 0)

    def require(self, phase):
        limit = phase_limit(phase)
        # MAX_FRAME относится к JSON body; ещё четыре bytes -- length prefix.
        if self.http > limit or self.private > limit or self.credential > MAX_FRAME+4:
            raise TransferError('initial launch transfer exceeds the aggregate phase byte budget')
        return self


def measure(value, *, scope=None, admission=None):
    """Exact ASCII response/frame; без claim -- доказанная верхняя граница.

    DRF UTF-8 JSON не длиннее ASCII JSON того же объекта. Размер credential
    response точен независимо от порядка keys. Никакого fixed size-overhead.
    """
    payload = canonical_json(value)
    private = 0
    phase = value['phase']
    phase_limit(phase)
    if phase in PURPOSES:
        work = bound_work(value['work']['role'], value['work']['bundle_digest'], phase)
        if scope is None:
            scope = {key: work[key] for key in ('epoch', 'node_id', 'executor_epoch', 'target_id', 'incarnation_id')}
            scope['launch_id'] = work['operation_id']
        if admission is None:
            admission = dict(work=work, request_id=_UUID, attempt_id=_UUID,
                revision=_COUNTER, lease_until=_STAMP)
        private = len(private_frame(payload, phase=phase, scope=scope, admission=admission))
    return ByteProfile(len(payload), len(canonical_json(dict(ok=True, result=value)))+4, private)
