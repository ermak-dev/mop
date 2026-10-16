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


def create_endpoint_runtime(endpoints, **limits):
    runtime = EndpointRuntime(endpoints, **limits)
    return runtime, runtime.delivery
