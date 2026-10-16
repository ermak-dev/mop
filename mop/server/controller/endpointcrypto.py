"""Root adapter: сообщения используют существующий AEAD port, не новый key store."""
from mop.common.endpoint import SealedPayload
from mop.server.secrets.crypto import SecretContext, Envelope


class EndpointCipher:
    def __init__(self, provider, installation):
        self.provider, self.installation = provider, installation

    def _context(self, delivery_id, endpoint_id):
        return SecretContext(self.installation.encode(), delivery_id.bytes,
                             b'endpoint-message:'+endpoint_id.bytes, 1)

    def seal(self, delivery_id, endpoint_id, text):
        value = self.provider.encrypt(text, self._context(delivery_id, endpoint_id))
        return SealedPayload(value.to_bytes(), value.key_id, value.nonce)

    def open(self, delivery_id, endpoint_id, sealed):
        value = Envelope.from_bytes(sealed.data)
        if value.key_id != sealed.key_id or value.nonce != sealed.nonce:
            raise ValueError('private payload envelope binding changed')
        return self.provider.decrypt(value, self._context(delivery_id, endpoint_id))
