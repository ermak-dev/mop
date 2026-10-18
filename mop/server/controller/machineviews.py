"""DRF/composition root v1: adapters связаны явно, по умолчанию deny."""
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.sessions import protocol as p
from mop.server.sessions.services import Conflict
# #625 связывает публичные ports здесь, не в sessions import graph.
# Несвязанный physical/approval port отказывает до admission.
from .machinebinding import compose, authority
# #690: строгий разбор query -- общий strict-модуль; machine-протокол
# продолжает использовать чистые валидаторы sessions.protocol вне HTTP.
from .strict import StrictJSONParser, machine_document, query_document as query

execution = compose()


class MachineView(APIView):
    # #691: сырой ValueError конечных парсеров (query/protocol) -- клиентский
    # 400; доменные отказы переводит глобальный handler по реестру errors.
    value_error_is_input = True
    # #690: единый строгий декодер тела (no-duplicate-keys, bounded, nonfinite).
    parser_classes = (StrictJSONParser,)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response


class Work(MachineView):
    def get(self, request):
        values = query(request.query_params, ('schema_version', 'node_id', 'executor_epoch'), optional=('limit',))
        rows = execution.poll(request.actor, p.uuid_value(values['node_id']),
            executor_epoch=p.uuid_value(values['executor_epoch']), limit=p.counter(values.get('limit', '10')))
        return Response({'schema_version': 1, 'work': [p.work_to_wire(row) for row in rows]})


class ActionInput(MachineView):
    def get(self, request):
        values = query(request.query_params, ('schema_version', 'operation_id', 'step_id'))
        work = execution.action_work(request.actor, operation_id=p.uuid_value(values['operation_id']),
            step_id=p.uuid_value(values['step_id']))
        payload = {}
        if work.action == 'send_message':
            payload = execution.work.message_input(work)
            # Decrypt может ждать external provider: current machine/lease/identity
            # проверяются повторно уже после получения protected bytes.
            if execution.action_work(request.actor, operation_id=work.operation_id, step_id=work.step_id) != work:
                raise Conflict('action input binding changed')
        from .machinebinding import owner_identity
        identity = owner_identity(work.owner_id)
        return Response({'schema_version':1, 'operation_id':str(work.operation_id),'step_id':str(work.step_id),
            'git_name':identity.name, 'git_email':identity.email, **payload})


class Launch(MachineView):
    def post(self, request):
        from mop.common.credentials import Credential
        from mop.server.access.services import node_executor
        from .launchdelivery import read
        work = p.work_from_wire(request.data)
        node_executor(request.actor, work.node_id)
        if request.auth is None:
            from mop.server.access.services import Denied
            raise Denied('machine token required for addressed launch')
        return Response(read(Credential('token', request.auth.key), p.work_to_wire(work)))


class InitialLaunch(MachineView):
    def post(self, request):
        from mop.common.credentials import Credential
        from mop.server.access.services import node_executor, Denied
        from .launchdelivery import read_initial
        work = p.work_from_wire(request.data)
        node_executor(request.actor, work.node_id)
        if request.auth is None:
            raise Denied('machine token required for initial launch')
        return Response(read_initial(Credential('token', request.auth.key), p.work_to_wire(work)))


class ExecutorState(MachineView):
    def get(self, request):
        values = query(request.query_params, ('schema_version','node_id'))
        node_id = p.uuid_value(values['node_id'])
        value = execution.executor_state(request.actor,node_id)
        return Response({'schema_version':1,'node_id':str(node_id),'executor_epoch':str(value)})


class Claim(MachineView):
    def post(self, request):
        value = machine_document(request.data, ('schema_version', 'request_id', 'work'))
        work = p.work_from_wire(value['work'])
        attempt = execution.claim(request.actor, work, request_id=p.uuid_value(value['request_id']))
        return Response({'schema_version': 1, 'attempt_id': str(attempt.id),
            'revision': str(attempt.revision), 'state': attempt.state,
            'lease_until': attempt.lease_until.isoformat(), 'work': p.work_to_wire(work)})


class Result(MachineView):
    def post(self, request):
        value = machine_document(request.data, ('schema_version', 'attempt_id', 'expected_revision',
                                                'receipt_id', 'outcome', 'evidence'))
        result = execution.result(request.actor, p.uuid_value(value['attempt_id']),
            expected_revision=p.counter(value['expected_revision']), receipt_id=p.uuid_value(value['receipt_id']),
            outcome=value['outcome'], evidence=value['evidence'])
        return Response(result.to_wire())


class Restarted(MachineView):
    def post(self, request):
        value = machine_document(request.data, ('schema_version', 'node_id', 'previous_epoch', 'new_epoch'))
        result = execution.executor_restarted(request.actor, p.uuid_value(value['node_id']),
            previous_epoch=p.uuid_value(value['previous_epoch']), new_epoch=p.uuid_value(value['new_epoch']))
        return Response({'schema_version': 1, 'executor_epoch': str(result), 'replay_allowed': False})


class Facts(MachineView):
    def get(self, request):
        values = query(request.query_params, ('schema_version', 'node_id', 'kind'), optional=('limit',))
        page = execution.facts(request.actor, p.uuid_value(values['node_id']),
            kind=values['kind'], limit=p.counter(values.get('limit', '100')))
        return Response({'schema_version': 1, 'node_id': str(page.node_id), 'kind': page.kind,
            'complete': page.complete, 'items': [{
                'target_id': str(item.target_id) if item.target_id is not None else None,
                'incarnation_id': str(item.incarnation_id) if item.incarnation_id is not None else None,
                'sequence': str(item.sequence), 'observed_at': item.observed_at.isoformat(),
                'received_at': item.received_at.isoformat(), 'state': item.state,
                'values': dict(item.values)} for item in page.items]})
