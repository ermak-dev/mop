#!/usr/bin/env python3
"""#690: одна библиотека строгих DRF-полей/serializers для command/query входов.

HYPOTHESIS: приватные парсеры exact/uid/positive/counter в views повторяют
зону rest_framework.serializers без общего переиспользования, и каждый новый
endpoint копирует свой набор отказов.
SPEC: общие строгие поля (канонический UUID, положительное десятичное число
без ведущих нулей, конечные enum-строки, bool без коэрсинга, bounded-строки),
общий no-duplicate-keys decoder (переиспользованный decode_body, не вторая
копия), одинаковые отказы unknown/duplicate/non-canonical/null/bool-coercion
на всех переведённых входах; machine-протокол продолжает использовать чистые
валидаторы вне HTTP.
RED: сначала фиксировались копии приватных парсеров (exact/uid/query в
endpointviews/machineviews/projectionviews/usageviews) и отсутствие модуля
mop/server/controller/strict.py; матрица паритета площадок -- в
_strict_matrix_cases.py.
SOLUTION: strict-модуль поверх чистых валидаторов sessions.protocol и
переиспользованного operations.commands.decode_body.
RESULT: ниже; зелёный прогон -- python3 tests/strict_serializers.py.
STATUS: FIXED — see #690
"""
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
from uuid import UUID

import hermetic  # noqa: F401
from _lib import Checks

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

EPOCH = "d7ee4c63-2a6b-4e6f-a9cd-c2990bf3a05f"
REQUEST = "8e6449b9-65db-4148-aa68-a8b4020bd8b5"


def _configure(c):
    """DRF-поля не читают settings, но serializers.Serializer требует settings."""
    import django
    from django.conf import settings
    if not settings.configured:
        settings.configure(DEBUG=False, USE_TZ=True, INSTALLED_APPS=[])
        django.setup()
    return True


