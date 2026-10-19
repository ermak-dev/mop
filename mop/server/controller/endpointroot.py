"""Lifecycle root приватной доставки; #628 связывает peer с Channels, #642 с CLI."""
from mop.server.sessions.delivery import DeliveryGateway
from .runtime import Runtime


class EndpointRuntime(Runtime):
    """Сначала quiescence delivery/clients/leases, затем остановка DB executor.

    Gateway cleanup использует ещё работающий bounded database worker. Его нельзя
    помещать в adapters обычного Runtime: там admission уже остановлен к моменту
    adapter cleanup. Не меняем lifecycle общего runtime ради одного транспорта.
    """
    def __init__(self, endpoints, **limits):
        super().__init__(**limits)
        self.delivery = DeliveryGateway(self, endpoints)

    async def aclose(self, timeout=10):
        await self.delivery.aclose()
        await super().aclose(timeout=timeout)


class PrivateDeliveryRoot:
    """Production transport использует Runtime ASGI, не отдельный global app."""
    def __init__(self, runtime):
        from .endpointbinding import ProtectedReceivers
        from .endpointviews import default_endpoints
        from .adminservices import credential_client
        self.endpoints = default_endpoints()
        self.protected = ProtectedReceivers(runtime, self.endpoints)
        self.delivery = DeliveryGateway(runtime, self.endpoints, take_delivery=self.protected.take_delivery)
        self.available = credential_client() is not None

    async def start(self):
        await self.delivery.start()

    def stop_accepting(self):
        self.protected.stopped = True
        self.delivery._stopped = True
        for receiver in self.protected.receivers.values():
            receiver.close()

    async def aclose(self):
        self.stop_accepting()
        # Все protected jobs ещё учитываются общим Runtime до actual completion.
        await self.protected.aclose()
        await self.delivery.aclose()


def create_endpoint_runtime(endpoints, **limits):
    runtime = EndpointRuntime(endpoints, **limits)
    return runtime, runtime.delivery
