"""Узкий root-only gate реальных перекрёстных UID (#674).

Ровно два случая #669 требуют исполнять клиент под другим OS UID (65534) --
настоящий SO_PEERCRED, не мок. В общем непривилегированном прогоне они
жили пропуском, и CI молча считал их зелёными. Здесь они перенесены без
изменения утверждений и исполняются только точечными метками через
tests/_credential_uid_fixture.py в owned Docker fixture под root.

Модуль -- только определение случаев: классы наследуют setUp общих
модулей, но прогоняются ровно два перенесённых метода, не весь класс.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile

from asgiref.sync import async_to_sync
from django.test import override_settings

import _credential_cases as socket_cases
import _credential_binding_cases as binding_cases


class CredentialSocketUids(socket_cases.CredentialSocketCases):
    """Root-only подмножество CredentialSocketCases: один перенесённый случай."""

    def test_distinct_frontend_uid_uses_only_group_protected_socket(self):
        if os.geteuid() != 0:
            raise AssertionError('cross-UID gate requires the owned root container')
        import sys
        from mop.server.controller.runtime import Runtime
        shared = tempfile.TemporaryDirectory(prefix='mop-credential-crossuid-', dir='/tmp')
        self.addCleanup(shared.cleanup)
        self.path = str(Path(shared.name) / 'credential.sock')
        os.chmod(shared.name, 0o750)
        code = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from mop.common.credential_client import CredentialClient
from mop.common.credentials import Credential
from uuid import UUID
payload = json.loads(sys.stdin.read())
assert os.geteuid() == 65534
client = CredentialClient(payload['path'], server_uid=0)
item = client.create(Credential('token', payload['token']), project_id=UUID(payload['project']),
    name='DISTINCT_UID', kind='variable', purpose='project', value=b'uid-fixture')
assert not any(name.startswith(('django', 'mop.server', 'nacl')) for name in sys.modules)
print(item.revision)
'''
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        async def run():
            runtime = Runtime(queue_limit=0)
            server = self.service.CredentialService(self.path, application=app, runtime=runtime,
                allowed_uids=(65534,), socket_group=os.getegid())
            await server.start()
            try:
                process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                    str(Path(__file__).resolve().parents[1]), user=65534, group=os.getegid(), extra_groups=(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={key: value for key, value in os.environ.items() if not key.startswith(('MOP_', 'DJANGO_'))})
                payload = json.dumps({'path': self.path, 'token': self.token, 'project': str(self.project.id)}).encode()
                async with asyncio.timeout(8):
                    output, error = await process.communicate(payload)
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertEqual(output.strip(), b'1')
                client = self.Client(self.path, server_uid=0)
                with self.assertRaises(self.Error):
                    await asyncio.to_thread(self.create, client)
            finally:
                await server.aclose()
                await runtime.aclose()
        async_to_sync(run)()


class AddressedCredentialUids(binding_cases.AddressedCredentialCases):
    """Root-only подмножество AddressedCredentialCases: один перенесённый случай."""

    def test_addressed_delivery_uses_actual_frontend_uid_without_rights_inference(self):
        if os.geteuid() != 0:
            raise AssertionError('cross-UID gate requires the owned root container')
        import sys
        from mop.server.controller.runtime import Runtime
        self.delivery()
        shared = tempfile.TemporaryDirectory(prefix='mop-addressed-crossuid-', dir='/tmp')
        self.addCleanup(shared.cleanup)
        self.path = str(Path(shared.name) / 'credential.sock')
        os.chmod(shared.name, 0o750)
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        code = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from mop.common.credential_client import CredentialClient
from mop.common.credentials import Credential, CredentialError
from uuid import UUID
p = json.loads(sys.stdin.read())
assert os.geteuid() == 65534
client = CredentialClient(p['path'], server_uid=0)
rows = client.issue_addressed(Credential('token', p['machine']), target_id=UUID(p['target']))
assert len(rows) == 1 and rows[0].value == b'exact-addressed-value'
try:
    client.issue_addressed(Credential('token', p['human']), target_id=UUID(p['target']))
except CredentialError:
    pass
else:
    raise AssertionError('peer UID gave human node authority')
assert not any(n.startswith(('django', 'mop.server', 'nacl')) for n in sys.modules)
print('addressed-only')
'''
        async def run():
            runtime = Runtime(queue_limit=0)
            server = self.service.CredentialService(self.path, application=app, runtime=runtime,
                allowed_uids=(65534,), socket_group=os.getegid())
            await server.start()
            try:
                process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                    str(Path(__file__).resolve().parents[1]), user=65534, group=os.getegid(), extra_groups=(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={k: v for k, v in os.environ.items() if not k.startswith(('MOP_', 'DJANGO_'))})
                async with asyncio.timeout(10):
                    output, error = await process.communicate(json.dumps({'path': self.path,
                        'machine': self.machine_auth.value, 'human': self.token,
                        'target': str(self.assignment.id)}).encode())
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertEqual(output.strip(), b'addressed-only')
            finally:
                await server.aclose()
                await runtime.aclose()
        async_to_sync(run)()


# Точка входа -- tests/_credential_uid_fixture.py: gate всегда идёт с
# проверкой реальной пары UID под root, а не запуском модуля наугад.
