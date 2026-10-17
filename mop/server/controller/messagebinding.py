"""Root-owned private message ports; ни ключа по умолчанию, ни fake executor."""
import hashlib
import hmac

from mop.server.access.delegation import RoleBinding
from mop.server.operations.commands import intent_digest
from mop.server.secrets.crypto import SecretContext, Envelope


class MessageCipher:
    def __init__(self, provider, installation, *, digest_key):
        if type(digest_key) is not bytes or len(digest_key) < 32:
            raise ValueError('protected message intent key required')
        self.provider, self.installation = provider, installation
        self._digest_key = digest_key

    def digest(self, command, holder_endpoint):
        intent = intent_digest(command, key=self._digest_key)
        return hmac.new(self._digest_key, b'operation-message-v1:'+intent.encode()+b':'+
            (str(holder_endpoint).encode() if holder_endpoint else b''), hashlib.sha256).hexdigest()

    def _context(self, operation_id, message_id, target_id):
        return SecretContext(self.installation.encode(), operation_id.bytes,
            b'operation-message:'+message_id.bytes+target_id.bytes, 1)

    def _require_provider(self):
        if self.provider is None:
            from mop.server.operations.service import Refused
            raise Refused('controller_unavailable', safe_reason='protected message cipher provider is unavailable')

    def seal(self, operation_id, message_id, target_id, text):
        self._require_provider()
        return self.provider.encrypt(text, self._context(operation_id, message_id, target_id)).to_bytes()

    def open(self, operation_id, message_id, target_id, data):
        self._require_provider()
        return self.provider.decrypt(Envelope.from_bytes(data), self._context(operation_id, message_id, target_id))


class MessageRecipients:
    def __init__(self, grants):
        self.grants = grants

    def authorize(self, target, *, dispatch):
        self.grants.authorize_recipient(RoleBinding(target.project_id, target.target_id, target.incarnation_id,
            target.role, target.bundle_digest, target.grant_id, target.owner_id), dispatch=dispatch)
        from .machinebinding import owner_identity
        owner_identity(target.owner_id)
