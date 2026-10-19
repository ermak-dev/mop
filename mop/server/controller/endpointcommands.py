"""Credential-only private application commands через публичный Sessions lifecycle.

Wire задаёт сообщения и locators, не actor/holder/ciphertext/AAD. Crypto ждёт
вне SQL; окончательная проверка authority выполняется до возврата plaintext.
"""
from django.views.decorators.debug import sensitive_variables

from mop.server.sessions.endpoints import Endpoints, Unavailable
from mop.server.secrets.crypto import SecretCryptoError


class _PrivateProvider:
    """Sanitization только raw provider errors, не безопасных domain verdicts."""
    def __init__(self, provider):
        self.provider = provider

    @sensitive_variables()
    def _invoke(self, method, *args):
        try:
            return method(*args)
        except SecretCryptoError:
            raise
        except Exception:
            raise Unavailable('private delivery cryptography unavailable; mutation outcome unknown, do not replay') from None

    def encrypt(self, value, context):
        return self._invoke(self.provider.encrypt, value, context)

    def decrypt(self, envelope, context):
        return self._invoke(self.provider.decrypt, envelope, context)


class PrivateCommands:
    def __init__(self, application):
        # Этот constructor вызывается только credential composition. Rootless
        # metadata adapter не импортирует его и не создаёт provider/cipher.
        from .endpointcrypto import EndpointCipher
        self.application = application
        self.endpoints = Endpoints(authority=self.authority, grants=application.local_grants,
            cipher=EndpointCipher(_PrivateProvider(application.store.provider), application.snapshot['identity']))

    def authority(self, *, lock=False):
        from .endpointviews import authority
        value = authority(lock=lock)
        # Restore/installation locks уже взяты public root-ом. Проверка snapshot
        # внутри той же короткой секции не позволяет commit под старым root.
        self.application.fence()
        return value

    @sensitive_variables()
    def send(self, actor, *, sender_endpoint_id, sender_connection_nonce, **message):
        sender = None
        if sender_endpoint_id is not None:
            # SQL owner связывает current credential+nonce с conversation;
            # metadata read не является authority для send. Сам send заново
            # проверяет связь sender/recipient project после provider wait.
            sender = self.endpoints.trusted_context(actor, sender_endpoint_id,
                sender_connection_nonce)
        return self.endpoints.send(actor, **message, sender=sender)

    @sensitive_variables()
    def take(self, actor, *, endpoint_id, connection_nonce):
        context = self.endpoints.trusted_context(actor, endpoint_id, connection_nonce)
        frame = self.endpoints.take(context)
        if frame is not None:
            # take уже committed attempting до crypto: потерянный ответ не
            # переоткрывает payload. Публичный gate заново проверяет receiver,
            # sender, deadline, installation и lease после decrypt. Gateway
            # обязан вызвать свой before_dispatch ещё раз перед socket bytes.
            self.endpoints.before_dispatch(context, frame.id)
        return frame
