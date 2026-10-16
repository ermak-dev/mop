"""REST открывает допуск, WS не превращается во второй command API."""
from uuid import UUID
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.sessions.terminal_control import Terminals, Conflict, Capacity
from .authentication import actor_for_request
from .authviews import FiniteSerializer


class OpenInput(FiniteSerializer):
    schema_version=serializers.ChoiceField(choices=[1])
    target_id=serializers.UUIDField()
    incarnation_id=serializers.UUIDField()
    mode=serializers.ChoiceField(choices=['ro','rw'])
    cols=serializers.IntegerField(min_value=20,max_value=500)
    rows=serializers.IntegerField(min_value=5,max_value=200)


class Open(APIView):
    def post(self,request):
        value=OpenInput(data=request.data)
        value.is_valid(raise_exception=True)
        args=dict(value.validated_data)
        args.pop('schema_version')
        try:
            result=Terminals().open(actor_for_request(request),**args)
        except Conflict as exc:
            return Response({'error':str(exc)},status=409)
        except Capacity as exc:
            return Response({'error':str(exc)},status=429)
        return Response({'schema_version':1,**result},status=201)


class Close(APIView):
    def delete(self,request,attachment_id:UUID):
        # SQL fence закрыт сразу; socket отцепляется при recheck до следующей
        # приватной доставки. Assignment/PTY body этот endpoint не останавливает.
        Terminals().abandon(actor_for_request(request),attachment_id)
        return Response(status=204)
