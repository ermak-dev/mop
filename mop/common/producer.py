"""Конечный несекретный отчёт trusted node runtime, не native CLI протокол.

pid/start/pane -- наблюдение enrolled узла. Контроллер сверяет authority и SQL
fences, но не выдаёт принятие этого отчёта за удалённую проверку ОС.
"""
from dataclasses import dataclass
import re
from uuid import UUID

from .telemetry import SessionScope, counter, fields, positive, uuid


@dataclass(frozen=True)
class ProducerReport:
    scope: SessionScope
    bootstrap_revision: UUID
    bootstrap_digest: str
    image_digest: str
    display_name: str
    pane: str
    pid: int
    process_start: str

    def __post_init__(self):
        if (type(self.scope) is not SessionScope or type(self.bootstrap_revision) is not UUID
                or any(type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value)
                       for value in (self.bootstrap_digest, self.image_digest))
                or type(self.display_name) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}', self.display_name)
                or type(self.pane) is not str or not re.fullmatch('%[0-9]{1,10}', self.pane)
                or type(self.pid) is not int or not 0 < self.pid < 2**31
                or type(self.process_start) is not str or len(self.process_start) > 56
                or not re.fullmatch('[A-Za-z0-9_-]{1,128}', self.scope.session_id)):
            raise ValueError('finite observed physical producer and launch pins required')
        parts = self.process_start.split(':')
        if len(parts) != 2:
            raise ValueError('observed boot UUID and process start ticks required')
        uuid(parts[0])
        counter(parts[1])
        if self.scope.producer_epoch == self.scope.executor_epoch:
            raise ValueError('independent producer epoch required')

    def wire(self):
        return dict(scope=self.scope.wire(), launch=dict(bootstrap_revision=str(self.bootstrap_revision),
            bootstrap_digest=self.bootstrap_digest, image_digest=self.image_digest, display_name=self.display_name),
            physical=dict(pane=self.pane, pid=str(self.pid), process_start=self.process_start))

    @classmethod
    def parse(cls, value):
        fields(value, ('scope', 'launch', 'physical'))
        launch, physical = value['launch'], value['physical']
        fields(launch, ('bootstrap_revision', 'bootstrap_digest', 'image_digest', 'display_name'))
        fields(physical, ('pane', 'pid', 'process_start'))
        return cls(SessionScope.parse(value['scope']), uuid(launch['bootstrap_revision']),
            launch['bootstrap_digest'], launch['image_digest'], launch['display_name'],
            physical['pane'], counter(physical['pid']), physical['process_start'])


def predecessor(scope, revision):
    if (type(revision) is not int or not 0 <= revision < 2**63
            or (scope is None) != (revision == 0)
            or scope is not None and type(scope) is not SessionScope):
        raise ValueError('exact predecessor scope and revision required; null only for first registration')


@dataclass(frozen=True)
class ProducerAck:
    scope: SessionScope
    revision: int

    def __post_init__(self):
        if type(self.scope) is not SessionScope:
            raise ValueError('exact producer scope required')
        positive(self.revision)

    def wire(self):
        return dict(schema_version=1, scope=self.scope.wire(), revision=str(self.revision))

    @classmethod
    def parse(cls, value):
        fields(value, ('schema_version', 'scope', 'revision'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('unsupported producer acknowledgement')
        return cls(SessionScope.parse(value['scope']), counter(value['revision']))
