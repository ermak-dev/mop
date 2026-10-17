"""Проверки #691: один глобальный handler и типизированный реестр отказов.

FEATURE: controller/views.py уже глобальный, но endpointviews/machineviews/
usageviews/projectionviews держат приватные exception->(code,status,retryable)
мапперы; код Refused на границе доверяется без проверки регистрации.
HYPOTHESIS: реестр (тип -> code/status/retryable) в одном месте и
делегирование из views.exception_handler дают byte-identical конверты без
приватных мапперов; незарегистрированный или спуфнутый код fail-closed.
SPEC: envelope {schema_version, error{code,message,retryable}} неизменен;
CSRF/no-store/Retry-After сохранены; сырые причины DB/провайдера не утекают;
unknown-COMMIT не retryable; незарегистрированный код -- generic 500,
не придуманный 400. Координация с #690: стандартные DRF ValidationErrors
идут framework-путём того же handler без смягчения.
RED (база 59f70c0f): реестра нет (ImportError), глобальный handler не знает
семейств delivery/execution/usage/read, спуфнутый код уезжает клиенту эхом,
read-маппер глотает доменные отказы в Response, а один и тот же Refused или
execution.Conflict получает 400 на EndpointView и 409 на MachineView.
DispatchParity -- оракул byte-identity: снят с приватных мапперов базы и
обязан быть зелёным и до, и после переноса.
RED (первый перенос): read-конверт собирал extra как {reset, epoch} --
словари равны, байты нет; проверка байтов ловит порядок ключей.
SOLUTION: errors.py -- таблицы Entry (REFUSED из домена операций, TYPED,
ACCESS, READ_OVERRIDES, DATABASE_*), views.exception_handler строит конверт
из записи; view объявляет value_error_is_input, read-семейство несёт эпоху в
ReadRefusal.
STATUS: FIXED -- see #691
"""
import unittest
from unittest.mock import patch


EPOCH = '11111111-1111-1111-1111-111111111111'
ALLOWED_STATUSES = {400, 403, 404, 409, 410, 429, 503}


def _handler():
    from mop.server.controller.views import exception_handler
    return exception_handler


def _body(response):
    return response.data


def _bytes(value):
    from rest_framework.renderers import JSONRenderer
    return JSONRenderer().render(value)


def _request():
    from rest_framework.test import APIRequestFactory
    return APIRequestFactory().get('/probe')


def _probe(base, error):
    """Настоящий DRF dispatch через базовый view переведённого входа."""
    class Probe(base):
        authentication_classes = ()
        permission_classes = ()

        def get(self, request):
            raise error
    return Probe.as_view()


def _read_probe(error):
    """projectionviews.response через DRF dispatch с уже прочитанной эпохой."""
    from rest_framework.views import APIView
    from mop.server.controller import projectionviews

    def read():
        raise error

    class Probe(APIView):
        authentication_classes = ()
        permission_classes = ()

        def get(self, request):
            return projectionviews.response(read)

    view = Probe.as_view()

    def call(request):
        with patch.object(projectionviews, 'read_authority', return_value={'epoch': EPOCH}):
            return view(request)
    return call


def _dispatch(view):
    response = view(_request())
    response.render()
    return response


def _bases():
    from mop.server.controller.endpointviews import EndpointView
    from mop.server.controller.machineviews import MachineView
    from mop.server.controller.usageviews import Usage
    return (EndpointView, MachineView, Usage)


def _registry():
    from mop.server.controller import errors
    return errors


def _all_entries():
    errors = _registry()
    yield from errors.REFUSED.values()
    for _, entry in errors.TYPED + errors.ACCESS + errors.READ_OVERRIDES:
        yield entry
    yield errors.INPUT
    yield errors.DATABASE_UNAVAILABLE
    yield errors.DATABASE_OUTCOME_UNKNOWN


