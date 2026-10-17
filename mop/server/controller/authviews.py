"""DRF/Django boundary: finite input, CSRF, стандартные credentials и no-store."""
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import ParseError, PermissionDenied, Throttled
from rest_framework.parsers import BaseParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.access import services as access


def catalog_api():
    return access.AuthorizedCatalog()


def login_session(request, user):
    """После password/LDAP I/O: стандартная session и binding коммитятся вместе."""
    with transaction.atomic():
        current = get_user_model().objects.select_for_update().get(pk=user.pk)
        if current.get_session_auth_hash() != user.get_session_auth_hash():
            raise access.Denied('session authentication changed')
        login(request, user)
        request.session['access_reauthenticated_at'] = timezone.now().timestamp()
        request.session.save()
        access.bind_session(user.pk, request.session.session_key)


from .authentication import actor_for_request

class FiniteSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not hasattr(data, 'keys') or set(data.keys()) - set(self.fields):
            raise serializers.ValidationError({'non_field_errors': ['unknown fields']})
        return super().to_internal_value(data)


class Credentials(FiniteSerializer):
    username = serializers.CharField(max_length=150, required=False)
    password = serializers.CharField(max_length=4096, required=False, write_only=True, trim_whitespace=False)


def _input(request):
    serializer = Credentials(data=request.data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


@sensitive_variables('password')
def _password_user(request, data, *, browser_login=False):
    if not data.get('username') or not data.get('password'):
        raise serializers.ValidationError('username and password required')
    # axes требует request (#694): блокировку по логину/адресу проверяет первый
    # backend до bind каталога; сессию сбрасывает только браузерный вход.
    raw = getattr(request, '_request', request)
    user = authenticate(request=raw, username=data['username'], password=data['password'],
        session_login=browser_login)
    if user is None:
        if getattr(raw, 'axes_locked_out', False):
            raise Throttled(wait=300, detail='authentication rate limit exceeded')
        raise PermissionDenied('authentication refused')
    return user


def _reauthenticated_session(request, data):
    if request.auth is not None:
        return
    SessionAuthentication().enforce_csrf(request)
    raw = request._request
    stamp = raw.session.get('access_reauthenticated_at', 0)
    if not isinstance(stamp, (float, int)) or timezone.now().timestamp() - stamp > 300:
        if not data.get('password'):
            raise PermissionDenied('password reauthentication required')
        user = _password_user(raw, {'username': request.user.username, 'password': data['password']})
        if user.pk != request.user.pk:
            raise PermissionDenied('credential identity changed')
        raw.session['access_reauthenticated_at'] = timezone.now().timestamp()
        raw.session.save()


@never_cache
@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({'schema_version': 1})


@never_cache
def csrf_failure(request, reason=''):
    return JsonResponse({'schema_version': 1, 'error': {'code': 'csrf_failed',
        'message': 'CSRF validation failed', 'retryable': False}}, status=403)


class PrivateView(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response


@method_decorator(sensitive_post_parameters('password'), name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
class Login(PrivateView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        user = _password_user(request._request, _input(request), browser_login=True)
        login_session(request._request, user)
        return Response({'schema_version': 1, 'user_id': user.pk})


@method_decorator(csrf_protect, name='dispatch')
class Logout(PrivateView):
    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        if request.data:
            raise serializers.ValidationError('logout accepts no fields')
        from django.db import transaction
        with transaction.atomic():
            access.note_session_logout(request.session.session_key)
            logout(request._request)
        return Response({'schema_version': 1})


@method_decorator(sensitive_post_parameters('password'), name='dispatch')
class IssueToken(PrivateView):
    authentication_classes = (SessionAuthentication,)
    permission_classes = (AllowAny,)

    @sensitive_variables('token')
    def post(self, request):
        data = _input(request)
        if request.user.is_authenticated:
            _reauthenticated_session(request, data)
            actor = actor_for_request(request)
            user_id = access.current_actor(actor).user_id
        else:
            if request.META.get('HTTP_ORIGIN') or request.META.get('HTTP_REFERER') or request.COOKIES:
                SessionAuthentication().enforce_csrf(request)
                raise PermissionDenied('browser token issuance requires login')
            user_id = _password_user(request._request, data).pk
        token = access.issue_token(user_id)
        return Response({'schema_version': 1, 'token': token})


class RotateToken(PrivateView):
    @sensitive_variables('token')
    def post(self, request):
        data = _input(request)
        if set(data) - {'password'}:
            raise serializers.ValidationError('only password reauthentication is accepted')
        _reauthenticated_session(request, data)
        token = access.rotate_token(actor_for_request(request))
        return Response({'schema_version': 1, 'token': token})


class RevokeToken(PrivateView):
    def post(self, request):
        data = _input(request)
        if set(data) - {'password'}:
            raise serializers.ValidationError('only password reauthentication is accepted')
        _reauthenticated_session(request, data)
        access.revoke_token(actor_for_request(request))
        return Response({'schema_version': 1})


class Identity(PrivateView):
    def get(self, request):
        actor = access.current_actor(request.actor, grants=getattr(self, 'grant_resolver', None))
        return Response({'schema_version': 1, 'user_id': actor.user_id,
            'kind': actor.kind.value,
            'human_owner_id': actor.human_owner_id, 'node_id': str(actor.node_id) if actor.node_id else None,
            'grant_id': str(actor.grant_id) if actor.grant_id else None})


def _project(row):
    return {'id': str(row.id), 'name': row.name, 'origin': row.origin,
            'revision': str(row.revision), 'puppet_limit': row.puppet_limit}


class ProjectJSONParser(BaseParser):
    media_type = 'application/json'

    def parse(self, stream, media_type=None, parser_context=None):
        from mop.server.operations.commands import decode_body, InvalidCommand, MAX_BODY_BYTES
        try:
            return decode_body(stream.read(MAX_BODY_BYTES + 1))
        except InvalidCommand as error:
            raise ParseError(str(error)) from None


class CatalogRevision(serializers.Field):
    def to_internal_value(self, value):
        if (type(value) is not str or not value.isascii() or not value.isdecimal()
                or value.startswith('0') or len(value) > 19 or int(value) > 9223372036854775807):
            raise serializers.ValidationError('expected_revision must be a positive decimal string')
        return int(value)


class ProjectLimit(serializers.IntegerField):
    def to_internal_value(self, value):
        if type(value) is not int:
            raise serializers.ValidationError('puppet_limit must be an integer or null')
        return super().to_internal_value(value)


class ProjectCreate(FiniteSerializer):
    origin = serializers.CharField(max_length=2048, trim_whitespace=False)
    puppet_limit = ProjectLimit(min_value=0, max_value=2147483647, allow_null=True, required=False, default=None)


class ProjectConfiguration(ProjectCreate):
    expected_revision = CatalogRevision()
    puppet_limit = ProjectLimit(min_value=0, max_value=2147483647, allow_null=True)


def _catalog_mutation(function, *, created=False):
    from mop.server.catalog.services import Conflict
    try:
        row = function()
    except Conflict as error:
        return Response({'schema_version': 1, 'error': {'code': 'catalog_conflict',
            'message': str(error), 'retryable': False}}, status=409)
    except ValueError as error:
        raise serializers.ValidationError(str(error)) from None
    response = Response({'schema_version': 1, **_project(row)}, status=201 if created else 200)
    if created:
        response['Location'] = '/api/v1/projects/' + str(row.id)
    return response


class Projects(PrivateView):
    parser_classes = (ProjectJSONParser,)

    def post(self, request):
        serializer = ProjectCreate(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        return _catalog_mutation(lambda: catalog_api().create_project(actor_for_request(request),
            values['origin'], limit=values['puppet_limit']), created=True)

    def get(self, request):
        actor = actor_for_request(request)
        rows = catalog_api().list_projects(actor)
        access.Access().authorize(actor, 'catalog.view_project', project_ids=tuple(row.id for row in rows))
        from .pagination import project_page
        page, following = project_page(actor, rows, request.query_params)
        # Фильтр применён до count и pagination; cursor не хранит authority.
        access.Access().authorize(actor, 'catalog.view_project', project_ids=tuple(row.id for row in page))
        return Response({'schema_version': 1, 'count': len(rows), 'next_cursor': following,
            'results': [_project(row) for row in page]})


class Project(PrivateView):
    parser_classes = (ProjectJSONParser,)

    def put(self, request, project_id):
        serializer = ProjectConfiguration(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        return _catalog_mutation(lambda: catalog_api().configure_project(actor_for_request(request), project_id,
            origin=values['origin'], limit=values['puppet_limit'], expected_revision=values['expected_revision']))

    def get(self, request, project_id):
        row = catalog_api().get_project(actor_for_request(request), project_id)
        return Response({'schema_version': 1, **_project(row)})
