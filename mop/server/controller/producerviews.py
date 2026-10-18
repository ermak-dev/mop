"""Конечная authenticated регистрация, не endpoint для hook JSON."""
from rest_framework.response import Response

from mop.server.sessions.producer import RegistrationRequest
from .machineviews import MachineView
from .telemetryroot import producer_registration
from .telemetryviews import BoundedJson


class RegisterProducer(MachineView):
    parser_classes = (BoundedJson,)

    def post(self, request):
        result = producer_registration.register(request.actor, RegistrationRequest.parse(request.data))
        return Response(result.wire())
