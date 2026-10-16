"""Материализованная WS authority: никаких LazyUser или ORM objects через await."""
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit
from django.conf import settings
from mop.server.access import services as access


class AuthenticationRequired(PermissionError):
    """Handshake не предъявил допустимый credential."""


def credentials(scope):
    headers={}
    for name,value in scope.get('headers',()):
        key=name.lower()
        if key in headers:
            raise AuthenticationRequired('duplicate authentication headers')
        headers[key]=value.decode('latin1')
    token=headers.get(b'authorization')
    cookie=SimpleCookie()
    try:
        cookie.load(headers.get(b'cookie',''))
    except CookieError:
        raise AuthenticationRequired('invalid session cookie') from None
    session=cookie.get(settings.SESSION_COOKIE_NAME)
    if token:
        if session or not token.startswith('Token ') or len(token)!=46:
            raise AuthenticationRequired('one standard credential is required')
        return ('token',token[6:])
    if session:
        return ('session',session.value)
    raise AuthenticationRequired('authentication required')


def origin_allowed(scope, credential):
    headers=dict(scope.get('headers',()))
    value=headers.get(b'origin')
    if value is None:
        return credential[0]=='token'
    try:
        origin=urlsplit(value.decode('ascii'))
        host=headers.get(b'host')
        if host is None:
            name,port=scope['server']
            host=(name+('' if port==443 else ':'+str(port))).encode('ascii')
        expected=urlsplit('https://'+host.decode('ascii'))
        return (scope.get('scheme')=='wss' and origin.scheme=='https' and not origin.username and
            not origin.password and not origin.path and not origin.query and not origin.fragment and
            origin.hostname==expected.hostname and (origin.port or 443)==(expected.port or 443) and
            origin.hostname in settings.ALLOWED_HOSTS)
    except (ValueError,UnicodeError,KeyError):
        return False


class Authority:
    def __init__(self, *, grants=None):
        self.grants=grants

    def authenticate(self, credential):
        kind,value=credential
        if kind=='session':
            return access.session_context(value)
        return access.token_context(value,grants=self.grants)

    def authorize(self, actor, action, project_ids):
        projects=tuple(project_ids)
        if not projects or len(projects)>16:
            raise access.NotVisible('resource not found')
        access.Access(grants=self.grants).authorize(actor,action,project_ids=projects)
        return actor

    def current(self, credential, actor, project_ids):
        current=self.authenticate(credential)
        if current!=actor:
            raise access.Denied('credential generation changed')
        return self.authorize(current,'catalog.view_pool',project_ids)