class RegistryContract(unittest.TestCase):
    """Таблица кодов полна, согласована с доменом и fail-closed на спуфинг."""

    def test_refused_registry_covers_every_admitted_code_and_status(self):
        from mop.server.operations.service import Refused, _MESSAGES
        table = {code: entry.status for code, entry in _registry().REFUSED.items()}
        self.assertEqual(set(table), set(_MESSAGES),
                         'таблица границы покрывает каждый код домена операций')
        expected = {'result_expired': 410, 'admission_limit': 429,
                    'effects_disabled': 503, 'controller_unavailable': 503}
        expected.update({code: 409 for code in set(_MESSAGES) - set(expected)})
        self.assertEqual(table, expected)
        for code, status in sorted(table.items()):
            with self.subTest(code=code):
                entry = _registry().REFUSED[code]
                self.assertEqual(Refused(code).status, status,
                                 'реестр и домен обязаны соглашаться в статусе')
                self.assertEqual(entry.code, code)
                self.assertEqual(entry.retryable, status in (429, 503))
                self.assertEqual(entry.retry_after, '1' if status == 429 else None)

    def test_every_registered_entry_is_well_formed(self):
        errors = _registry()
        for entry in _all_entries():
            with self.subTest(code=entry.code, status=entry.status):
                self.assertIn(entry.status, ALLOWED_STATUSES, 'статус реестра конечен')
                self.assertRegex(entry.code, r'^[a-z][a-z_]*[a-z]$')
                if entry is errors.DATABASE_OUTCOME_UNKNOWN:
                    self.assertFalse(entry.retryable, 'unknown-COMMIT не retryable')
                else:
                    self.assertEqual(entry.retryable, entry.status in (429, 503))
                if entry.retry_after is not None:
                    self.assertEqual(entry.status, 429, 'Retry-After только у 429')
        self.assertEqual(errors.DATABASE_OUTCOME_UNKNOWN.status, 503)

    def test_registered_types_do_not_overlap_so_order_cannot_change_a_verdict(self):
        from django.db import DatabaseError
        from mop.server.access.services import Denied
        from mop.server.operations.service import Refused
        kinds = [kind for kind, _ in _registry().TYPED]
        self.assertEqual(len(kinds), len(set(kinds)))
        for kind in kinds:
            with self.subTest(kind=kind.__qualname__):
                others = [other for other in kinds if other is not kind]
                self.assertFalse(any(issubclass(kind, other) for other in others),
                                 'подкласс чужой записи сделал бы вердикт зависимым от порядка')
                self.assertFalse(issubclass(kind, (Refused, Denied, DatabaseError)))

    def test_every_registered_type_round_trips_through_the_handler(self):
        errors = _registry()
        for kind, entry in errors.TYPED + errors.ACCESS:
            with self.subTest(kind=kind.__qualname__):
                error = kind('probe reason')
                response = _handler()(error, {'view': None})
                self.assertIsNotNone(response, 'зарегистрированный тип обязан иметь конверт')
                self.assertEqual(response.status_code, entry.status)
                expected = {'schema_version': 1, 'error': {
                    'code': entry.code,
                    'message': entry.message if entry.message is not None else 'probe reason',
                    'retryable': entry.retryable}}
                if entry.reset:
                    expected['reset'] = {'required': True}
                self.assertEqual(_bytes(_body(response)), _bytes(expected))
                self.assertEqual(response.get('Retry-After'), entry.retry_after)
                self.assertEqual(response['Cache-Control'], 'no-store')

    def test_unregistered_or_spoofed_refused_code_is_fail_closed_generic_500(self):
        from mop.server.operations.service import Refused
        from mop.server.controller.errors import ReadRefusal
        from mop.server.controller.machineviews import MachineView
        error = Refused('epoch_conflict', safe_reason='private attacker text')
        error.code = 'spoofed_code'
        self.assertIsNone(_handler()(error, {}),
                          'спуфнутый код не эхом и не придуманным 400, а re-raise -> 500')
        self.assertIsNone(_handler()(error, {'view': MachineView}),
                          'делегирование machine-семейства не смягчает fail-closed')
        self.assertIsNone(_handler()(ReadRefusal(error, EPOCH), {}),
                          'read-семейство не смягчает спуфнутый код до 400')
        invented = Refused('invented_code', safe_reason='private attacker text')
        self.assertIsNone(_handler()(invented, {'view': MachineView}),
                          'код, которого нет в реестре, не получает статус из error.status')

    def test_unregistered_raw_error_outside_an_input_view_is_not_softened(self):
        for error in (ValueError('raw parser detail'), RuntimeError('raw provider detail'),
                      KeyError('raw key')):
            with self.subTest(kind=type(error).__name__):
                self.assertIsNone(_handler()(error, {'view': None}))

    def test_registered_refused_codes_keep_their_envelope_and_retry_after(self):
        from mop.server.operations.service import Refused
        for code, status, retry_after in (('admission_limit', 429, '1'),
                                          ('result_expired', 410, None),
                                          ('epoch_conflict', 409, None),
                                          ('controller_unavailable', 503, None)):
            with self.subTest(code=code):
                response = _handler()(Refused(code), {})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.data['error']['code'], code)
                self.assertEqual(response.data['error']['retryable'], status in (429, 503))
                self.assertEqual(response['Retry-After'] if retry_after else None, retry_after)
                self.assertEqual(response['Cache-Control'], 'no-store')


