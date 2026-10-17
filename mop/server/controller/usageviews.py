"""DRF query adapter: никакого доступа к destructive proxy queue."""
from rest_framework.response import Response
from .machineviews import MachineView
from .strict import query_document
from mop.server.sessions.usage_services import UsageQueries, Unavailable


class Usage(MachineView):
    def handle_exception(self, error):
        if isinstance(error, Unavailable):
            return Response({'schema_version': 1, 'error': {'code': 'usage_unavailable',
                'message': str(error), 'retryable': True}}, status=503)
        return super().handle_exception(error)

    def get(self, request):
        values = query_document(request.query_params, ('schema_version', 'start', 'end'),
                                optional=('limit', 'after'))
        limit = values.get('limit', '100')
        if not limit.isascii() or not limit.isdecimal():
            raise ValueError('invalid usage page size')
        return Response(UsageQueries().read(request.actor, start=values['start'], end=values['end'],
            limit=int(limit), after=values.get('after', '')))
