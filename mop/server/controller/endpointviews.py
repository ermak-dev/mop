"""DRF v1 root для presence/reports. Ни один route не принимает socket path."""
from datetime import datetime
from uuid import UUID
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.sessions import endpoints as s
from .models import Installation
from .runtime import require_restore_ready
from .strict import StrictJSONParser, uid, strict_document, query_document


def authority(*, lock=False):
    try:
        require_restore_ready(lock=lock)
        rows = Installation.objects.select_for_update() if lock else Installation.objects
        row = rows.get(pk=1)
        return s.EndpointAuthority(row.epoch, row.pk, row.effects_enabled)
    except RuntimeError as error:
        raise s.Unavailable(str(error)) from None


# SQL grant root общий с command/read composition. Private bytes проходят
# отдельный finite protected port; web никогда не конструирует cipher/provider.
def default_endpoints():
    from .machinebinding import local_grants
    from .endpointbinding import ProtectedEndpoints
    return ProtectedEndpoints(authority=authority, grants=local_grants().local_authority())


endpoints = default_endpoints()


# #690: приватные exact/uid заменены общими строгими полями strict-модуля;
# тексты отказов и статус прежние (400 validation_error).

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
    # Тот же SQL-only root для authentication и subject service, не request JSON.
    @property
    def grant_resolver(self):
        return endpoints.grants
    # #691: сырой ValueError конечных парсеров (exact/uid) -- клиентский 400;
    # доменные отказы переводит глобальный handler по реестру errors.
    value_error_is_input = True
    # #690: единый строгий декодер тела (no-duplicate-keys, bounded, nonfinite).
    parser_classes = (StrictJSONParser,)

    def context(self, request):
        return endpoints.trusted_context(request.actor, uid(request.headers.get('X-Mop-Endpoint')),
                                         uid(request.headers.get('X-Mop-Connection')))

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response


class Register(EndpointView):
    def post(self, request):
        value = strict_document(request.data, ('schema_version', 'client_id', 'project_id',
                                               'external_session_id', 'display_name'))
        endpoint = endpoints.register(request.actor, client_id=uid(value['client_id']),
            project_id=uid(value['project_id']),
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
        strict_document(request.data, ('schema_version',))
        context = self.context(request)
        if context.endpoint_id != endpoint_id:
            raise s.Conflict('renew target differs from current endpoint')
        return Response({'schema_version': 1, 'endpoint': presence(endpoints.renew(context))})

    def delete(self, request, endpoint_id):
        strict_document(request.data, ('schema_version',))
        context = self.context(request)
        if context.endpoint_id != endpoint_id:
            raise s.Conflict('retire target differs from current endpoint')
        endpoints.retire(context)
        return Response(status=204)


class Reports(EndpointView):
    def post(self, request):
        value = strict_document(request.data, ('schema_version', 'epoch', 'address', 'message_id',
                                               'text', 'not_after'),
                                optional=('operation_request_id', 'notify_when_idle', 'priority'))
        if type(value['not_after']) is not str:
            raise ValueError('aware delivery deadline required')
        try:
            deadline = datetime.fromisoformat(value['not_after'].replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('aware delivery deadline required') from None
        from mop.common.credentials import Credential
        credential = (Credential('token', request.auth.key) if request.auth is not None
                      else Credential('session', request.session.session_key))
        has_sender = any(name in request.headers for name in ('X-Mop-Endpoint', 'X-Mop-Connection'))
        # Только locators; holder_context резолвит credential daemon из SQL.
        delivery = endpoints.send_private_report(credential, expected_epoch=uid(value['epoch']),
            address=value['address'], message_id=uid(value['message_id']),
            text=value['text'], not_after=deadline, operation_request_id=uid(value['operation_request_id'])
            if value.get('operation_request_id') is not None else None,
            notify_when_idle=value.get('notify_when_idle', False), priority=value.get('priority', 'next'),
            sender_endpoint_id=uid(request.headers.get('X-Mop-Endpoint')) if has_sender else None,
            sender_connection_nonce=uid(request.headers.get('X-Mop-Connection')) if has_sender else None)
        response = Response({'schema_version': 1, 'delivery': result(delivery)}, status=202)
        response['Location'] = '/api/v1/deliveries/'+str(delivery.id)
        return response


class Deliveries(EndpointView):
    def get(self, request):
        value = query_document(request.query_params, ('schema_version', 'epoch', 'message_id'))
        delivery = endpoints.delivery_for_message(request.actor,
            expected_epoch=uid(value['epoch']), message_id=uid(value['message_id']))
        return Response({'schema_version': 1, 'delivery': result(delivery)})


class Delivery(EndpointView):
    def get(self, request, delivery_id):
        return Response({'schema_version': 1, 'delivery': result(endpoints.delivery(request.actor, delivery_id))})


class Ack(EndpointView):
    def post(self, request, delivery_id):
        value = strict_document(request.data, ('schema_version', 'outcome'))
        value = endpoints.acknowledge(self.context(request), delivery_id, outcome=value['outcome'])
        return Response({'schema_version': 1, 'delivery': result(value)})