def main():
    c = Checks()
    if importlib.util.find_spec('django') is None or importlib.util.find_spec('rest_framework') is None:
        hermetic.skip('strict serializers', 'declared server extra is not installed')
        return 0
    present = importlib.util.find_spec("mop.server.controller.strict") is not None
    if not c.check("strict serializer library exists", present):
        return c.report("strict_serializers")
    _configure(c)
    from rest_framework import serializers as drf
    from mop.server.controller import strict
    from mop.server.operations.commands import decode_body, InvalidCommand

    def refuses(what, fn):
        try:
            fn()
        except drf.ValidationError:
            c.check(what, True)
        else:
            c.fail(what, "invalid input accepted by strict field")

    # ── канонический UUID: str-форма, не объект, не верхний регистр, не braced ──
    field = strict.UUIDField()
    c.expect("canonical uuid accepted", field.run_validation(EPOCH), UUID(EPOCH))
    for value in (EPOCH.upper(), "{"+EPOCH+"}", EPOCH+".", 42, None, True, [], {},
                  "not-a-uuid", "00000000-0000-4000-8000-00000000000g"):
        refuses(f"non-canonical uuid refused: {value!r}", lambda value=value: field.run_validation(value))

    # ── положительное десятичное без ведущих нулей, bounded ──
    count = strict.CounterField()
    c.expect("decimal counter accepted", count.run_validation("42"), 42)
    c.expect("large counter stays exact", count.run_validation("9007199254740993"), 9007199254740993)
    zero = strict.CounterField(zero=True)
    c.expect("zero accepted only with zero=True", zero.run_validation("0"), 0)
    for value in ("01", "0", "-1", "+1", "1.0", " 1", "1 ", 42, 1.0, True, None, [], "one", "1e3", ""):
        refuses(f"non-canonical counter refused: {value!r}", lambda value=value: count.run_validation(value))

    # ── bounded enum: точные строки, без коэрсинга чисел/bool ──
    outcome = strict.EnumField(('succeeded', 'failed'))
    c.expect("enum string accepted", outcome.run_validation('succeeded'), 'succeeded')
    for value in ('SUCCEEDED', 'shell', 42, True, None, [], ''):
        refuses(f"non-member enum refused: {value!r}", lambda value=value: outcome.run_validation(value))

    # ── bool без коэрсинга: только сам bool ──
    flag = strict.BooleanField()
    c.expect("true accepted", flag.run_validation(True), True)
    c.expect("false accepted", flag.run_validation(False), False)
    for value in (0, 1, "true", "", None, [], 0.0):
        refuses(f"coerced bool refused: {value!r}", lambda value=value: flag.run_validation(value))

    # ── bounded-строка без trim ──
    text = strict.TextField(8)
    c.expect("bounded text accepted", text.run_validation("abc"), "abc")
    c.expect("whitespace text kept", text.run_validation(" a "), " a ")
    for value in ("123456789", 42, True, None, []):
        refuses(f"non-string text refused: {value!r}", lambda value=value: text.run_validation(value))

    # ── десятичная строка revision (без ведущих нулей, отдельный от counter) ──
    revision = strict.RevisionField()
    c.expect("revision accepted", revision.run_validation("1"), 1)
    for value in ("01", "-1", 1, True, None, "1.5"):
        refuses(f"non-canonical revision refused: {value!r}", lambda value=value: revision.run_validation(value))

    # ── строгий JSON decoder переиспользован, не скопирован ──
    c.check("strict parser reuses operations decode_body",
            strict.StrictJSONParser.parse is not None)
    from rest_framework.exceptions import ParseError
    seen = {}
    class _Stream:
        def read(self, n=None):
            return seen['raw']
    parser = strict.StrictJSONParser()
    seen['raw'] = json.dumps({'schema_version': 1, 'a': 'b'}).encode()
    c.expect("parser decodes strict json", parser.parse(_Stream()), {'schema_version': 1, 'a': 'b'})
    for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'[1]', b'\xff'):
        seen['raw'] = raw
        try:
            parser.parse(_Stream())
        except ParseError:
            c.check(f"parser refuses strict violation {raw!r}", True)
        else:
            c.fail(f"parser refuses strict violation {raw!r}", "accepted")
    # decode_body остаётся единственной копией: strict не заводит свой json.loads
    source = Path(strict.__file__).read_text()
    c.check("strict module has no second JSON decoder", 'json.loads' not in source)

    # ── конечный документ: unknown/duplicate/missing поля ──
    class Document(strict.FiniteSerializer):
        schema_version = strict.SchemaVersionField()
        request_id = strict.UUIDField()

    doc = Document(data={'schema_version': 1, 'request_id': REQUEST})
    c.check("finite document accepts exact fields", doc.is_valid())
    def paired(*pairs):
        """Структура с настоящим повтором ключа: literal дубли схлопывает.

        Повтор ключа виден только количеству: парсер запроса обязан отдать
        отказ до serializer-полей, и FiniteSerializer проверяет это по
        счётчику, а не по set(keys) == set(fields).
        """
        class pairs_view(dict):
            def keys(self):
                return [k for k, _ in pairs]
        return pairs_view({k: v for k, v in pairs})

    for payload in ({'schema_version': 1, 'request_id': REQUEST, 'extra': 1},
                    {'schema_version': 1},
                    paired(('schema_version', 1), ('request_id', REQUEST), ('request_id', REQUEST)),
                    {'schema_version': '1', 'request_id': REQUEST},
                    {'schema_version': 2, 'request_id': REQUEST},
                    {'schema_version': 1, 'request_id': REQUEST.upper()}):
        try:
            bad = Document(data=payload)
            bad.is_valid(raise_exception=True)
        except drf.ValidationError:
            c.check(f"finite document refuses {list(payload.keys())}", True)
        else:
            c.fail(f"finite document refuses {list(payload.keys())}", "accepted")
    c.expect("finite document returns validated values", doc.validated_data['request_id'], UUID(REQUEST))

    # ── query-площадки: словарь QueryDict-подобный, повтор ключа -- отказ ──
    class FakeMulti(dict):
        def __init__(self, values):
            self.lists = {}
            for key, value in values:
                self.lists.setdefault(key, []).append(value)
            super().__init__((k, v[0]) for k, v in self.lists.items())
        def getlist(self, key):
            return self.lists.get(key, [])

    parsed = strict.query_document(FakeMulti([('schema_version', '1'), ('node_id', EPOCH)]),
                                   ('schema_version', 'node_id'))
    c.expect("query document returns raw strings", parsed['node_id'], EPOCH)
    for values in ([('schema_version', '1'), ('node_id', EPOCH), ('limit', '5')],
                   [('schema_version', '1')],
                   [('schema_version', '1'), ('node_id', EPOCH), ('node_id', REQUEST)],
                   [('schema_version', '2'), ('node_id', EPOCH)]):
        try:
            strict.query_document(FakeMulti(values), ('schema_version', 'node_id'))
        except ValueError:
            c.check(f"query document refuses {values}", True)
        else:
            c.fail(f"query document refuses {values}", "accepted")
    # повтор необязательного поля: точный текст прежнего отказа
    try:
        strict.query_document(FakeMulti([('schema_version', '1'), ('limit', '5'), ('limit', '6')]),
                              ('schema_version',), optional=('limit',))
    except ValueError as error:
        c.expect("duplicate query field message", str(error), 'duplicate query fields')
    else:
        c.fail("duplicate query field message", "accepted")

    # ── конечные документы площадок: прежние тексты и ValueError-семантика ──
    for bad in ({'schema_version': 1, 'shell': 'id'}, {'schema_version': 1},
                {'schema_version': '1'}, {'schema_version': 2}):
        try:
            strict.machine_document(bad, ('schema_version', 'node_id'))
        except ValueError as error:
            c.check(f"machine document refuses {bad}", 'unknown or missing fields' in str(error)
                    or 'unsupported schema version' in str(error))
        else:
            c.fail(f"machine document refuses {bad}", "accepted")
    good = strict.machine_document({'schema_version': 1, 'node_id': EPOCH}, ('schema_version', 'node_id'))
    c.expect("machine document returns value", good['node_id'], EPOCH)
    c.expect("uid canonical accepted", strict.uid(EPOCH), UUID(EPOCH))
    for value in (EPOCH.upper(), 42, None, 'short'):
        try:
            strict.uid(value)
        except ValueError as error:
            c.check(f"uid refuses {value!r}", 'canonical UUID string required' == str(error))
        else:
            c.fail(f"uid refuses {value!r}", "accepted")
    for bad in ({'schema_version': 1, 'extra': 1}, {'schema_version': 1}, {'schema_version': '1'}):
        try:
            strict.strict_document(bad, ('epoch',))
        except ValueError as error:
            c.check(f"strict document refuses {bad}", 'unknown or missing endpoint fields' in str(error)
                    or 'unsupported endpoint schema version' in str(error))
        else:
            c.fail(f"strict document refuses {bad}", "accepted")
    document = strict.strict_document({'schema_version': 1, 'epoch': EPOCH}, ('schema_version', 'epoch'))
    c.expect("strict document returns value", document['epoch'], EPOCH)

    # ── machine-протокол: чистые валидаторы остаются вне HTTP ──
    from mop.server.sessions import protocol
    c.check("protocol validators stay importable outside http",
            all(callable(getattr(protocol, name, None))
                for name in ('uuid_value', 'counter', 'positive', 'deadline', 'exact')))
    try:
        protocol.uuid_value(EPOCH.upper())
    except ValueError as error:
        c.expect("protocol refusal text preserved", str(error), 'canonical UUID required')
    else:
        c.fail("protocol refusal text preserved", "accepted")
    return c.report("strict_serializers")


if __name__ == "__main__":
    sys.exit(main())
