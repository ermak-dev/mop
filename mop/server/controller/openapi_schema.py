"""AutoSchema и wire-сериализаторы публичного OpenAPI #696.

PublicAutoSchema публикует только аннотированный публичный контракт:
машинные и приватные корни исключаются, каждому публичному operation
дописывается общий конверт отказа (Error), сериализаторы берутся из
явной карты аннотаций по классу view -- генерация не угадывает и не
зависит от внутренних имён #690.

SchemaView отдаёт тот же документ: authenticated staff-only, никогда
anonymous; сам маршрут исключён из генерации.
"""
from rest_framework import serializers
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema_field

from .authentication import CurrentActorRequired


def free(shape, **kwargs):
    """Свободное значение с фиксированной формой в схеме (#696).

    Каждый вызов -- новый подкласс: декорировать общий serializers.Field
    нельзя, он общий для всех полей и форма «протечёт» в чужие поля.
    """
    return extend_schema_field(shape)(type('Free', (serializers.Field,), {}))(**kwargs)


def choices(*values):
    return serializers.ChoiceField(choices=[(value, value) for value in values])


class Error(serializers.Serializer):
    """Общий конверт отказа: глобальная форма, не предметный сериализатор."""
    schema_version = free({'type': 'integer', 'enum': [1]}, required=True)
    error = free({'type': 'object',
                   'properties': {'code': {'type': 'string'},
                                  'message': {'type': 'string'},
                                  'retryable': {'type': 'boolean'}},
                   'required': ['code', 'message', 'retryable']}, required=True)


class Operation(serializers.Serializer):
    id = serializers.CharField()
    request_id = serializers.CharField()
    epoch = serializers.CharField()
    state = choices('accepted', 'running', 'reconciling', 'succeeded',
                    'refused', 'failed', 'partial', 'unknown')
    revision = serializers.CharField()
    code = serializers.CharField(allow_null=True, required=False)
    reason = serializers.CharField(allow_null=True, required=False)
    target = free({'type': 'object', 'additionalProperties': True})
    steps = free({'type': 'array', 'items': {'type': 'object', 'additionalProperties': True}},
                 required=False)
    resources_held = serializers.BooleanField()
    evidence = free({'nullable': True, 'type': 'object', 'additionalProperties': True})


