"""Root-only owned gate: настоящий rootless UID читает лишь адресный payload #669.

HYPOTHESIS: новый stdlib frontend не имеет finite private send/take.
RESULT: AttributeError дал red; PG17 gate выполнил все четыре UID случая green.
STATUS: FIXED — see #669 (fixture UDS, не production WS/native binding).
"""
import asyncio
import json
import os
from pathlib import Path
import sys

from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import override_settings

from _endpoint_credential_cases import EndpointCredentialFixture
from mop.server.controller import credentials as service
from mop.server.controller.runtime import Runtime


class EndpointCredentialUids(EndpointCredentialFixture):
    def test_private_reports_cross_real_uid_without_root_or_caller_authority(self):
        self.assertEqual(os.geteuid(), 0, 'the UID gate requires the owned root container')
        Path(self.directory.name).chmod(0o750)
        with override_settings(BOOTSTRAP={**settings.BOOTSTRAP, 'role': 'credential'}):
            app = service.application(provider=self.provider)
        code = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from uuid import UUID
from datetime import datetime
from mop.common.credential_client import CredentialClient
from mop.common.credentials import Credential, CredentialError
p = json.loads(sys.stdin.read())
assert os.geteuid() == 65534
for path in p['forbidden']:
    try:
        open(path, 'rb')
    except PermissionError:
        pass
    else:
        raise AssertionError('frontend could read service custody')
client = CredentialClient(p['path'], server_uid=0)
args = p['args']
for key in ('expected_epoch', 'message_id', 'operation_request_id', 'sender_endpoint_id', 'sender_connection_nonce'):
    args[key] = UUID(args[key])
args['not_after'] = datetime.fromisoformat(args['not_after'])
sender, receiver = Credential('token', p['sender']), Credential('token', p['receiver'])
sent = client.send_private_report(sender, **args)
try:
    client.take_private_delivery(sender, endpoint_id=UUID(p['endpoint']), connection_nonce=UUID(p['nonce']))
except CredentialError:
    pass
else:
    raise AssertionError('OS peer substituted receiver authority')
frame = client.take_private_delivery(receiver, endpoint_id=UUID(p['endpoint']), connection_nonce=UUID(p['nonce']))
assert frame.id == sent.id and frame.text == args['text']
assert frame.text not in repr(frame) and sender.value not in repr(sender)
assert client.take_private_delivery(receiver, endpoint_id=UUID(p['endpoint']), connection_nonce=UUID(p['nonce'])) is None
assert not any(name.startswith(('mop.server', 'django', 'nacl')) for name in sys.modules)
print('private-addressed-only')
'''
        async def run():
            runtime = Runtime(queue_limit=0)
            server = service.CredentialService(self.path, application=app, runtime=runtime,
                allowed_uids=(65534,), socket_group=os.getegid())
            await server.start()
            process = None
            try:
                process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                    str(Path(__file__).resolve().parents[1]), user=65534, group=os.getegid(), extra_groups=(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={k: v for k, v in os.environ.items() if not k.startswith(('MOP_', 'DJANGO_'))})
                payload = dict(path=self.path, sender=self.sender_auth.value, receiver=self.auth.value,
                    args=self.send_args(), endpoint=str(self.endpoint.id), nonce=str(self.context.nonce),
                    forbidden=[settings.BOOTSTRAP['root_key'], settings.BOOTSTRAP['django_key']])
                async with asyncio.timeout(10):
                    output, error = await process.communicate(json.dumps(payload).encode())
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertEqual(output.strip(), b'private-addressed-only')
            finally:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
                await server.aclose()
                await runtime.aclose()
        async_to_sync(run)()