class PrivateMappersRemoved(unittest.TestCase):
    """Четыре приватных маппера удалены; перевод живёт в реестре одного handler."""

    def test_no_view_class_keeps_a_private_exception_mapper(self):
        for view in _bases():
            with self.subTest(view=view.__name__):
                self.assertNotIn('handle_exception', view.__dict__,
                                 'частный маппер подлежит глобальному реестру')

    def test_projection_response_wraps_refusals_with_the_epoch_it_already_read(self):
        from mop.server.controller import projectionviews
        from mop.server.controller.errors import ReadRefusal
        from mop.server.sessions.projections import SnapshotReset

        def read():
            raise SnapshotReset('authority changed while materializing snapshot')

        with patch.object(projectionviews, 'read_authority', return_value={'epoch': EPOCH}):
            with self.assertRaises(ReadRefusal) as caught:
                projectionviews.response(read)
        self.assertIsInstance(caught.exception.error, SnapshotReset)
        self.assertEqual(caught.exception.epoch, EPOCH)

    def test_projection_response_without_authority_wraps_without_invented_epoch(self):
        from mop.server.controller import projectionviews
        from mop.server.controller.errors import ReadAuthorityUnavailable, ReadRefusal

        def read():
            raise AssertionError('authority must be read before the query')

        failure = ReadAuthorityUnavailable('controller restore guard is not ready; maintenance recovery required')
        with patch.object(projectionviews, 'read_authority', side_effect=failure):
            with self.assertRaises(ReadRefusal) as caught:
                projectionviews.response(read)
        self.assertIsNone(caught.exception.epoch)

    def test_projection_response_success_path_is_unchanged(self):
        from mop.server.controller import projectionviews
        with patch.object(projectionviews, 'read_authority', return_value={'epoch': EPOCH}):
            value = projectionviews.response(lambda: {'schema_version': 1, 'ok': True})
        self.assertEqual(value.status_code, 200)
        self.assertEqual(value.data, {'schema_version': 1, 'ok': True})
        self.assertEqual(value['Cache-Control'], 'no-store')


class DeliveryFamilyParity(unittest.TestCase):
    """endpointviews: конверты byte-identical прежнему приватному мапперу."""

    def _call(self, error):
        from mop.server.controller.endpointviews import EndpointView
        return _handler()(error, {'view': EndpointView})

    def test_registered_delivery_refusals_match_the_old_mapper(self):
        from mop.server.sessions import endpoints as s
        cases = (
            (s.Conflict('renew target differs from current endpoint'), 409, 'delivery_conflict', False, None),
            (s.Capacity('shared durable admission is exhausted'), 429, 'delivery_capacity', True, '5'),
            (s.Unavailable('endpoint authority is not bound'), 503, 'delivery_unavailable', True, None),
            (ValueError('unknown or missing endpoint fields'), 400, 'validation_error', False, None),
        )
        for error, status, code, retryable, retry_after in cases:
            with self.subTest(code=code):
                response = self._call(error)
                self.assertIsNotNone(response)
                self.assertEqual(response.status_code, status)
                self.assertEqual(_body(response), {'schema_version': 1, 'error': {
                    'code': code, 'message': str(error), 'retryable': retryable}})
                self.assertEqual(response['Cache-Control'], 'no-store')
                self.assertEqual(response['Retry-After'] if retry_after else None, retry_after)


