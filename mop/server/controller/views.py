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
