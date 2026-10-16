"""DRF адаптеры read model; источник/эффекты не запускаются из GET."""
from uuid import UUID
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.access.context import PrincipalKind
from mop.server.access.services import Access, Denied, NotVisible
from mop.server.sessions.projections import PoolQueries, SnapshotReset, SnapshotCapacity, SourceConflict
from mop.server.sessions.projection_writes import ProjectionWriter, ControllerRun
from mop.server.sessions.projection_models import ProjectionRuntime
from mop.server.sessions.protocol import uuid_value, counter, deadline, exact
from .runtime import installation_snapshot


class ReadAuthorityUnavailable(RuntimeError):
    """Уже безопасный отказ foundation, не raw exception внешнего adapter."""


def read_authority():
    try:
        return installation_snapshot()
    except RuntimeError as error:
        raise ReadAuthorityUnavailable(str(error)) from None


def queries():
    return PoolQueries(epoch=lambda: UUID(read_authority()['epoch']))


def response(function):
    epoch = None
    try:
        epoch = read_authority()['epoch']
        result, status = function(), 200
    except (ValueError, Denied, ReadAuthorityUnavailable) as error:
        code, status = 'validation_error', 400
        if isinstance(error, ReadAuthorityUnavailable):
            code, status = 'authority_unavailable', 503
        elif isinstance(error, SnapshotReset):
            code, status = 'reset_required', 410
        elif isinstance(error, SnapshotCapacity):
            code, status = 'unavailable', 503
        elif isinstance(error, SourceConflict):
            code, status = 'incarnation_conflict', 409
        elif isinstance(error, NotVisible):
            code, status = 'not_found', 404
        elif isinstance(error, Denied):
            code, status = 'permission_denied', 403
        result = {'schema_version':1, 'error':{'code':code, 'message':str(error), 'retryable':status == 503}}
        if epoch is not None:
            result['epoch'] = epoch
        if isinstance(error, SnapshotReset):
            result['reset'] = {'required':True}
    value = Response(result, status=status)
    value['Cache-Control'] = 'no-store'
    return value


def parameters(request):
    query = request.query_params
    if set(query) - {'project_id','page_size','cursor'} or any(len(query.getlist(key)) != 1 for key in query):
        raise ValueError('unknown or repeated query fields')
    result = {}
    if 'project_id' in query:
        result['project_id'] = uuid_value(query['project_id'])
    if 'page_size' in query:
        raw = query['page_size']
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 3:
            raise ValueError('page_size must be within 1..500')
        result['page_size'] = int(raw)
    if 'cursor' in query:
        result['cursor'] = query['cursor']
    return result


class PoolSnapshot(APIView):
    def get(self, request):
        return response(lambda: queries().snapshot(request.actor, **parameters(request)))


class ProjectionEvents(APIView):
    def get(self, request):
        def read():
            params = parameters(request)
            if 'cursor' not in params:
                raise ValueError('event cursor is required')
            params['limit'] = params.pop('page_size', 100)
            return queries().events_after(request.actor, **params)
        return response(read)


class PuppetDetail(APIView):
    def get(self, request, target_id):
        def read():
            if request.query_params:
                raise ValueError('detail query accepts no fields')
            return queries().detail(request.actor, target_id)
        return response(read)


class ObservationContext(APIView):
    def get(self, request):
        def read():
            actor = request.actor
            if actor.kind is not PrincipalKind.SERVICE:
                raise Denied('node observation context requires a service credential')
            Access().authorize(actor, 'execution.ingest_observations', node_ids=(actor.node_id,))
            runtime = ProjectionRuntime.objects.filter(pk=1, epoch=UUID(read_authority()['epoch'])).first()
            if runtime is None:
                raise SnapshotCapacity('observation startup is unavailable')
            return {'schema_version':1, 'epoch':str(runtime.epoch), 'controller_run':str(runtime.run_id)}
        return response(read)


def source_writer():
    epoch = UUID(read_authority()['epoch'])
    runtime = ProjectionRuntime.objects.filter(pk=1, epoch=epoch).first()
    if runtime is None:
        raise SnapshotCapacity('observation startup is unavailable')
    return ProjectionWriter(ControllerRun(epoch, runtime.run_id))


class ProducerStatus(APIView):
    def get(self, request, target_id, kind):
        def read():
            if request.query_params:
                raise ValueError('producer status accepts no query fields')
            writer = source_writer()
            return {'schema_version':1, 'epoch':str(writer.run.epoch),
                    **writer.producer(request.actor, target_id=target_id, kind=kind)}
        return response(read)


class Producers(APIView):
    def post(self, request):
        def bind():
            body = request.data
            exact(body, {'schema_version','controller_run','target_id','incarnation_id','executor_epoch',
                         'producer_epoch','kind','expected_revision'})
            epoch = UUID(read_authority()['epoch'])
            writer = ProjectionWriter(ControllerRun(epoch, uuid_value(body['controller_run'])))
            revision = writer.bind_producer(request.actor, target_id=uuid_value(body['target_id']),
                incarnation_id=uuid_value(body['incarnation_id']), executor_epoch=uuid_value(body['executor_epoch']),
                producer_epoch=uuid_value(body['producer_epoch']), kind=body['kind'],
                expected_revision=0 if body['expected_revision'] == '0' else counter(body['expected_revision']))
            return {'schema_version':1, 'epoch':str(epoch), 'revision':str(revision)}
        return response(bind)


class Observations(APIView):
    def post(self, request):
        def ingest():
            body = request.data
            exact(body, {'schema_version','controller_run','target_id','incarnation_id','producer_epoch',
                         'sequence','kind','observed_at','payload','status','reason'})
            epoch = UUID(read_authority()['epoch'])
            run = ControllerRun(epoch, uuid_value(body['controller_run']))
            changed = ProjectionWriter(run).observe(request.actor, target_id=uuid_value(body['target_id']),
                incarnation_id=uuid_value(body['incarnation_id']), producer_epoch=uuid_value(body['producer_epoch']),
                sequence=counter(body['sequence']), kind=body['kind'], observed_at=deadline(body['observed_at']),
                payload=body['payload'], status=body['status'], reason=body['reason'])
            return {'schema_version':1, 'epoch':str(epoch), 'recorded':changed}
        return response(ingest)
