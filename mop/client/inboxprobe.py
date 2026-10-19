"""Фиксированный owned-native эксперимент #773, не production launcher.

Вызывается лишь после kernel proof normal Docker. Launcher передаёт ровно
свой измеренный процесс и fixture-only key из созданного им private home.
WSS peer здесь контролируемый; его receipt не выдаётся за SQL COMMIT.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
from uuid import uuid4

from mop import session
from mop.common.endpoint import inbox_encode, inbox_decode
from .control import ServerContext
from .local_bridge import OwnSession
from .private_inbox import InboxOwner


def run(terminal, ready, home, runtime, output):
    from . import chatprobe
    chatprobe._normal_guard()
    if not chatprobe.live_contract_observed(ready) or len(ready) != 1:
        raise RuntimeError('native inbox fixture requires exactly one owned live conversation')
    row = ready[0]
    # Это собственный key launcher-а, не production config и не discovery API.
    peer = json.loads((home/'.claude'/'sessions'/row['key_file']).read_text())
    measured = session.native_process(row['pid'])
    own = OwnSession(row['messagingSocketPath'], row['pid'], row['sessionId'],
        process_start=measured['start'], uid=measured['uid'], namespaces=measured['namespaces'],
        cwd=Path(measured['cwd']), token=peer['peerToken'])
    cert, key = output/'inbox-ca.pem', output/'inbox-tls-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
        '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    async def experiment():
        from aiohttp import web, WSMsgType
        endpoint, epoch, client, project, nonce, delivery, message = [str(uuid4()) for _ in range(7)]
        evidence = asyncio.Queue(maxsize=4)
        async def capabilities(request):
            return web.json_response(dict(schema_version=1,catalogue_version=1,epoch=epoch,
                actor=dict(user_id=1,kind='human',human_owner_id=1,node_id=None,grant_id=None),
                scope=dict(project_id=project,node_id=None,client_id=client),
                actions=['execution.register_endpoint']))
        async def register(request):
            body=await request.json()
            if body != dict(schema_version=1,client_id=client,project_id=project,
                    external_session_id=own.external_session_id,display_name='owned native fixture'):
                raise web.HTTPBadRequest()
            return web.json_response(dict(schema_version=1,endpoint=dict(id=endpoint,session_id=str(uuid4()),
                address='private:'+endpoint,project_id=project,external_session_id=own.external_session_id,
                display_name='owned native fixture',expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),
                state='registered',live=False)),status=201)
        async def websocket(request):
            ws=web.WebSocketResponse(compress=False,max_msg_size=8192)
            await ws.prepare(request)
            try:
                value=inbox_decode((await ws.receive()).data,direction='client')
                if value['type']!='peer_ready': raise ValueError('missing native readiness')
                await ws.send_str(inbox_encode(dict(schema_version=1,type='bound',endpoint_id=endpoint,
                    external_session_id=own.external_session_id,epoch=epoch,generation='1',connection_nonce=nonce,lease_seconds=15)))
                value=inbox_decode((await ws.receive()).data,direction='client')
                if value['type']!='bound_received': raise ValueError('missing native bound confirmation')
                await ws.send_str(inbox_encode(dict(schema_version=1,type='delivery',connection_nonce=nonce,
                    frame=dict(id=delivery,message_id=message,external_session_id=own.external_session_id,
                        text='probe:after-model owned WSS native inbox fixture '+message,
                        priority='now',notify_when_idle=True,reply_address=None))))
                async for raw in ws:
                    if raw.type!=WSMsgType.TEXT: break
                    value=inbox_decode(raw.data,direction='client')
                    if value['type']=='prewrite':
                        await ws.send_str(inbox_encode(dict(value,type='write_allowed')))
                    elif value['type']=='application_acceptance':
                        await evidence.put(dict(outcome=value['outcome']))
                        await ws.send_str(inbox_encode(dict(schema_version=1,type='receipt',connection_nonce=nonce,
                            result=dict(id=delivery,message_id=message,operation_request_id=None,state=value['outcome'],idle=False))))
                    elif value['type']=='idle_observed':
                        await evidence.put(dict(idle=True))
                        await ws.send_str(inbox_encode(dict(schema_version=1,type='receipt',connection_nonce=nonce,
                            result=dict(id=delivery,message_id=message,operation_request_id=None,state='accepted',idle=True))))
            except (ConnectionError, ValueError):
                pass
            finally:
                await ws.close()
            return ws
        app=web.Application()
        app.router.add_get('/api/v1/capabilities',capabilities)
        app.router.add_post('/api/v1/endpoints',register)
        app.router.add_get('/ws/v1/inbox/{endpoint}',websocket)
        runner=web.AppRunner(app,access_log=None); await runner.setup()
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert,key)
        site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=tls); await site.start()
        port=site._server.sockets[0].getsockname()[1]
        active=True
        owner=InboxOwner(ServerContext('https://127.0.0.1:'+str(port),'owned-fixture-only-token',str(cert)),
            own,client_id=client,project_id=project,display_name='owned native fixture',current=lambda: active)
        details=dict(native_version='2.1.302',trusted_provenance='owned Terminal launcher; exact PID/start/namespace/cwd/fixture conversation',
            verified_https=True,actual_wss=True,native_outcome='unknown',native_idle=False,
            controller_receipt='controlled WSS peer only; not committed PostgreSQL evidence',
            production_launcher=False,paid_provider_tested=False)
        try:
            await owner.start()
            details['bound']=owner.holder() is not None
            end=asyncio.get_running_loop().time()+8
            while asyncio.get_running_loop().time()<end:
                await asyncio.to_thread(terminal.pump,.05)
                while not evidence.empty():
                    value=evidence.get_nowait()
                    if 'outcome' in value: details['native_outcome']=value['outcome']
                    if value.get('idle'): details['native_idle']=True
                if details['native_outcome']!='accepted' and details['native_outcome']!='unknown': break
                if details['native_idle']: break
        finally:
            active=False
            await owner.aclose()
            details['holder_cleared']=owner.holder() is None
            details['tasks_drained']=not owner.tasks
            details['callback_removed']=owner.bridge._callback is None
            await runner.cleanup()
        details['no_fabricated_acceptance']=True
        return details
    return asyncio.run(experiment())
