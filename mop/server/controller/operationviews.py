"""Finite DRF commands и queries; accepted возвращается после настоящего COMMIT."""
from uuid import UUID
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from ..operations.commands import decode_body, InvalidCommand
from ..operations.node import parse
from .runtime import operations_service


def operations_api():
    # Никаких fake effects и динамического registry в production root.
    return operations_service()


def _reply(operation, status=200):
    operation = {**operation, 'revision': str(operation['revision'])}
    response = Response({'schema_version': 1, 'epoch': operation['epoch'], 'operation': operation}, status=status)
    response['Cache-Control'] = 'no-store'
    if status == 202:
        response['Location'] = '/api/v1/operations/' + operation['id']
    return response


class NodeEnvelope(serializers.Serializer):
    """Serializer принимает только конечный canonical command value."""
    def to_internal_value(self, data):
        try:
            return parse(self.context['node_id'], data, eligible=self.context['eligible'])
        except InvalidCommand as error:
            raise serializers.ValidationError({'non_field_errors': [str(error)]}) from None


@method_decorator(never_cache, name='dispatch')
class DrainNode(APIView):
    eligible = False

    def post(self, request, node_id):
        try:
            value = decode_body(request.body)
        except InvalidCommand as error:
            raise serializers.ValidationError(str(error)) from None
        serializer = NodeEnvelope(data=value, context={'node_id': node_id, 'eligible': self.eligible})
        serializer.is_valid(raise_exception=True)
        operation = operations_api().submit(request.actor, serializer.validated_data)
        return _reply(operation, status=202 if operation['state'] == 'accepted' else 200)


class EnableNode(DrainNode):
    eligible = True


@method_decorator(never_cache, name='dispatch')
class OperationStatus(APIView):
    def get(self, request, operation_id):
        epoch = request.query_params.get('epoch')
        try:
            epoch = UUID(epoch) if epoch is not None else None
        except (ValueError, TypeError):
            raise serializers.ValidationError('invalid epoch') from None
        return _reply(operations_api().status(request.actor, operation_id, epoch=epoch))


@method_decorator(never_cache, name='dispatch')
class RequestLookup(APIView):
    def get(self, request):
        if set(request.query_params) != {'request_id', 'epoch'}:
            raise serializers.ValidationError('request_id and epoch are required')
        try:
            request_id = UUID(request.query_params['request_id'])
            epoch = UUID(request.query_params['epoch'])
        except (ValueError, TypeError):
            raise serializers.ValidationError('invalid request identity') from None
        return _reply(operations_api().by_request(request.actor, request_id, epoch=epoch))
