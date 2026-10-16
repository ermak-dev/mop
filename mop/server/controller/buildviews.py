"""DRF build/rebuild: только finite submit и долговечный status/progress."""
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import serializers
from rest_framework.views import APIView
from ..builds.commands import parse
from ..operations.commands import decode_body, InvalidCommand
from .runtime import builds_service
from .operationviews import _reply


@method_decorator(never_cache, name='dispatch')
class BuildProject(APIView):
    def post(self, request, project_id):
        try:
            command = parse(project_id, decode_body(request.body))
        except InvalidCommand as error:
            raise serializers.ValidationError(str(error)) from None
        operation = builds_service().submit(request.actor, command)
        return _reply(operation, status=202 if operation['state']=='accepted' else 200)
