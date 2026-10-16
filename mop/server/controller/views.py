"""Синхронный DRF query инфраструктуры; admission принадлежит #625."""
from django.db import DatabaseError
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler as framework_exception_handler
from .runtime import installation_snapshot


def exception_handler(error, context):
    """Отказ DB, включая auth lookup, не становится anonymous/empty success."""
    if isinstance(error, DatabaseError):
        unknown = getattr(error.__cause__, 'outcome_unknown', False)
        response = Response({'schema_version': 1, 'error': {
            'code': 'database_outcome_unknown' if unknown else 'database_unavailable',
            'message': ('database commit outcome unknown; reconcile before retry' if unknown
                        else 'controller database unavailable'),
            'retryable': not unknown}}, status=503)
        response['Cache-Control'] = 'no-store'
        return response
    from mop.server.operations.service import Refused
    if isinstance(error, Refused):
        response = Response({'schema_version': 1, 'error': {'code': error.code,
            'message': str(error), 'retryable': error.status in (429, 503)}}, status=error.status)
        response['Cache-Control'] = 'no-store'
        if error.status == 429:
            response['Retry-After'] = '1'
        return response
    from django.core.exceptions import ObjectDoesNotExist
    from mop.server.access.services import Denied, NotVisible
    from .authbackend import AuthenticationUnavailable
    if isinstance(error, (Denied, AuthenticationUnavailable, ObjectDoesNotExist)):
        status = 503 if isinstance(error, AuthenticationUnavailable) else 404 if isinstance(error, (NotVisible, ObjectDoesNotExist)) else 403
        message = 'resource not found' if status == 404 else str(error)
        response = Response({'schema_version': 1, 'error': {'code':
            'authority_unavailable' if status == 503 else 'not_found' if status == 404 else 'access_denied',
            'message': message, 'retryable': status == 503}}, status=status)
        response['Cache-Control'] = 'no-store'
        return response
    response = framework_exception_handler(error, context)
    if response is not None:
        from .pagination import CursorReset
        detail = response.data.get('detail') if isinstance(response.data, dict) else None
        code = {400: 'validation_error', 401: 'authentication_required', 403: 'permission_denied',
            404: 'not_found', 405: 'method_not_allowed', 429: 'throttled'}.get(response.status_code, 'request_refused')
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
