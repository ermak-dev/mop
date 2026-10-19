"""Rootless metadata/lease root плюс два конечных protected application ports."""
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from mop.common import credentials as p
from mop.server.access.services import Denied, NotVisible
from mop.server.sessions.endpoints import Endpoints, Unavailable, Conflict, Capacity


class ProtectedEndpoints(Endpoints):
    def take(self, context):
        # Context-only gateway ещё не связан с исходным credential. Не claim-им
        # queued payload перед Unbound cipher: это потеряло бы попытку доставки.
        raise Unavailable('private delivery requires a current connection credential binding')

    @sensitive_variables()
    def _protected(self, method, credential, **args):
        if transaction.get_connection().in_atomic_block:
            raise RuntimeError('protected private delivery I/O must run outside SQL transaction')
        from .adminservices import credential_client
        client = credential_client()
        if client is None:
            raise Unavailable('private delivery credential service is not bound')
        try:
            return getattr(client, method)(credential, **args)
        except p.CredentialInput:
            raise
        except p.PrivateDeliveryError as error:
            kind = {'conflict': Conflict, 'capacity': Capacity, 'not_found': NotVisible,
                    'invalid': ValueError}[error.code]
            raise kind(str(error)) from None
        except p.CredentialRefused as error:
            raise Denied(str(error)) from None
        except p.CredentialError as error:
            raise Unavailable(str(error)) from None

    @sensitive_variables()
    def send_private_report(self, credential, **message):
        return self._protected('send_private_report', credential, **message)

    @sensitive_variables()
    def take_private_delivery(self, credential, *, endpoint_id, connection_nonce):
        # Native/WS binding передаёт исходный credential отдельно от lease.
        # TrustedEndpointContext сам по себе не даёт protected plaintext port.
        return self._protected('take_private_delivery', credential,
            endpoint_id=endpoint_id, connection_nonce=connection_nonce)