class ExecutionFamilyParity(unittest.TestCase):
    """machineviews и его наследники (telemetry): прежние конверты."""

    def _call(self, error, view=None):
        from mop.server.controller.machineviews import MachineView
        return _handler()(error, {'view': view or MachineView})

    def test_registered_execution_refusals_match_the_old_mapper(self):
        from mop.server.sessions.services import Conflict, Unavailable
        cases = (
            (Conflict('target changed; refresh before submitting'), 409, 'execution_conflict', False),
            (Unavailable('operation work port is not bound'), 503, 'authority_unavailable', True),
            (ValueError('unknown or missing query fields'), 400, 'validation_error', False),
        )
        for error, status, code, retryable in cases:
            with self.subTest(code=code):
                response = self._call(error)
                self.assertIsNotNone(response)
                self.assertEqual(response.status_code, status)
                self.assertEqual(_body(response), {'schema_version': 1, 'error': {
                    'code': code, 'message': str(error), 'retryable': retryable}})
                self.assertEqual(response['Cache-Control'], 'no-store')

    def test_usage_unavailable_keeps_its_own_envelope(self):
        from mop.server.sessions.usage_services import Unavailable
        from mop.server.controller.usageviews import Usage
        error = Unavailable('usage admission requires no surrounding transaction')
        response = self._call(error, view=Usage)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(_body(response), {'schema_version': 1, 'error': {
            'code': 'usage_unavailable', 'message': str(error), 'retryable': True}})
        self.assertEqual(response['Cache-Control'], 'no-store')


class ReadFamilyParity(unittest.TestCase):
    """projectionviews: прежние конверты, включая epoch и reset верхнего уровня."""

    def _call(self, error, *, epoch=EPOCH):
        from mop.server.controller.projectionviews import PoolSnapshot
        return _handler()(error, {'view': PoolSnapshot})

    def _wrapped(self, error, epoch):
        from mop.server.controller.errors import ReadRefusal
        return ReadRefusal(error, epoch)

    def _cases(self):
        from mop.server.sessions.projections import SnapshotReset, SnapshotCapacity
        from mop.server.sessions.projection_writes import SourceConflict
        from mop.server.access.services import Denied, NotVisible
        return (
            (SnapshotReset('authority changed while materializing snapshot'), 410, 'reset_required', False,
             {'epoch': EPOCH, 'reset': {'required': True}}),
            (SnapshotCapacity('snapshot target bound exceeded; select a project'), 503, 'unavailable', True,
             {'epoch': EPOCH}),
            (SourceConflict('installation read authority is not bound'), 409, 'incarnation_conflict', False,
             {'epoch': EPOCH}),
            (NotVisible('resource not found'), 404, 'not_found', False, {'epoch': EPOCH}),
            (Denied('identity is inactive'), 403, 'permission_denied', False, {'epoch': EPOCH}),
            (ValueError('page_size must be within 1..500'), 400, 'validation_error', False, {'epoch': EPOCH}),
        )

    def test_registered_read_refusals_match_the_old_mapper(self):
        for error, status, code, retryable, extra in self._cases():
            with self.subTest(code=code):
                response = self._call(self._wrapped(error, EPOCH))
                self.assertIsNotNone(response)
                self.assertEqual(response.status_code, status)
                self.assertEqual(_body(response), {'schema_version': 1, 'error': {
                    'code': code, 'message': str(error), 'retryable': retryable}, **extra})
                self.assertEqual(response['Cache-Control'], 'no-store')

    def test_read_envelope_keeps_the_old_key_order_byte_for_byte(self):
        # Прежний маппер дописывал epoch, затем reset: словари равны при любом
        # порядке, а байты ответа -- нет.
        for error, status, code, retryable, extra in self._cases():
            with self.subTest(code=code):
                response = self._call(self._wrapped(error, EPOCH))
                self.assertEqual(_bytes(response.data), _bytes({'schema_version': 1, 'error': {
                    'code': code, 'message': str(error), 'retryable': retryable}, **extra}))

    def test_read_authority_refusal_omits_epoch_it_never_learned(self):
        from mop.server.controller.errors import ReadAuthorityUnavailable
        response = self._call(self._wrapped(
            ReadAuthorityUnavailable('controller restore guard is not ready; maintenance recovery required'), None))
        self.assertEqual(response.status_code, 503)
        body = _body(response)
        self.assertEqual(body['error']['code'], 'authority_unavailable')
        self.assertTrue(body['error']['retryable'])
        self.assertNotIn('epoch', body, 'неизвестная эпоха не выдумывается')
        self.assertIn('restore guard', body['error']['message'])


