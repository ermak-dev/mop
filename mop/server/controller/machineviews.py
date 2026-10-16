"""DRF/composition root v1: adapters связаны явно, по умолчанию deny."""
from uuid import UUID
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.sessions import protocol as p
from mop.server.sessions.services import Conflict, Unavailable
from django.db import transaction
from .runtime import installation_snapshot, require_restore_ready, machine_execution_service


def authority():
    try:
        if transaction.get_connection().in_atomic_block:
            require_restore_ready(lock=True)
        value = installation_snapshot()
        return p.AuthorityV1(UUID(value['epoch']), value['effects_enabled'])
    except RuntimeError as error:
        raise Unavailable(str(error)) from None


# #625 связывает existing Operation/Step port здесь, не в sessions import graph.
# Отсутствие #635/#640 adapters — явный 503, не fake authority/вторая очередь.
execution = machine_execution_service(installation=authority)


def query(request, names, *, optional=()):
    values = request.query_params
    if set(values) - set(names) - set(optional) or not set(names) <= set(values):
        raise ValueError('unknown or missing query fields')
    if any(len(values.getlist(name)) != 1 for name in values):
        raise ValueError('duplicate query fields')
    result = dict(values.items())
    if result.get('schema_version') != '1':
        raise ValueError('unsupported schema version')
    return result


class MachineView(APIView):
    def handle_exception(self, error):
        if isinstance(error, (Conflict, Unavailable, ValueError)):
            status = 503 if isinstance(error, Unavailable) else 409 if isinstance(error, Conflict) else 400
            response = Response({'schema_version': 1, 'error': {
                'code': 'authority_unavailable' if status == 503 else 'execution_conflict' if status == 409 else 'validation_error',
                'message': str(error), 'retryable': status == 503}}, status=status)
            response['Cache-Control'] = 'no-store'
            return response
        return super().handle_exception(error)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response


class Work(MachineView):
    def get(self, request):
        values = query(request, ('schema_version', 'node_id', 'executor_epoch'), optional=('limit',))
        rows = execution.poll(request.actor, p.uuid_value(values['node_id']),
            executor_epoch=p.uuid_value(values['executor_epoch']), limit=p.counter(values.get('limit', '10')))
        return Response({'schema_version': 1, 'work': [p.work_to_wire(row) for row in rows]})


class Claim(MachineView):
    def post(self, request):
        p.exact(request.data, ('schema_version', 'request_id', 'work'))
        work = p.work_from_wire(request.data['work'])
        attempt = execution.claim(request.actor, work, request_id=p.uuid_value(request.data['request_id']))
        return Response({'schema_version': 1, 'attempt_id': str(attempt.id),
            'revision': str(attempt.revision), 'state': attempt.state,
            'lease_until': attempt.lease_until.isoformat(), 'work': p.work_to_wire(work)})


class Result(MachineView):
    def post(self, request):
        value = request.data
        p.exact(value, ('schema_version', 'attempt_id', 'expected_revision', 'receipt_id', 'outcome', 'evidence'))
        result = execution.result(request.actor, p.uuid_value(value['attempt_id']),
            expected_revision=p.counter(value['expected_revision']), receipt_id=p.uuid_value(value['receipt_id']),
            outcome=value['outcome'], evidence=value['evidence'])
        return Response(result.to_wire())


class Restarted(MachineView):
    def post(self, request):
        value = request.data
        p.exact(value, ('schema_version', 'node_id', 'previous_epoch', 'new_epoch'))
        result = execution.executor_restarted(request.actor, p.uuid_value(value['node_id']),
            previous_epoch=p.uuid_value(value['previous_epoch']), new_epoch=p.uuid_value(value['new_epoch']))
        return Response({'schema_version': 1, 'executor_epoch': str(result), 'replay_allowed': False})


class Facts(MachineView):
    def get(self, request):
        values = query(request, ('schema_version', 'node_id', 'kind'), optional=('limit',))
        page = execution.facts(request.actor, p.uuid_value(values['node_id']),
            kind=values['kind'], limit=p.counter(values.get('limit', '100')))
        return Response({'schema_version': 1, 'node_id': str(page.node_id), 'kind': page.kind,
            'complete': page.complete, 'items': [{
                'target_id': str(item.target_id) if item.target_id is not None else None,
                'incarnation_id': str(item.incarnation_id) if item.incarnation_id is not None else None,
                'sequence': str(item.sequence), 'observed_at': item.observed_at.isoformat(),
                'received_at': item.received_at.isoformat(), 'state': item.state,
                'values': dict(item.values)} for item in page.items]})
