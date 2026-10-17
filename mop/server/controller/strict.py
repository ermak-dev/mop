"""#690: одна библиотека строгих DRF-полей/serializers/parsers для входов.

Каждое поле отказывает тем же текстом, что и прежний приватный парсер
площадки: канонические UUID-строки, положительные десятичные числа без
ведущих нулей, конечные enum-строки, bool без коэрсинга, bounded-строки,
unknown/duplicate поля -- одинаково на всех входах. Чистые валидаторы
остаются в mop.server.sessions.protocol (machine-протокол использует их
вне HTTP); DRF-обёртки здесь только переводят их отказ в
serializers.ValidationError и не заводят вторую семантику.

Строгий телодекодер не копируется: StrictJSONParser переиспользует
operations.commands.decode_body (no-duplicate-keys, bounded, nonfinite
refusal) как единственный декодер команды.
"""
from uuid import UUID

from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser

from mop.server.sessions import protocol


def _validator(function, message):
    """Обёртка чистого валидатора: тот же текст отказа, тип -- DRF."""

    def validate(value):
        try:
            return function(value)
        except ValueError:
            raise serializers.ValidationError(message) from None

    return validate


class UUIDField(serializers.Field):
    """Каноническая строка UUID: без объектов, верхнего регистра и скобок."""

    def to_internal_value(self, data):
        try:
            return protocol.uuid_value(data)
        except ValueError:
            raise serializers.ValidationError('canonical UUID required') from None


class CounterField(serializers.Field):
    """Положительное десятичное число без ведущих нулей, точное на больших.

    zero=True допускает '0' (нулевая ревизия producer binding), но не '00'.
    """

    default_error_messages = {'invalid': 'decimal counter string required'}

    def __init__(self, *, zero=False, maximum=None, **kwargs):
        self.zero = zero
        self.maximum = maximum
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if type(data) is not str or not data.isascii() or not data.isdecimal():
            self.fail('invalid')
        if len(data) > 1 and data.startswith('0'):
            self.fail('invalid')
        if not self.zero and data == '0':
            self.fail('invalid')
        if self.maximum is not None and int(data) > self.maximum:
            self.fail('invalid')
        return int(data)


class RevisionField(serializers.Field):
    """Десятичная строка ревизии: '0' или без ведущих нулей, bounded."""

    def to_internal_value(self, data):
        if (type(data) is not str or not data.isascii() or not data.isdecimal()
                or (len(data) > 1 and data.startswith('0')) or len(data) > 128):
            raise serializers.ValidationError('invalid revision string') from None
        return int(data)


class EnumField(serializers.Field):
    """Конечный набор точных строк; числа/bool/None не коэрсятся."""

    def __init__(self, names, **kwargs):
        self.names = frozenset(names)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if type(data) is not str or data not in self.names:
            raise serializers.ValidationError('unknown command value') from None
        return data


class BooleanField(serializers.Field):
    """Только сам bool: 'true'/1/null не превращаются в истину."""

    def to_internal_value(self, data):
        if type(data) is not bool:
            raise serializers.ValidationError('boolean required') from None
        return data


class TextField(serializers.Field):
    """Bounded-строка без trim: пробелы значимы, пустая -- валидна."""

    def __init__(self, maximum, **kwargs):
        self.maximum = maximum
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if type(data) is not str or len(data.encode('utf-8')) > self.maximum:
            raise serializers.ValidationError('bounded string required') from None
        return data


class SchemaVersionField(serializers.Field):
    """schema_version ровно 1: int, не строка и не True."""

    def to_internal_value(self, data):
        if type(data) is not int or data != 1:
            raise serializers.ValidationError('unsupported schema version') from None
        return data


class FiniteSerializer(serializers.Serializer):
    """Конечный документ: unknown и повторные поля отказывают до полей.

    Повтор ключа ловится здесь по количеству: parse-слой (StrictJSONParser)
    уже отказал бы сырой дубль, но конечный документ не полагается на то,
    что между парсером и serializer никто не построил dict заново.
    """

    def to_internal_value(self, data):
        names = set(self.fields)
        if not hasattr(data, 'keys'):
            raise serializers.ValidationError({'non_field_errors': ['unknown fields']})
        keys = list(data.keys())
        if set(keys) - names or len(keys) != len(set(keys)):
            raise serializers.ValidationError({'non_field_errors': ['unknown fields']})
        return super().to_internal_value(data)


class StrictJSONParser(BaseParser):
    """Единственный строгий декодер: переиспользует operations decode_body."""

    media_type = 'application/json'

    def parse(self, stream, media_type=None, parser_context=None):
        from mop.server.operations.commands import decode_body, InvalidCommand, MAX_BODY_BYTES
        try:
            return decode_body(stream.read(MAX_BODY_BYTES + 1))
        except InvalidCommand as error:
            raise ParseError(str(error)) from None


def query_document(values, names, *, optional=()):
    """Строгий разбор query-площадки: строки значений возвращаются как есть.

    Отказ -- ValueError с текстами прежнего парсера machineviews.query
    (unknown/missing, duplicate, unsupported schema): глобальный handler
    обеих границ (текущая и #691 value_error_is_input) переводит его в
    400 validation_error с той же причиной, что и до #690.
    """
    if set(values) - set(names) - set(optional) or not set(names) <= set(values):
        raise ValueError('unknown or missing query fields')
    if any(len(values.getlist(name)) != 1 for name in values):
        raise ValueError('duplicate query fields')
    result = dict(values.items())
    if result.get('schema_version') != '1':
        raise ValueError('unsupported schema version')
    return result


def uuid_field(value):
    """Функциональная форма для query-строк вне serializer-документа.

    Отказ -- ValueError 'canonical UUID required' как у protocol.uuid_value:
    глобальный handler обеих границ сохраняет прежний текст и статус.
    """
    return protocol.uuid_value(value)


def uid(value):
    """Каноническая UUID-строка presence-семейства: str(UUID(v)) == v."""
    if type(value) is not str:
        raise ValueError('canonical UUID string required')
    try:
        result = UUID(value)
    except ValueError:
        raise ValueError('canonical UUID string required') from None
    if str(result) != value:
        raise ValueError('canonical UUID string required')
    return result


def strict_document(value, required, *, optional=()):
    """Конечный документ presence-семейства: unknown/missing/schema_version.

    Тексты прежнего endpointviews.exact: 'unknown or missing endpoint fields'
    и 'unsupported endpoint schema version'.
    """
    if type(value) is not dict or not set(required) <= value.keys() or value.keys()-set(required)-set(optional):
        raise ValueError('unknown or missing endpoint fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported endpoint schema version')
    return value


def machine_document(value, names):
    """Конечный machine-документ: тексты прежнего sessions.protocol.exact.

    ValueError 'unknown or missing fields' / 'unsupported schema version'
    переводится глобальным handler в тот же 400, что и до #690.
    """
    from mop.server.sessions import protocol
    protocol.exact(value, names)
    return value
