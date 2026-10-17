"""Матрица негативных контрактов #690: одинаковые отказы на всех переведённых входах.

Каждая строка -- площадка (view + валидный базовый ввод). Колонки: валидный
ввод / unknown поле / повтор поля / не-каноническое значение / null /
bool-коэрсинг. Ожидания unknown/non-canonical/null/bool записаны ДО миграции
на приватных парсерах (exact/uid/query/protocol) и обязаны сохраниться после
перевода на mop/server/controller/strict.py: тот же статус и текст. Колонка
повтора меняется вместе с декодером в том же изменении: раньше DRF
JSONParser схлопывал сырой дубль до view (последнее значение выигрывало),
теперь StrictJSONParser отказывает сам повтор 400 'duplicate JSON input
fields are not allowed' -- то самое единообразие, ради которого #690.
"""
import json
from uuid import uuid4

from django.test import Client


def _post(client, path, body, **headers):
    return client.post(path, body, content_type='application/json', secure=True, **headers)


def _get(client, path, query, **headers):
    return client.get(path, query, secure=True, **headers)


class Matrix:
    """Ожидания одной площадки: статус + текст для каждой колонки матрицы."""

    # Стандартный порядок: валидный ввод первым. Площадки, чей валидный
    # ввод фиксирует состояние (result делает attempt терминальным), дают
    # свой порядок: отказные колонки обязаны увидеть ещё живой attempt.
    ORDER = ('valid', 'unknown', 'duplicate', 'noncanonical', 'null', 'boolean')

    def __init__(self, site, *, valid, unknown, duplicate, noncanonical, null, boolean,
                 order=None):
        self.site = site
        self.valid = valid
        self.unknown = unknown
        self.duplicate = duplicate
        self.noncanonical = noncanonical
        self.null = null
        self.boolean = boolean
        self.order = order or self.ORDER


def endpoint_matrix(test, installation_epoch):
    """POST /api/v1/endpoints: регистрация presence."""
    base = {'schema_version': 1, 'client_id': str(test.client_reg.pk),
            'project_id': str(test.project.pk), 'external_session_id': 'matrix-1',
            'display_name': 'matrix'}
    return Matrix(
        'endpoint_register',
        valid=dict(body=base, expect=(201, None)),
        unknown=dict(body={**base, 'owner': 1}, expect=(400, 'unknown or missing endpoint fields')),
        # Повтор ключа раньше схлопывался DRF JSONParser (второе значение
        # выигрывало); #690 переводит площадку на StrictJSONParser, и сырой
        # дубль отказывает 400 на самом декодере.
        duplicate=dict(raw=json_bytes([('schema_version', 1), ('schema_version', 2),
            ('client_id', str(test.client_reg.pk)), ('project_id', str(test.project.pk)),
            ('external_session_id', 'matrix-1'), ('display_name', 'matrix')]),
            expect=(400, 'duplicate JSON input fields are not allowed')),
        noncanonical=dict(body={**base, 'project_id': str(test.project.pk).upper()},
                          expect=(400, 'canonical UUID string required')),
        null=dict(body={**base, 'display_name': None}, expect=(400, None)),
        boolean=dict(body={**base, 'schema_version': True}, expect=(400, 'unsupported endpoint schema version')),
    )


def reports_matrix(test, installation_epoch):
    """POST /api/v1/reports: приватная доставка."""
    from django.utils import timezone as _tz
    from datetime import timedelta as _td
    deadline = (_tz.now()+_td(minutes=30)).isoformat()
    base = {'schema_version': 1, 'epoch': str(installation_epoch),
            'address': test.endpoint.address, 'message_id': str(uuid4()),
            'text': 'matrix', 'not_after': deadline}
    return Matrix(
        'endpoint_reports',
        valid=dict(body=base, expect=(202, None)),
        unknown=dict(body={**base, 'shell': 'id'}, expect=(400, 'unknown or missing endpoint fields')),
        # Дубль одинаковых значений раньше схлопывался в валидный запрос
        # (202); #690 отказывает сам повтор ключа 400 на строгом декодере.
        duplicate=dict(raw=json_bytes([('schema_version', 1), ('schema_version', 1),
            ('epoch', str(installation_epoch)), ('address', test.endpoint.address),
            ('message_id', str(uuid4())), ('text', 'matrix'), ('not_after', deadline)]),
            expect=(400, 'duplicate JSON input fields are not allowed')),
        noncanonical=dict(body={**base, 'message_id': str(uuid4()).upper()},
                          expect=(400, 'canonical UUID string required')),
        null=dict(body={**base, 'text': None}, expect=(400, None)),
        boolean=dict(body={**base, 'notify_when_idle': 'true'},
                     expect=(400, 'invalid delivery options')),
    )


