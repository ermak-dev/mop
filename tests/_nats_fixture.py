"""Собственный native TLS broker, только binary из объявленного Docker image."""
from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
import socket
import ssl
import signal
import subprocess
import tempfile
import hermetic


@asynccontextmanager
async def broker(project_id, other_id):
    binary=hermetic.FIXTURE_ENV.get('MOP_TEST_NATS_BINARY')
    if not binary:
        raise RuntimeError('native TLS NATS fixture requires mop dev controller --realtime')
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)
        cert,key=path/'cert.pem',path/'key.pem'
        process=await asyncio.create_subprocess_exec('openssl','req','-x509','-newkey','rsa:2048','-nodes',
            '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost',
            '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if await process.wait():
            raise RuntimeError('native TLS fixture certificate creation failed')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        project=f'mop.rt.v1.fixture.project.{project_id}.events'
        other=f'mop.rt.v1.fixture.project.{other_id}.events'
        from mop.server.realtime.acl import broker_permissions
        from uuid import UUID
        config={'host':'127.0.0.1','port':port,
            'tls':{'cert_file':str(cert),'key_file':str(key),'timeout':2},
            'authorization':{'users':[
                {'user':'gateway','password':'fixture-gateway','permissions':
                    broker_permissions('gateway','fixture',project_ids=(project_id,))},
                {'user':'publisher','password':'fixture-publisher','permissions':
                    broker_permissions('publisher','fixture',project_ids=(project_id,))},
                {'user':'node','password':'fixture-node','permissions':
                    broker_permissions('node','fixture',node_id=UUID(int=1))},
            ]}}
        config_path=path/'nats.conf'
        config_path.write_text(json.dumps(config))
        log=(path/'broker.log').open('wb')
        server=await asyncio.create_subprocess_exec(binary,'-c',str(config_path),stdout=log,stderr=log)
        context=ssl.create_default_context(cafile=str(cert))
        try:
            for attempt in range(100):
                if server.returncode is not None:
                    raise RuntimeError('owned NATS fixture stopped during startup')
                try:
                    reader,writer=await asyncio.open_connection('127.0.0.1',port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.02)
            else:
                raise RuntimeError('owned NATS fixture startup deadline exceeded')
            async def revoke_gateway():
                config['authorization']['users']=[user for user in config['authorization']['users']
                                                   if user['user']!='gateway']
                config_path.write_text(json.dumps(config))
                server.send_signal(signal.SIGHUP)
            yield {'servers':[f'tls://127.0.0.1:{port}'],'tls':context,'project':project,'other':other,
                   'cert':str(cert),'key':str(key),'log':str(path/'broker.log'),'revoke_gateway':revoke_gateway}
        finally:
            if server.returncode is None:
                server.terminate()
            try:
                await asyncio.wait_for(server.wait(),3)
            except TimeoutError:
                server.kill()
                await server.wait()
            log.close()
