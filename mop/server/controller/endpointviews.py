"""DRF v1 root для presence/reports. Ни один route не принимает socket path."""
from datetime import datetime
from uuid import UUID
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.sessions import endpoints as s
from .models import Installation
from .runtime import require_restore_ready


def authority(*, lock=False):
    try:
        require_restore_ready(lock=lock)
        rows = Installation.objects.select_for_update() if lock else Installation.objects
        row = rows.get(pk=1)
        return s.EndpointAuthority(row.epoch, row.pk, row.effects_enabled)
    except RuntimeError as error:
        raise s.Unavailable(str(error)) from None


# #628/#642 устанавливают root-owned transport/grant/cipher. Без cipher private
# send отказывает, а registration/query уже работают через стандартный Auth.
endpoints = s.Endpoints(authority=authority)


def exact(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys()-set(required)-set(optional):
        raise ValueError('unknown or missing endpoint fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported endpoint schema version')


def uid(value):
    if type(value) is not str:
        raise ValueError('canonical UUID string required')
    try:
        result = UUID(value)
    except ValueError:
        raise ValueError('canonical UUID string required') from None
    if str(result) != value:
        raise ValueError('canonical UUID string required')
    return result


def result(value):
    return {'id': str(value.id), 'message_id': str(value.message_id),
        'operation_request_id': str(value.operation_request_id) if value.operation_request_id else None,
        'state': value.state, 'idle': value.idle}


def presence(value):
    return {'id': str(value.id), 'session_id': str(value.session_id), 'address': value.address,
        'project_id': str(value.project_id), 'external_session_id': value.external_session_id,
        'display_name': value.display_name, 'expires_at': value.expires_at.isoformat(),
        'state': value.state, 'live': value.live}


class EndpointView(APIView):
    # Grant resolver назначается доверенной root composition, не request JSON.
    grant_resolver = None
    # #691: сырой ValueError конечных парсеров (exact/uid) -- клиентский 400;
    # доменные отказы переводит глобальный handler по реестру errors.
    value_error_is_input = True

    def context(self, request):
        return endpoints.trusted_context(request.actor, uid(request.headers.get('X-Mop-Endpoint')),
                                         uid(request.headers.get('X-Mop-Connection')))

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response


class Register(EndpointView):
    def post(self, request):
        value = request.data
        exact(value, ('schema_version', 'client_id', 'project_id', 'external_session_id', 'display_name'))
        endpoint = endpoints.register(request.actor, client_id=uid(value['client_id']), project_id=uid(value['project_id']),
            external_session_id=value['external_session_id'], display_name=value['display_name'])
        return Response({'schema_version': 1, 'endpoint': presence(endpoint)}, status=201)


class Addresses(EndpointView):
    def get(self, request):
        values = request.query_params
        if set(values)-{'schema_version', 'project_id', 'limit'} or any(len(values.getlist(k)) != 1 for k in values):
            raise ValueError('unknown or duplicate presence query fields')
        if values.get('schema_version') != '1':
            raise ValueError('unsupported endpoint schema version')
        limit = values.get('limit', '100')
        if not limit.isdecimal() or len(limit) > 3:
            raise ValueError('invalid presence limit')
        return Response({'schema_version': 1, 'addresses': [presence(x) for x in endpoints.addresses(
            request.actor, uid(values.get('project_id')), limit=int(limit))]})


class Address(EndpointView):
    def get(self, request, endpoint_id):
        return Response({'schema_version': 1, 'endpoint': presence(endpoints.presence(request.actor, endpoint_id))})


class Renew(EndpointView):
    def post(self, request, endpoint_id):
        exact(request.data, ('schema_version',))
        context = self.context(request)
        if context.endpoint_id != endpoint_id:
            raise s.Conflict('renew target differs from current endpoint')
        return Response({'schema_version': 1, 'endpoint': presence(endpoints.renew(context))})

    def delete(self, request, endpoint_id):
        exact(request.data, ('schema_version',))
        context = self.context(request)
        if context.endpoint_id != endpoint_id:
            raise s.Conflict('retire target differs from current endpoint')
        endpoints.retire(context)
        return Response(status=204)


class Reports(EndpointView):
    def post(self, request):
        value = request.data
        exact(value, ('schema_version', 'epoch', 'address', 'message_id', 'text', 'not_after'),
            ('operation_request_id', 'notify_when_idle', 'priority'))
        if type(value['not_after']) is not str:
            raise ValueError('aware delivery deadline required')
        try:
            deadline = datetime.fromisoformat(value['not_after'].replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('aware delivery deadline required') from None
        sender = self.context(request) if 'X-Mop-Endpoint' in request.headers else None
        delivery = endpoints.send(request.actor, expected_epoch=uid(value['epoch']),
            address=value['address'], message_id=uid(value['message_id']),
            text=value['text'], not_after=deadline, operation_request_id=uid(value['operation_request_id'])
            if value.get('operation_request_id') is not None else None,
            notify_when_idle=value.get('notify_when_idle', False), priority=value.get('priority', 'next'), sender=sender)
        response = Response({'schema_version': 1, 'delivery': result(delivery)}, status=202)
        response['Location'] = '/api/v1/deliveries/'+str(delivery.id)
        return response


class Delivery(EndpointView):
    def get(self, request, delivery_id):
        return Response({'schema_version': 1, 'delivery': result(endpoints.delivery(request.actor, delivery_id))})


class Ack(EndpointView):
    def post(self, request, delivery_id):
        exact(request.data, ('schema_version', 'outcome'))
        value = endpoints.acknowledge(self.context(request), delivery_id, outcome=request.data['outcome'])
        return Response({'schema_version': 1, 'delivery': result(value)})
