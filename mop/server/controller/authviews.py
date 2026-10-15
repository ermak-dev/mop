"""DRF/Django boundary: finite input, CSRF, стандартные credentials и no-store."""
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import PermissionDenied, NotAuthenticated, Throttled
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView
from mop.server.access import services as access
from mop.server.catalog.services import Catalog


def catalog_api():
    return Catalog(authorizer=access.Access())


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
    address = str(request.META.get('REMOTE_ADDR', 'local')) if request is not None else 'local'
    user = authenticate(request=request if browser_login else None, username=data['username'],
        password=data['password'], remote_address=address)
    if user is None:
        if access.attempts_blocked(data['username'], address):
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
        login(request._request, user)
        request.session['access_reauthenticated_at'] = timezone.now().timestamp()
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
        actor = access.current_actor(actor_for_request(request))
        return Response({'schema_version': 1, 'user_id': actor.user_id,
            'principal_id': str(actor.principal_id), 'kind': actor.kind.value,
            'human_owner_id': actor.human_owner_id, 'node_id': str(actor.node_id) if actor.node_id else None})


def _project(row):
    return {'id': str(row.id), 'name': row.name, 'origin': row.origin,
            'revision': str(row.revision), 'puppet_limit': row.puppet_limit}


class Projects(PrivateView):
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
    def get(self, request, project_id):
        row = catalog_api().get_project(actor_for_request(request), project_id)
        return Response({'schema_version': 1, **_project(row)})