class OperationReply(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    epoch = serializers.CharField()
    operation = Operation()


class NodeCommand(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    epoch = serializers.CharField()
    request_id = serializers.CharField()
    expected_revision = serializers.CharField()
    not_after = serializers.CharField()
    params = free({'type': 'object', 'properties': {}, 'additionalProperties': False})


class RestartCommand(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    epoch = serializers.CharField()
    request_id = serializers.CharField()
    incarnation = serializers.CharField()
    expected_revision = serializers.CharField()
    not_after = serializers.CharField()
    params = free({'type': 'object', 'properties': {'fresh': {'type': 'boolean'}},
                   'additionalProperties': False})


class BuildCommand(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    epoch = serializers.CharField()
    request_id = serializers.CharField()
    expected_revision = serializers.CharField()
    not_after = serializers.CharField()
    policy_id = serializers.CharField()
    mode = choices('missing', 'update', 'rebuild')
    target_nodes = serializers.ListField(child=serializers.CharField(),
                                         min_length=1, max_length=64)
    force_safety = serializers.BooleanField()


class Health(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    state = serializers.CharField()
    identity = serializers.CharField()
    epoch = serializers.CharField()
    effects_enabled = serializers.BooleanField()


class Credentials(serializers.Serializer):
    username = serializers.CharField(max_length=150, required=False)
    password = serializers.CharField(max_length=4096, required=False)


class TokenReply(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    token = serializers.CharField()


class VersionOnly(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)


class UserIdReply(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    user_id = serializers.IntegerField()


class IdentityReply(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    user_id = serializers.IntegerField()
    kind = choices('human', 'service', 'agent')
    human_owner_id = serializers.IntegerField(allow_null=True)
    node_id = serializers.CharField(allow_null=True)
    grant_id = serializers.CharField(allow_null=True)


class ProjectFields(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    origin = serializers.CharField()
    revision = serializers.CharField()
    puppet_limit = serializers.IntegerField(allow_null=True)


class ProjectBody(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    id = serializers.CharField()
    name = serializers.CharField()
    origin = serializers.CharField()
    revision = serializers.CharField()
    puppet_limit = serializers.IntegerField(allow_null=True)


class ProjectCreate(serializers.Serializer):
    origin = serializers.CharField(max_length=2048)
    puppet_limit = serializers.IntegerField(min_value=0, max_value=2147483647,
                                            allow_null=True, required=False)


class ProjectConfiguration(serializers.Serializer):
    origin = serializers.CharField(max_length=2048)
    puppet_limit = serializers.IntegerField(min_value=0, max_value=2147483647, allow_null=True)
    expected_revision = serializers.CharField()


class ProjectPage(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    count = serializers.IntegerField()
    next_cursor = serializers.CharField(allow_null=True)
    results = ProjectFields(many=True)


class PuppetState(serializers.Serializer):
    kind = serializers.CharField()
    detail = serializers.CharField(allow_null=True)
    branch = serializers.CharField(allow_null=True)


class Puppet(serializers.Serializer):
    kind = serializers.CharField()
    id = serializers.CharField()
    incarnation = serializers.CharField(allow_null=True)
    revision = serializers.CharField()
    deleted = serializers.BooleanField()
    name = serializers.CharField(allow_null=True)
    aliases = serializers.ListField(child=serializers.CharField())
    project_id = serializers.CharField()
    project_name = serializers.CharField()
    node_id = serializers.CharField()
    node_name = serializers.CharField()
    desired = free({'type': 'object', 'additionalProperties': True})
    applied = free({'type': 'object', 'additionalProperties': True})
    observed = free({'type': 'object', 'additionalProperties': True})
    state = PuppetState()
    node = free({'type': 'object', 'additionalProperties': True})
    usage = free({'type': 'object', 'additionalProperties': True})


class Snapshot(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    epoch = serializers.CharField()
    snapshot_id = serializers.CharField()
    cursor = serializers.CharField()
    next_cursor = serializers.CharField(allow_null=True)
    results = Puppet(many=True)
    coverage = free({'type': 'object', 'additionalProperties': {'type': 'boolean'}})
    handshake_complete = serializers.BooleanField()


class PuppetDetailReply(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    epoch = serializers.CharField()
    puppet = Puppet()


class EventEntity(serializers.Serializer):
    kind = serializers.CharField()
    id = serializers.CharField()
    incarnation = serializers.CharField(allow_null=True)
    revision = serializers.CharField()
    deleted = serializers.BooleanField()


class ProjectionEvent(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    event_id = serializers.CharField()
    epoch = serializers.CharField()
    project_id = serializers.CharField()
    type = choices('puppet.changed', 'puppet.deleted')
    entity = EventEntity()


class Events(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    epoch = serializers.CharField()
    cursor = serializers.CharField()
    results = ProjectionEvent(many=True)
    coverage = free({'type': 'array', 'items': {'type': 'string'}})


class UsageSource(serializers.Serializer):
    id = serializers.CharField()
    profile = serializers.CharField()
    installed_version = serializers.CharField()
    source_complete = serializers.BooleanField()
    unresolved_reads = serializers.IntegerField()
    gap_reads = serializers.IntegerField()
    last_read_at = serializers.CharField(allow_null=True)
    status = serializers.CharField()


class UsageItem(serializers.Serializer):
    id = serializers.CharField()
    source_id = serializers.CharField()
    date = serializers.CharField()
    key_id = serializers.CharField(allow_null=True)
    provider = serializers.CharField()
    model = serializers.CharField()
    alias = serializers.CharField(allow_null=True)
    requests = serializers.IntegerField()
    failed_requests = serializers.IntegerField()
    partial_events = serializers.IntegerField()
    counters = free({'type': 'object', 'additionalProperties': {'type': 'object', 'properties': {
        'value': {'type': 'string', 'nullable': True},
        'known_events': {'type': 'integer'},
        'unknown_events': {'type': 'integer'}}}})


class Usage(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1)
    source = serializers.CharField()
    source_complete = serializers.BooleanField()
    period = free({'type': 'object', 'properties': {
        'start': {'type': 'string'}, 'end': {'type': 'string'},
        'timezone': {'type': 'string'}, 'end_exclusive': {'type': 'boolean'}}})
    attribution = free({'type': 'object', 'additionalProperties': True})
    sources = UsageSource(many=True)
    items = UsageItem(many=True)
    next = serializers.CharField(allow_null=True)


# ── карта аннотаций: класс view -> {метод: контракт} ─────────────────────

def _param(name, required=False, description='', pattern=None):
    return OpenApiParameter(name, OpenApiTypes.STR, OpenApiParameter.QUERY, required=required,
                            description=description, pattern=pattern)


def parameters():
    """{класс view: query-параметры реального адаптера} (#696)."""
    from . import operationviews, projectionviews, usageviews
    return {
        usageviews.Usage: (
            _param('schema_version', description='wire version, always 1'),
            _param('start', description='inclusive ISO date of the period'),
            _param('end', description='exclusive ISO date of the period'),
            _param('limit', description='page size within 1..100'),
            _param('after', description='page cursor of the previous response')),
        operationviews.OperationStatus: (
            _param('epoch', description='bounds a stale read to an installation epoch'),),
        operationviews.RequestLookup: (
            _param('request_id', required=True, description='idempotency key of the command'),
            _param('epoch', required=True, description='installation epoch of the command')),
        projectionviews.PoolSnapshot: (
            _param('project_id', description='narrow the snapshot to one project'),
            _param('page_size', description='page size within 1..500'),
            _param('cursor', description='page cursor of the previous response')),
        projectionviews.ProjectionEvents: (
            _param('cursor', required=True, description='event cursor of the previous response'),
            _param('project_id', description='narrow the events to one project'),
            _param('page_size', description='event limit within 1..500')),
    }


def annotations():
    """{класс view: {метод: (request, response, status, описание)}}.

    Ключ -- класс view, не маршрут: аннотация переживает перестановку
    urlpatterns и держится рядом с реальными адаптерами. Вызывается
    лениво: модуль urls импортируется только при генерации.
    """
    from . import authviews, buildviews, operationviews, projectionviews, usageviews, views
    return {
        views.Health: {'get': (None, Health, 200,
            'Liveness and installation identity of the controller.')},
        usageviews.Usage: {'get': (None, Usage, 200,
            'Materialized proxy usage aggregates for a bounded ISO date period.')},
        authviews.Login: {'post': (Credentials, UserIdReply, 200,
            'Password login: standard session plus access binding commit together.')},
        authviews.Logout: {'post': (None, VersionOnly, 200,
            'Logout: notes the session and flushes it; accepts no fields.')},
        authviews.IssueToken: {'post': (Credentials, TokenReply, 200,
            'Issues an API token for a session or an explicit password pair.')},
        authviews.RotateToken: {'post': (Credentials, TokenReply, 200,
            'Rotates the current token after password reauthentication.')},
        authviews.RevokeToken: {'post': (Credentials, VersionOnly, 200,
            'Revokes the current token after password reauthentication.')},
        authviews.Identity: {'get': (None, IdentityReply, 200,
            'Current actor identity: user, kind and delegation owner.')},
        authviews.Projects: {
            'get': (None, ProjectPage, 200,
                    'Visible projects as a cursor page; the cursor is not a permission.'),
            'post': (ProjectCreate, ProjectBody, 201,
                     'Creates a project from its origin; 201 with a Location header.')},
        authviews.Project: {
            'get': (None, ProjectBody, 200, 'One visible project.'),
            'put': (ProjectConfiguration, ProjectBody, 200,
                    'Reconfigures a project; expected_revision is a compare-and-swap guard.')},
        buildviews.BuildProject: {'post': (BuildCommand, OperationReply, 202,
            'Submits a finite pinned build command; accepted returns 202 with a Location header.')},
        operationviews.DrainNode: {'post': (NodeCommand, OperationReply, 202,
            'Submits a node drain command; eligible is fixed by the route.')},
        operationviews.EnableNode: {'post': (NodeCommand, OperationReply, 202,
            'Submits a node enable command; eligible is fixed by the route.')},
        operationviews.RestartPuppet: {'post': (RestartCommand, OperationReply, 202,
            'Submits a puppet restart command; fresh requires its own credential binding.')},
        operationviews.OperationStatus: {'get': (None, OperationReply, 200,
            'Durable operation status; optional epoch bounds a stale read.')},
        operationviews.RequestLookup: {'get': (None, OperationReply, 200,
            'Idempotent operation lookup by request_id and epoch.')},
        projectionviews.PoolSnapshot: {'get': (None, Snapshot, 200,
            'Scoped materialized pool snapshot as a cursor page.')},
        projectionviews.PuppetDetail: {'get': (None, PuppetDetailReply, 200,
            'One materialized puppet with observations; accepts no query fields.')},
        projectionviews.ProjectionEvents: {'get': (None, Events, 200,
            'Durable invalidations after a cursor; the event cursor is required.')},
    }


class PublicAutoSchema(AutoSchema):
    """Публичное подмножество: исключение машинных корней и конверт Error."""

    def is_excluded(self):
        from .openapi import MACHINE_ROOTS
        return any(self.path.startswith('/' + root) for root in MACHINE_ROOTS) \
            or super().is_excluded()

    def _entry(self):
        return annotations().get(type(self.view), {}).get(self.method.lower())

    def get_description(self):
        entry = self._entry()
        if entry:
            return entry[3]
        return super().get_description()

    def get_request_serializer(self):
        entry = self._entry()
        if entry is None:
            return super().get_request_serializer()
        return entry[0]() if entry[0] else None

    def map_parsers(self):
        # Команды и credentials идут только как JSON: парсер формы в теле
        # провода не существует (#696).
        return ['application/json']

    def get_response_serializers(self):
        entry = self._entry()
        if entry is None:
            return super().get_response_serializers()
        from .openapi import ERROR_STATUSES
        result = {str(entry[2]): entry[1]}
        result.update({status: Error for status in ERROR_STATUSES})
        return result

    def get_override_parameters(self):
        found = parameters().get(type(self.view))
        return list(found) if found else super().get_override_parameters()

    def get_operation_id(self):
        # Явные имена вместо числовых суффиксов _2 у list/detail коллизий:
        # маршрут с path-параметром получает _detail, команда -- _submit;
        # имя не зависит от порядка маршрутов в urls.py.
        method = self.method.lower()
        suffix = {'get': 'retrieve', 'post': 'create', 'put': 'update'}.get(method, method)
        segments = [part for part in self.path.strip('/').split('/') if part]
        if len(segments) > 1 and segments[-2] == 'commands':
            suffix = 'submit'
        elif segments and segments[-1].startswith('{'):
            suffix = 'detail' if method == 'get' else suffix + '_detail'
        base = '_'.join(part.strip('{}') for part in segments)
        return '_'.join(part for part in (base, suffix) if part)

    def get_tags(self):
        segments = [part for part in self.path.split('/') if part]
        return ['-'.join(segments[:3])] if segments[:2] == ['api', 'v1'] else super().get_tags()


class StaffRequired(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)


class SchemaView(APIView):
    """Отдаёт публичный документ; authenticated staff-only (#696)."""
    permission_classes = (CurrentActorRequired, StaffRequired)

    def get(self, request):
        from .openapi import schema
        response = Response(schema())
        response['Cache-Control'] = 'no-store'
        return response