class DispatchParity(unittest.TestCase):
    """Настоящий DRF dispatch каждого переведённого входа: байты и заголовки
    прежних приватных мапперов. Зелёный на базе -- это оракул, а не дефект."""

    def assertEnvelope(self, response, status, expected, *, retry_after=None):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.content, _bytes(expected))
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response.get('Retry-After'), retry_after)

    def test_endpoint_view_bytes_and_headers(self):
        from mop.server.controller.endpointviews import EndpointView
        from mop.server.sessions import endpoints as s
        for error, status, code, retryable, retry_after in (
                (s.Conflict('renew target differs from current endpoint'), 409, 'delivery_conflict', False, None),
                (s.Capacity('shared durable admission is exhausted'), 429, 'delivery_capacity', True, '5'),
                (s.Unavailable('endpoint authority is not bound'), 503, 'delivery_unavailable', True, None),
                (ValueError('unknown or missing endpoint fields'), 400, 'validation_error', False, None)):
            with self.subTest(code=code):
                self.assertEnvelope(_dispatch(_probe(EndpointView, error)), status, {
                    'schema_version': 1, 'error': {'code': code, 'message': str(error), 'retryable': retryable}},
                    retry_after=retry_after)

    def test_machine_view_bytes_and_headers(self):
        from mop.server.controller.machineviews import MachineView
        from mop.server.operations.service import Refused
        from mop.server.sessions.services import Conflict, Unavailable
        for error, status, code, retryable, retry_after in (
                (Conflict('target changed; refresh before submitting'), 409, 'execution_conflict', False, None),
                (Unavailable('operation work port is not bound'), 503, 'authority_unavailable', True, None),
                (ValueError('unknown or missing query fields'), 400, 'validation_error', False, None),
                (Refused('admission_limit'), 429, 'admission_limit', True, '1'),
                (Refused('controller_unavailable', safe_reason='current owner Git identity is unavailable'),
                 503, 'controller_unavailable', True, None)):
            with self.subTest(code=code):
                self.assertEnvelope(_dispatch(_probe(MachineView, error)), status, {
                    'schema_version': 1, 'error': {'code': code, 'message': str(error), 'retryable': retryable}},
                    retry_after=retry_after)

    def test_usage_view_bytes_and_headers(self):
        from mop.server.controller.usageviews import Usage
        from mop.server.sessions.usage_services import Unavailable
        error = Unavailable('usage admission requires no surrounding transaction')
        self.assertEnvelope(_dispatch(_probe(Usage, error)), 503, {'schema_version': 1, 'error': {
            'code': 'usage_unavailable', 'message': str(error), 'retryable': True}})

    def test_projection_response_bytes_and_headers(self):
        from mop.server.sessions.projections import SnapshotReset
        from mop.server.access.services import Denied
        for error, status, code, retryable, extra in (
                (SnapshotReset('authority changed while materializing snapshot'), 410, 'reset_required', False,
                 {'epoch': EPOCH, 'reset': {'required': True}}),
                (Denied('identity is inactive'), 403, 'permission_denied', False, {'epoch': EPOCH}),
                (ValueError('page_size must be within 1..500'), 400, 'validation_error', False, {'epoch': EPOCH})):
            with self.subTest(code=code):
                self.assertEnvelope(_dispatch(_read_probe(error)), status, {'schema_version': 1, 'error': {
                    'code': code, 'message': str(error), 'retryable': retryable}, **extra})

    def test_raw_database_error_never_leaks_through_any_entry(self):
        from django.db import DatabaseError
        for view in (*(_probe(base, DatabaseError('private database detail')) for base in _bases()),
                     _read_probe(DatabaseError('private database detail'))):
            response = _dispatch(view)
            self.assertEnvelope(response, 503, {'schema_version': 1, 'error': {
                'code': 'database_unavailable', 'message': 'controller database unavailable',
                'retryable': True}})
            self.assertNotIn(b'private', response.content)


