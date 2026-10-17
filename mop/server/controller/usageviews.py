"""DRF query adapter: никакого доступа к destructive proxy queue."""
from rest_framework.response import Response
from .machineviews import MachineView, query
from mop.server.sessions.usage_services import UsageQueries


class Usage(MachineView):
    def get(self, request):
        values = query(request, ('schema_version', 'start', 'end'), optional=('limit', 'after'))
        limit = values.get('limit', '100')
        if not limit.isascii() or not limit.isdecimal():
            raise ValueError('invalid usage page size')
        return Response(UsageQueries().read(request.actor, start=values['start'], end=values['end'],
            limit=int(limit), after=values.get('after', '')))
