"""Composition root для Django backend и LDAP secret boundary."""
from django.utils import timezone
from mop.server.access.directory import Router, DirectoryUnavailable
from mop.server.access.ldap import LdapDirectory


def lockout_username(request, credentials):
    """Ключ логина для axes (#694): HMAC-дайджест, не логин в открытом виде.

    Блокировка ищет и пишет записи по одному и тому же значению, поэтому
    дайджест сохраняет поведение счётчика, не храня сам логин в БД.
    """
    from mop.server.access.services import _digest
    username = (credentials or {}).get('username')
    return _digest('account:' + username) if isinstance(username, str) else None


class AuthenticationUnavailable(RuntimeError):
    """Безопасная причина adapter; authenticate не превращает outage в bad password."""


class ControllerBackend(Router):
    def __init__(self):
        super().__init__(provider=LdapDirectory())

    def authenticate(self, request, username=None, password=None, *, session_login=True, **kwargs):
        # Учёт неудач и блокировку ведёт axes первым backend'ом (#694): сюда
        # заблокированный запрос не доходит, bind каталога не выполняется.
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 150 or len(password) > 4096:
            return None
        try:
            user = super().authenticate(request, username=username, password=password)
        except DirectoryUnavailable as error:
            raise AuthenticationUnavailable(str(error)) from None
        if user is not None and session_login and getattr(request, 'session', None) is not None:
            # Новая password authentication не возрождает старый session binding.
            request.session.flush()
            request.session['access_reauthenticated_at'] = timezone.now().timestamp()
        return user