class FailClosedDispatch(unittest.TestCase):
    """Незарегистрированное -- generic 500 Django, причина только в серверном логе."""

    def _generic(self, view):
        from django.core.handlers.exception import convert_exception_to_response
        with self.assertLogs('django.request', 'ERROR') as logged:
            response = convert_exception_to_response(view)(_request())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b'schema_version', response.content, 'не придуманный конверт')
        self.assertNotIn(b'private', response.content)
        self.assertTrue(logged.output, 'причина не проглатывается: она в серверном логе')
        return response

    def test_spoofed_refused_code_is_a_generic_500_on_every_entry(self):
        from mop.server.operations.service import Refused
        def spoofed():
            error = Refused('epoch_conflict', safe_reason='private attacker text')
            error.code = 'spoofed_code'
            return error
        for view in (*(_probe(base, spoofed()) for base in _bases()), _read_probe(spoofed())):
            response = self._generic(view)
            self.assertNotIn(b'spoofed_code', response.content)

    def test_raw_provider_error_is_a_generic_500_on_every_entry(self):
        for view in (*(_probe(base, RuntimeError('private provider detail')) for base in _bases()),
                     _read_probe(RuntimeError('private provider detail'))):
            self._generic(view)


class SameExceptionSameEnvelope(unittest.TestCase):
    """Приёмка #691: один и тот же отказ -- один конверт на каждом входе.

    Denied/NotVisible в read-семействе исключены сознательно: их коды
    (permission_denied, not_found с причиной) -- прежний контракт read-страниц,
    который byte-identity обязана сохранить.
    """

    def test_registered_refusals_do_not_depend_on_the_entry(self):
        from mop.server.operations.service import Refused
        from mop.server.sessions import endpoints as delivery
        from mop.server.sessions import services as execution
        from mop.server.sessions import usage_services as usage
        factories = (
            lambda: Refused('epoch_conflict'),
            lambda: Refused('admission_limit'),
            lambda: delivery.Conflict('renew target differs from current endpoint'),
            lambda: delivery.Capacity('shared durable admission is exhausted'),
            lambda: execution.Conflict('target changed; refresh before submitting'),
            lambda: execution.Unavailable('operation work port is not bound'),
            lambda: usage.Unavailable('usage admission requires no surrounding transaction'),
        )
        for make in factories:
            sample = make()
            with self.subTest(kind=type(sample).__qualname__, message=str(sample)):
                seen = set()
                for view in (*(_probe(base, make()) for base in _bases()), _read_probe(make())):
                    response = _dispatch(view)
                    seen.add((response.status_code, _bytes(response.data['error']),
                              response.get('Retry-After')))
                self.assertEqual(len(seen), 1, seen)


