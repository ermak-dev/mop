"""Current human/session authority и узкий controller SecretStore consumer."""
from django.contrib.auth import get_user_model
from mop.server.access.context import PrincipalKind
from mop.server.access.services import current_actor, session_context, Denied
from mop.server.secrets.services import Refused
from .values import Credential


class HumanAuthority:
    def authorize(self, actor):
        current_actor(actor)
        if actor.kind != PrincipalKind.HUMAN or actor.grant_id is not None:
            raise Denied('proxy management requires a current human principal')
        # Никаких cached request.user permissions после await.
        user = get_user_model().objects.get(pk=actor.user_id)
        if not user.has_perm('gateway.manage_proxy'):
            raise Denied('proxy management permission required')
        return actor

    def session(self, session_key):
        return self.authorize(session_context(session_key))


class CredentialSource:
    """Без root composition нет discovery файлов, env, global current_key."""
    def get(self, actor):
        raise Refused('proxy management credential source is not bound')

    def current(self, actor, credential):
        return False


class ControllerSecretGate:
    """Выбранный root secret id, не параметр request и не wildcard consumer."""
    def __init__(self, secret_id, *, authority=None):
        self.secret_id = secret_id
        self.authority = authority or HumanAuthority()

    def resolve(self, actor):
        self.authority.authorize(actor)
        return self.secret_id


class SecretCredentialSource(CredentialSource):
    def __init__(self, store):
        self.store = store

    def get(self, actor):
        value = self.store.issue_proxy_management(actor)
        try:
            key = value.value.decode('ascii')
        except UnicodeDecodeError:
            raise Refused('proxy management credential is malformed') from None
        if not 1 <= len(key) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise Refused('proxy management credential is malformed')
        return Credential(value.version_id, key)

    def current(self, actor, credential):
        return self.store.proxy_management_current(actor, credential.version_id)