def addresses_matrix(test):
    """GET /api/v1/addresses: query-площадка presence."""
    valid_query = {'schema_version': '1', 'project_id': str(test.project.pk)}
    return Matrix(
        'endpoint_addresses',
        valid=dict(query=valid_query, expect=(200, None)),
        unknown=dict(query={**valid_query, 'extra': '1'},
                     expect=(400, 'unknown or duplicate presence query fields')),
        duplicate=dict(query=None, raw_query='schema_version=1&schema_version=1&project_id='+str(test.project.pk),
                       expect=(400, 'unknown or duplicate presence query fields')),
        noncanonical=dict(query={**valid_query, 'project_id': str(test.project.pk).upper()},
                          expect=(400, 'canonical UUID string required')),
        null=dict(query={**valid_query, 'project_id': ''},
                  expect=(400, 'canonical UUID string required')),
        boolean=dict(query={**valid_query, 'limit': 'true'}, expect=(400, 'invalid presence limit')),
    )


def claim_matrix(test, work_wire, request_id):
    """POST /api/v1/machine/work/claim.

    Claim идемпотентен по request_id: valid-колонка переигрывает уже
    принятый setup-claim и получает тот же snapshot (200), не второе
    владение ресурсом.
    """
    base = {'schema_version': 1, 'request_id': str(request_id), 'work': work_wire}
    return Matrix(
        'machine_claim',
        valid=dict(body=base, expect=(200, None)),
        unknown=dict(body={**base, 'shell': 'id'}, expect=(400, 'unknown or missing fields')),
        # Повтор ключа раньше схлопывался парсером до view и запрос приходил
        # валидным; #690 переводит площадку на StrictJSONParser, и сырой дубль
        # отказывает 400 на самом декодере.
        duplicate=dict(raw=json_bytes([('schema_version', 1), ('request_id', str(request_id)),
            ('work', work_wire), ('schema_version', 1)]),
            expect=(400, 'duplicate JSON input fields are not allowed')),
        noncanonical=dict(body={**base, 'request_id': str(uuid4()).upper()},
                          expect=(400, 'canonical UUID required')),
        null=dict(body={**base, 'request_id': None}, expect=(400, 'canonical UUID required')),
        boolean=dict(body={**base, 'work': {**work_wire, 'generation': True}},
                     expect=(400, 'decimal counter string required')),
    )


def result_matrix(test, attempt_id, revision):
    """POST /api/v1/machine/work/result."""
    base = {'schema_version': 1, 'attempt_id': str(attempt_id), 'expected_revision': str(revision),
            'receipt_id': str(uuid4()), 'outcome': 'succeeded', 'evidence': 'session-replaced'}
    # Валидный ввод фиксирует результат и делает attempt терминальным:
    # отказные колонки идут до него, иначе ловят 409 вместо 400.
    order = ('unknown', 'duplicate', 'noncanonical', 'null', 'boolean', 'valid')
    return Matrix(
        'machine_result',
        valid=dict(body=base, expect=(200, None)),
        unknown=dict(body={**base, 'holder': 'root'}, expect=(400, 'unknown or missing fields')),
        duplicate=dict(raw=json_bytes([('schema_version', 1), ('attempt_id', str(attempt_id)),
            ('expected_revision', str(revision)), ('receipt_id', str(uuid4())),
            ('receipt_id', str(uuid4())), ('outcome', 'succeeded'), ('evidence', 'session-replaced')]),
            expect=(400, 'duplicate JSON input fields are not allowed')),
        noncanonical=dict(body={**base, 'attempt_id': str(attempt_id).upper()},
                          expect=(400, 'canonical UUID required')),
        null=dict(body={**base, 'expected_revision': None}, expect=(400, 'decimal counter string required')),
        boolean=dict(body={**base, 'outcome': True},
                     expect=(400, 'named outcome and public evidence code required')),
        order=order,
    )


