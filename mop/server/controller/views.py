"""Синхронный DRF query инфраструктуры; admission принадлежит #625."""
from django.db import DatabaseError
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler as framework_exception_handler
from .runtime import installation_snapshot


def exception_handler(error, context):
    """Отказ DB, включая auth lookup, не становится anonymous/empty success."""
    if isinstance(error, DatabaseError):
        response = Response({'schema_version': 1, 'error': {
            'code': 'database_unavailable', 'message': 'controller database unavailable',
            'retryable': True}}, status=503)
        response['Cache-Control'] = 'no-store'
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
    return framework_exception_handler(error, context)


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
