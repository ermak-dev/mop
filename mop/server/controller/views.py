"""Синхронный DRF query инфраструктуры; admission принадлежит #625."""
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler as framework_exception_handler
from . import errors
from .runtime import installation_snapshot


def _envelope(entry, error, *, epoch=None):
    """Конверт {schema_version, error{code,message,retryable}} из записи реестра.

    Порядок ключей -- прежний: error, затем epoch, затем reset; no-store и
    Retry-After назначаются здесь, чтобы ни один view их не забыл.
    """
    body = {'schema_version': 1, 'error': {'code': entry.code,
        'message': str(error) if entry.message is None else entry.message,
        'retryable': entry.retryable}}
    if epoch is not None:
        body['epoch'] = epoch
    if entry.reset:
        body['reset'] = {'required': True}
    response = Response(body, status=entry.status)
    response['Cache-Control'] = 'no-store'
    if entry.retry_after is not None:
        response['Retry-After'] = entry.retry_after
    return response


def exception_handler(error, context):
    """Единственный перевод исключений границы в конверт (#691).

    Доменные отказы, доступ и БД -- по реестру errors; framework-исключения
    DRF -- по своему статусу. Чего нет ни там, ни там (незарегистрированный
    тип, подменённый код Refused), получает None: DRF делает re-raise, и
    клиент видит generic 500, а не придуманный статус.
    """
    if isinstance(error, errors.ReadRefusal):
        entry = errors.read_verdict(error)
        return None if entry is None else _envelope(entry, error.error, epoch=error.epoch)
    view = context.get('view') if isinstance(context, dict) else None
    entry = errors.verdict(error, value_error_is_input=getattr(view, 'value_error_is_input', False))
    if entry is not None:
        return _envelope(entry, error)
    response = framework_exception_handler(error, context)
    if response is not None:
        from .pagination import CursorReset
        detail = response.data.get('detail') if isinstance(response.data, dict) else None
        code = errors.FRAMEWORK_CODES.get(response.status_code, 'request_refused')
        message = str(detail) if detail is not None else 'input validation failed'
        if message.startswith('CSRF Failed:'):
            code, message = 'csrf_failed', 'CSRF validation failed'
        verdict = {'code': code, 'message': message, 'retryable': response.status_code in (429, 503)}
        if isinstance(error, CursorReset):
            verdict.update(code='reset_required', reset_required=True)
        response.data = {'schema_version': 1, 'error': verdict}
        response['Cache-Control'] = 'no-store'
    return response


class Health(APIView):
    def get(self, request):
        try:
            installation = installation_snapshot()
            response = Response({'schema_version': 1, 'state': 'ready', **installation})
        except RuntimeError as error:
            response = Response({'schema_version': 1, 'error': {
                'code': 'installation_unavailable', 'message': str(error), 'retryable': False}}, status=503)
        response['Cache-Control'] = 'no-store'
        return response