class GlobalPathUnchanged(unittest.TestCase):
    """Глобальные записи и framework путь не смягчаются (координация с #690)."""

    def test_access_and_database_entries_keep_their_bytes(self):
        from django.core.exceptions import ObjectDoesNotExist
        from django.db import DatabaseError
        from mop.server.access.services import Denied, NotVisible
        from mop.server.controller.authbackend import AuthenticationUnavailable
        cases = (
            (Denied('identity is inactive'), 403, 'access_denied', 'identity is inactive', False),
            (NotVisible('resource not found'), 404, 'not_found', 'resource not found', False),
            (ObjectDoesNotExist('Installation matching query does not exist'), 404, 'not_found',
             'resource not found', False),
            (AuthenticationUnavailable('directory unavailable'), 503, 'authority_unavailable',
             'directory unavailable', True),
            (DatabaseError('private database detail'), 503, 'database_unavailable',
             'controller database unavailable', True),
        )
        for error, status, code, message, retryable in cases:
            with self.subTest(code=code):
                response = _handler()(error, {})
                self.assertEqual(response.status_code, status)
                self.assertEqual(_bytes(_body(response)), _bytes({'schema_version': 1, 'error': {
                    'code': code, 'message': message, 'retryable': retryable}}))
                self.assertEqual(response['Cache-Control'], 'no-store')

    def test_uncertain_commit_stays_non_retryable_without_private_detail(self):
        from django.db import DatabaseError
        driver = Exception('private driver context')
        driver.outcome_unknown = True
        error = DatabaseError('private database detail')
        error.__cause__ = driver
        response = _handler()(error, {})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['error']['code'], 'database_outcome_unknown')
        self.assertEqual(response.data['error']['message'],
                         'database commit outcome unknown; reconcile before retry')
        self.assertFalse(response.data['error']['retryable'])
        self.assertNotIn('private', str(response.data))
        self.assertIsNone(response.get('Retry-After'), 'unknown-COMMIT не зовёт повторить')

    def test_framework_validation_errors_are_not_loosened(self):
        from rest_framework.exceptions import ValidationError, PermissionDenied, NotAuthenticated, Throttled
        cases = (
            (ValidationError({'non_field_errors': ['bad input']}), 400, 'validation_error',
             'input validation failed'),
            (ValidationError(['bad input']), 400, 'validation_error', 'input validation failed'),
            (PermissionDenied('authentication refused'), 403, 'permission_denied', 'authentication refused'),
            (NotAuthenticated(), 401, 'authentication_required',
             'Authentication credentials were not provided.'),
            (Throttled(wait=300, detail='authentication rate limit exceeded'), 429, 'throttled',
             'authentication rate limit exceeded Expected available in 300 seconds.'),
        )
        for error, status, code, message in cases:
            with self.subTest(code=code, kind=type(error).__name__):
                response = _handler()(error, {})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.data['schema_version'], 1)
                self.assertEqual(response.data['error']['code'], code)
                self.assertEqual(response.data['error']['message'], message)
                self.assertEqual(response.data['error']['retryable'], status == 429)
                self.assertEqual(response['Cache-Control'], 'no-store')

    def test_framework_validation_inside_an_input_view_keeps_framework_text(self):
        # #690: сериализаторы input-view поднимают DRF ValidationError -- это
        # не сырой ValueError, и запись validation_error с причиной его не
        # перехватывает: текст остаётся стабильным 'input validation failed'.
        from rest_framework.exceptions import ValidationError
        from mop.server.controller.endpointviews import EndpointView
        from mop.server.controller.machineviews import MachineView
        for view in (EndpointView, MachineView):
            with self.subTest(view=view.__name__):
                response = _handler()(ValidationError({'name': ['private field detail']}), {'view': view})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {'schema_version': 1, 'error': {
                    'code': 'validation_error', 'message': 'input validation failed', 'retryable': False}})

    def test_throttled_keeps_the_framework_retry_after(self):
        from rest_framework.exceptions import Throttled
        response = _handler()(Throttled(wait=300), {})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '300')

    def test_csrf_failure_keeps_its_stable_code(self):
        from rest_framework.exceptions import PermissionDenied
        response = _handler()(PermissionDenied('CSRF Failed: Origin checking failed'), {})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['error']['code'], 'csrf_failed')
        self.assertEqual(response.data['error']['message'], 'CSRF validation failed')