def work_matrix(test, node_id, boot):
    """GET /api/v1/machine/work: query-площадка machine."""
    valid_query = {'schema_version': '1', 'node_id': str(node_id), 'executor_epoch': str(boot)}
    # Матрица бежит после зафиксированного result: работа уже не возвращается
    # poll'у, и валидный query отвечает 200 с пустым списком (принят, не
    # отказан) -- ранний 503 был артефактом порядка записи, не контракта.
    return Matrix(
        'machine_work',
        valid=dict(query=valid_query, expect=(200, None)),
        unknown=dict(query={**valid_query, 'shell': 'id'}, expect=(400, 'unknown or missing query fields')),
        duplicate=dict(query=None, raw_query='schema_version=1&node_id='+str(node_id)+'&executor_epoch='+str(boot)
                       + '&schema_version=1', expect=(400, 'duplicate query fields')),
        noncanonical=dict(query={**valid_query, 'node_id': str(node_id).upper()},
                          expect=(400, 'canonical UUID required')),
        null=dict(query={**valid_query, 'node_id': ''}, expect=(400, 'canonical UUID required')),
        boolean=dict(query={**valid_query, 'limit': 'true'}, expect=(400, 'decimal counter string required')),
    )


def puppets_matrix(test):
    """GET /api/v1/puppets: query-площадка read model."""
    valid_query = {}
    return Matrix(
        'projection_puppets',
        valid=dict(query=valid_query, expect=(200, None)),
        unknown=dict(query={'extra': '1'}, expect=(400, 'unknown or repeated query fields')),
        duplicate=dict(query=None, raw_query='page_size=1&page_size=2', expect=(400, 'unknown or repeated query fields')),
        noncanonical=dict(query={'project_id': str(test.project.pk).upper()},
                          expect=(400, 'canonical UUID required')),
        null=dict(query={'project_id': ''}, expect=(400, 'canonical UUID required')),
        boolean=dict(query={'page_size': 'true'}, expect=(400, 'page_size must be within 1..500')),
    )


def usage_matrix():
    """GET /api/v1/usage: query-площадка usage."""
    valid_query = {'schema_version': '1', 'start': '2026-10-16', 'end': '2026-10-17'}
    return Matrix(
        'usage',
        valid=dict(query=valid_query, expect=(200, None)),
        unknown=dict(query={**valid_query, 'extra': '1'}, expect=(400, 'unknown or missing query fields')),
        duplicate=dict(query=None, raw_query='schema_version=1&start=2026-10-16&start=2026-10-15&end=2026-10-17',
                       expect=(400, 'duplicate query fields')),
        noncanonical=dict(query={**valid_query, 'start': '2026-10-16T00:00:00'},
                          expect=(400, 'usage period requires ISO dates')),
        null=dict(query={**valid_query, 'limit': ''}, expect=(400, 'invalid usage page size')),
        boolean=dict(query={**valid_query, 'limit': 'true'}, expect=(400, 'invalid usage page size')),
    )


def json_bytes(pairs):
    """Сырое тело с настоящим повтором ключа: dict в Python схлопывает дубли."""
    return ('{' + ', '.join(json.dumps(k)+': '+json.dumps(v) for k, v in pairs) + '}').encode()


def check_matrix(test, matrix, send):
    """Прогон одной матрицы через send(kind, **kwargs) -> response.

    send возвращает уже выполненный HTTP-ответ; kind -- имя колонки.
    """
    import json as _json
    columns = {'valid': matrix.valid, 'unknown': matrix.unknown,
               'duplicate': matrix.duplicate, 'noncanonical': matrix.noncanonical,
               'null': matrix.null, 'boolean': matrix.boolean}
    for column in matrix.order:
        spec = columns[column]
        status, message = spec['expect']
        response = send(column, spec)
        test.assertEqual(response.status_code, status,
                         f'{matrix.site}/{column}: {response.status_code} != {status}: {response.content[:400]}')
        if message is not None:
            body = _json.loads(response.content) if response.content else {}
            got = body.get('error', {}).get('message', '') if isinstance(body, dict) else ''
            test.assertTrue(got.startswith(message) or message in got,
                            f'{matrix.site}/{column}: message {got!r} does not carry {message!r}')


def post_spec(client, path, **headers):
    def send(column, spec):
        if spec.get('raw') is not None:
            return client.post(path, spec['raw'], content_type='application/json', secure=True, **headers)
        return _post(client, path, spec['body'], **headers)
    return send


def get_spec(client, path, **headers):
    def send(column, spec):
        if spec.get('raw_query') is not None:
            return client.get(path+'?'+spec['raw_query'], secure=True, **headers)
        return _get(client, path, spec['query'], **headers)
    return send
