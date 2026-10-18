"""Отдельный конечный DRF read adapter журнала Operation, без effect/admission."""
from uuid import UUID
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.views import APIView

from ..operations.audit import AuditQueries
from . import strict
from .machinebinding import local_grants
from .projectionviews import read_authority, response


def parameters(request):
    query = request.query_params
    if set(query) - {'project_id','cursor','limit'} or any(len(query.getlist(key)) != 1 for key in query):
        raise ValueError('unknown or repeated query fields')
    result = {}
    if 'project_id' in query:
        result['project_id'] = strict.uuid_field(query['project_id'])
    if 'limit' in query:
        raw = query['limit']
        if not raw.isascii() or not raw.isdecimal() or raw.startswith('0') or len(raw) > 3:
            raise ValueError('limit must be within 1..100')
        result['limit'] = int(raw)
    if 'cursor' in query:
        result['cursor'] = query['cursor']
    return result


@method_decorator(never_cache, name='dispatch')
class Audit(APIView):
    http_method_names = ['get','head','options']

    @property
    def grant_resolver(self):
        return local_grants().local_authority()

    def get(self, request):
        return response(lambda: AuditQueries(epoch=lambda:UUID(read_authority()['epoch']),
            grants=self.grant_resolver).read(request.actor, **parameters(request)).wire())
