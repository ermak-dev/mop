"""Конечный Landing v1: query никогда не становится командой."""
from rest_framework import serializers
from rest_framework.response import Response

from .authviews import PrivateView, CatalogRevision
from .strict import FiniteSerializer, StrictJSONParser, SchemaVersionField, UUIDField
from .landingbinding import landing_service


class LandingRevision(CatalogRevision):
    def to_internal_value(self, value):
        if type(value) is str and value == '0':
            return 0
        return super().to_internal_value(value)


class LandingCommand(FiniteSerializer):
    schema_version = SchemaVersionField()
    epoch = UUIDField()
    expected_revision = LandingRevision()


class LandingQuery(FiniteSerializer):
    epoch = UUIDField(required=False)


def query(request, *, read=False):
    values = request.query_params
    if any(len(values.getlist(name)) != 1 for name in values) or not read and values:
        raise serializers.ValidationError('unknown or duplicate landing query fields')
    serializer = LandingQuery(data=dict(values.items()))
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class LandingDetail(PrivateView):
    def get(self, request, project_id):
        return Response(landing_service().get(request.actor, project_id, **query(request, read=True)))


class TakeLanding(PrivateView):
    parser_classes = (StrictJSONParser,)
    value_error_is_input = True

    def change(self, actor, project_id, values):
        return landing_service().acquire(actor, project_id, **values)

    def post(self, request, project_id):
        query(request)
        serializer = LandingCommand(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        return Response(self.change(request.actor, project_id,
            {'epoch': values['epoch'], 'expected_revision': values['expected_revision']}))


class ReleaseLanding(TakeLanding):
    def change(self, actor, project_id, values):
        return landing_service().release(actor, project_id, **values)
