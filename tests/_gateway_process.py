"""Отдельный spawn entrypoint; до configure никакого Django ORM import."""

def process_gateway(bootstrap, database, actor, version, port, start, results, key_value='two-process-fixture-key'):
    import asyncio
    import json
    import os
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory(prefix='gateway-child-') as directory:
        path = Path(directory) / 'bootstrap.json'
        path.write_text(json.dumps(bootstrap))
        path.chmod(0o600)
        os.environ['MOP_CONTROLLER_BOOTSTRAP'] = str(path)
        from mop.server.controller.runtime import configure, Runtime, close_pools
        configure()
        from django.conf import settings
        from django.db import connections
        settings.DATABASES['default']['NAME'] = database
        connections['default'].settings_dict['NAME'] = database
        from mop.server.gateway.service import Gateway
        from mop.server.gateway.transport import LoopbackHTTP
        from mop.server.gateway.values import Credential
        key = Credential(version, key_value)
        class Source:
            def get(self, actor):
                return key
            def current(self, actor, value):
                return value == key
        async def run():
            runtime = Runtime(blocking_limit=2, queue_limit=2)
            await runtime.start()
            try:
                gateway = Gateway(runtime=runtime, source=Source(), transport=LoopbackHTTP(port), wait_seconds=2)
                return (await gateway.request(actor, 'GET', '/v8/management/config', b'', {})).status
            finally:
                await runtime.aclose()
        try:
            start.wait(10)
            results.put(asyncio.run(run()))
        except Exception as exc:
            results.put(type(exc).__name__)
        finally:
            close_pools()
