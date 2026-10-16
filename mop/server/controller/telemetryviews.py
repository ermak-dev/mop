"""DRF boundary #641: authenticated node scope, bounded bodies, private pages."""
import json
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser
from rest_framework.response import Response
from mop.common.telemetry import MAX_RECORD, Observation, SessionScope, counter, fields
from mop.server.sessions.telemetry import Telemetry
from .machineviews import MachineView, authority


telemetry = Telemetry(installation=authority)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate telemetry field')
        result[key] = value
    return result


class BoundedJson(BaseParser):
    media_type = 'application/json'

    def parse(self, stream, media_type=None, parser_context=None):
        body = stream.read(MAX_RECORD+1)
        if len(body) > MAX_RECORD:
            raise ParseError('telemetry body exceeds limit')
        try:
            return json.loads(body, object_pairs_hook=unique)
        except (ValueError, UnicodeError):
            raise ParseError('invalid telemetry JSON') from None


class Ingest(MachineView):
    parser_classes = (BoundedJson,)

    def post(self, request):
        result = telemetry.ingest(request.actor, Observation.parse(request.data))
        return Response(result.wire())


class Latest(MachineView):
    parser_classes = (BoundedJson,)

    def post(self, request):
        fields(request.data, ('scope',))
        row = telemetry.latest(request.actor, SessionScope.parse(request.data['scope']))
        return Response(dict(current_model=row.current_model, last_confirmed_model=row.last_confirmed_model,
            model_hint=row.model_hint, sequence=str(row.sequence), incomplete=row.incomplete,
            received_at=row.received_at.isoformat()))


def history_arguments(value):
    fields(value, ('scope', 'expected_revision'))
    scope = SessionScope.parse(value['scope'])
    revision = 0 if value['expected_revision'] == '0' else counter(value['expected_revision'])
    return scope, revision


class History(MachineView):
    parser_classes = (BoundedJson,)

    def post(self, request):
        scope, revision = history_arguments(request.data)
        page = telemetry.read_history(request.actor, scope, expected_revision=revision)
        return Response(dict(page, revision=str(page['revision'])))


class ResetHistory(MachineView):
    parser_classes = (BoundedJson,)

    def post(self, request):
        scope, revision = history_arguments(request.data)
        result = telemetry.reset_history(request.actor, scope, expected_revision=revision)
        return Response(dict(revision=str(result)))
