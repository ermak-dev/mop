"""Composition root для Django backend и LDAP secret boundary."""
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from mop.server.access.directory import Router, DirectoryUnavailable
from mop.server.access.ldap import LdapDirectory
from mop.server.access.services import consume_attempt


class AuthenticationUnavailable(RuntimeError):
    """Безопасная причина adapter; authenticate не превращает outage в bad password."""


class ControllerBackend(Router):
    def __init__(self):
        super().__init__(provider=LdapDirectory())

    def authenticate(self, request, username=None, password=None, **kwargs):
        address = str(request.META.get('REMOTE_ADDR', 'local')) if request is not None else 'local'
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 150 or len(password) > 4096:
            return None
        if not consume_attempt(username, address):
            raise PermissionDenied('authentication rate limit exceeded')
        try:
            user = super().authenticate(request, username=username, password=password)
        except DirectoryUnavailable as error:
            raise AuthenticationUnavailable(str(error)) from None
        if user is not None and request is not None:
            # Новая password authentication не возрождает старый session binding.
            request.session.flush()
            request.session['access_reauthenticated_at'] = timezone.now().timestamp()
        return user
