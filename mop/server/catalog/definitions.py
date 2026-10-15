"""Конечная схема настроек; bootstrap и секреты здесь не определяются."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Definition:
    kind: str
    node_override: bool = False
    minimum: int = 0
    maximum: int = 0
    choices: tuple = ()

    def validate(self, value):
        expected = {'int': int, 'text': str, 'bool': bool}[self.kind]
        if type(value) is not expected:
            raise ValueError('setting has incorrect type')
        if self.kind == 'int' and not self.minimum <= value <= self.maximum:
            raise ValueError('setting is outside supported bounds')
        if self.choices and value not in self.choices:
            raise ValueError('setting has unsupported value')
        return value


DEFINITIONS = MappingProxyType({
    'effect_limit': Definition('int', minimum=1, maximum=8),
    'queue_limit': Definition('int', minimum=1, maximum=256),
    'node_effect_limit': Definition('int', node_override=True, minimum=1, maximum=4),
    'log_level': Definition('text', node_override=True, choices=('debug', 'info', 'warning', 'error')),
    'telemetry_enabled': Definition('bool', node_override=True),
})


def validate_batch(batch, *, node=False):
    if type(batch) is not dict or not batch:
        raise ValueError('settings batch must be a nonempty mapping')
    result = {}
    for key, value in batch.items():
        definition = DEFINITIONS.get(key)
        if definition is None or (node and not definition.node_override):
            raise ValueError('unknown setting or invalid scope')
        result[key] = definition.validate(value)
    return result
