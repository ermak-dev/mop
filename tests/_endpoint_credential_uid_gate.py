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
    # SPEC: настоящий frontend ASGI UID != protected service UID; verified
    # HTTPS/WSS и default CredentialClient, не mock allowlist/provider cipher.
    # Контролируемый WS peer не доказывает native application acceptance.
    # RESULT: default root прошёл verified TLS/WSS между UID 65534/65533;
    # frontend реально не читает custody, существующий UID gate сохранён.
    # STATUS: FIXED — see #763.
    def test_private_wss_default_root_crosses_actual_frontend_uid(self):
        import socket, ssl, subprocess
        from django.db import connection
        self.assertEqual(os.geteuid(), 0, 'the UID gate requires the owned root container')
        self.metadata.disconnect(self.context)
        parent = Path(self.directory.name)
        parent.chmod(0o750)
        frontend = parent/'frontend'
        frontend.mkdir(mode=0o700)
        os.chown(frontend,65534,os.getegid())
        cert,key = frontend/'tls.crt',frontend/'tls.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
            '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        django_key=frontend/'django.key'
        django_key.write_bytes(Path(settings.BOOTSTRAP['django_key']).read_bytes())
        custodian = parent/'custodian'
        custodian.mkdir(mode=0o750)
        os.chown(custodian,65533,os.getegid())
        keys = custodian/'keys'
        keys.mkdir(mode=0o700)
        os.chown(keys,65533,os.getegid())
        service_key = keys/'root.key'
        service_key.write_bytes(Path(settings.BOOTSTRAP['root_key']).read_bytes())
        service_key.chmod(0o600)
        os.chown(service_key,65533,os.getegid())
        self.path = str(custodian/'service.sock')
        config={**settings.BOOTSTRAP,'role':'web','django_key':str(django_key),
            'credential_service':dict(path=self.path,server_uid=65533)}
        config.pop('root_key',None)
        from uuid import uuid4
        from psycopg import sql
        roles={name:'private_ws_'+name+'_'+uuid4().hex for name in ('web','credential')}
        password='owned-fixture-'+uuid4().hex
        with connection.cursor() as cursor:
            for role in roles.values():
                cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s').format(sql.Identifier(role)),[password])
                cursor.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
                cursor.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(role)))
                cursor.execute(sql.SQL('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(sql.Identifier(role)))
        def drop_roles():
            with connection.cursor() as cursor:
                for role in roles.values():
                    cursor.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                    cursor.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        self.addCleanup(drop_roles)
        config['database']={**config['database'],'NAME':connection.settings_dict['NAME'],
            'USER':roles['web'],'PASSWORD':password}
        bootstrap=frontend/'bootstrap.json'
        bootstrap.write_text(json.dumps(config))
        for path in (cert,key,django_key,bootstrap):
            path.chmod(0o600)
            os.chown(path,65534,os.getegid())
        service_bootstrap=keys/'bootstrap.json'
        service_config={**settings.BOOTSTRAP,'role':'credential','root_key':str(service_key),
            'database':{**config['database'],'USER':roles['credential']}}
        service_django=keys/'django.key'
        service_django.write_bytes(Path(settings.BOOTSTRAP['django_key']).read_bytes())
        service_django.chmod(0o600)
        os.chown(service_django,65533,os.getegid())
        service_config['django_key']=str(service_django)
        service_bootstrap.write_text(json.dumps(service_config))
        service_bootstrap.chmod(0o600)
        os.chown(service_bootstrap,65533,os.getegid())
        code='''
import json, sys
sys.path[:0] = [sys.argv[1], sys.argv[1]+'/tests']
import _private_inbox_fixture as fixture
p=json.loads(sys.argv[3])
getattr(fixture,sys.argv[2])(**p)
'''
        async def run():
            import aiohttp
            from mop.common.credential_client import CredentialClient
            from mop.server.sessions import endpoint_models as m
            runtime=Runtime(queue_limit=0)
            daemon=None
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16)
            port=listener.getsockname()[1]
            args=dict(bootstrap=str(bootstrap),listener_fd=listener.fileno(),cert=str(cert),key=str(key),
                forbidden=[str(service_key),settings.BOOTSTRAP['root_key'],settings.BOOTSTRAP['django_key']])
            process=None
            try:
                daemon=await asyncio.create_subprocess_exec(sys.executable,'-I','-c',code,
                    str(Path(__file__).resolve().parents[1]),'credential',
                    json.dumps(dict(bootstrap=str(service_bootstrap),path=self.path)),
                    user=65533,group=os.getegid(),extra_groups=(),
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                    env={k:v for k,v in os.environ.items() if not k.startswith(('MOP_','DJANGO_'))})
                for _ in range(200):
                    if Path(self.path).exists(): break
                    await asyncio.sleep(.02)
                self.assertTrue(Path(self.path).exists(),'owned protected daemon did not start')
                process=await asyncio.create_subprocess_exec(sys.executable,'-I','-c',code,
                    str(Path(__file__).resolve().parents[1]),'frontend',json.dumps(args),
                    user=65534,group=os.getegid(),extra_groups=(),pass_fds=(listener.fileno(),),
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                    env={k:v for k,v in os.environ.items() if not k.startswith(('MOP_','DJANGO_'))})
                tls=ssl.create_default_context(cafile=str(cert))
                origin=f'https://localhost:{port}'
                async with aiohttp.ClientSession(trust_env=False,connector=aiohttp.TCPConnector(ssl=tls),
                        timeout=aiohttp.ClientTimeout(total=3)) as client:
                    deadline=asyncio.get_running_loop().time()+12
                    while True:
                        try:
                            async with client.get(origin+'/api/v1/addresses/'+str(self.endpoint.id),
                                    headers={'Authorization':'Token '+self.auth.value}) as response:
                                self.assertEqual(response.status,200,await response.text())
                            break
                        except (aiohttp.ClientError,TimeoutError):
                            if asyncio.get_running_loop().time()>=deadline:
                                raise AssertionError('owned rootless ASGI failed to become ready') from None
                            await asyncio.sleep(.05)
                    async with client.ws_connect(origin.replace('https:','wss:')+'/ws/v1/inbox/'+str(self.endpoint.id),
                            headers={'Authorization':'Token '+self.auth.value,'Origin':origin},compress=0) as ws:
                        await ws.send_json(dict(schema_version=1,type='peer_ready',external_session_id=self.endpoint.external_session_id))
                        bound=await ws.receive_json(timeout=3)
                        nonce=bound['connection_nonce']
                        await ws.send_json(dict(schema_version=1,type='bound_received',connection_nonce=nonce))
                        sent=await runtime.blocking(self.send,CredentialClient(self.path,server_uid=65533))
                        delivery=await ws.receive_json(timeout=3)
                        self.assertEqual(delivery['frame']['id'],str(sent.id))
                        self.assertEqual(delivery['frame']['text'],self.values['text'])
                        ids=dict(connection_nonce=nonce,delivery_id=str(sent.id),message_id=str(sent.message_id))
                        await ws.send_json(dict(schema_version=1,type='prewrite',**ids))
                        self.assertEqual((await ws.receive_json(timeout=3))['type'],'write_allowed')
                        await ws.send_json(dict(schema_version=1,type='application_acceptance',outcome='accepted',**ids))
                        self.assertEqual((await ws.receive_json(timeout=3))['result']['state'],'accepted')
                        await ws.send_json(dict(schema_version=1,type='idle_observed',**ids))
                        self.assertTrue((await ws.receive_json(timeout=3))['result']['idle'])
                    self.assertEqual(await runtime.database(m.DeliveryReceipt.objects.count),2)
            finally:
                failures=[]
                for owned,name in ((process,'frontend'),(daemon,'credential')):
                    if owned is None:
                        continue
                    if owned.returncode is None:
                        owned.terminate()
                    try:
                        output,error=await asyncio.wait_for(owned.communicate(),10)
                    except TimeoutError:
                        owned.kill();output,error=await owned.communicate()
                        failures.append(name+' shutdown incomplete')
                    # Сначала собрать обоих owners; assertions не оставляют daemon.
                    if self.auth.value.encode() in output+error or self.values['text'].encode() in output+error:
                        failures.append(name+' leaked private data')
                    if owned.returncode not in (0,-15):
                        failures.append(name+' failed: '+error.decode())
                listener.close()
                await runtime.aclose()
                self.assertEqual(failures,[])
        async_to_sync(run)()

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
