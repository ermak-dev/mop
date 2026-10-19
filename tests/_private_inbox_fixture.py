"""Только owned UID fixture: настоящий rootless ASGI, не native launcher."""
import os
from pathlib import Path


def credential(bootstrap, path):
    import asyncio
    import signal
    if os.geteuid() != 65533:
        raise AssertionError('protected fixture requires its separate service UID')
    os.environ['MOP_CONTROLLER_BOOTSTRAP'] = bootstrap
    from mop.server.controller.runtime import configure, Runtime, close_pools
    configure()
    from mop.server.controller.credentials import application, CredentialService
    from mop.server.secrets.crypto import XChaCha20Provider, Keyring
    app = application(provider=XChaCha20Provider(Keyring('fixture', {'fixture': b'k' * 32})))
    close_pools()
    async def run():
        runtime = Runtime(queue_limit=0)
        server = CredentialService(path, application=app, runtime=runtime,
            allowed_uids=(0, 65534), socket_group=os.getegid())
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stopped.set)
        try:
            await server.start()
            await stopped.wait()
        finally:
            await server.aclose()
            await runtime.aclose()
    asyncio.run(run())


def frontend(bootstrap, listener_fd, cert, key, forbidden):
    if os.geteuid() != 65534:
        raise AssertionError('private frontend fixture requires the separate UID')
    for path in forbidden:
        try:
            Path(path).read_bytes()
        except PermissionError:
            pass
        else:
            raise AssertionError('frontend read protected service custody')
    os.environ['MOP_CONTROLLER_BOOTSTRAP'] = bootstrap
    from mop.server.controller.runtime import configure
    configure()
    from django.conf import settings
    settings.ALLOWED_HOSTS = ['localhost', '127.0.0.1']
    from mop.server.controller.asgi import ControllerApplication
    import socket
    import uvicorn
    listener = socket.socket(fileno=listener_fd)
    app = ControllerApplication()
    uvicorn.Server(uvicorn.Config(app, lifespan='on', log_level='error', access_log=False,
        ssl_certfile=cert, ssl_keyfile=key, ws_max_size=401408, ws_per_message_deflate=False)).run(sockets=[listener])
