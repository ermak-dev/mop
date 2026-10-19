"""Фиксированный owned-native эксперимент #773, не production launcher.

Вызывается лишь после kernel proof normal Docker. Launcher передаёт ровно
свой измеренный процесс и fixture-only key из созданного им private home.
WSS peer здесь контролируемый; его receipt не выдаётся за SQL COMMIT.

Контракт квитанций native 2.1.302 (#785, вариант A): принятый обычный
user-кадр не порождает положительной квитанции, поэтому `unknown` — правдивый
терминальный исход. Подтверждением служит только явный native `delivered`
(освобождение ранее удержанного сообщения); явный отказ даёт `unavailable`.
Тишина, слитый write, idle и активность провайдера/UI исход не повышают.
Контракт привязан к версии 2.1.302 и подлежит перепроверке при её смене.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
from uuid import UUID, uuid4

from mop import session
from mop.common.endpoint import inbox_encode, inbox_decode
from .control import ServerContext
from .local_bridge import LocalBridge, OwnSession, _unique_object, _invalid_constant
from .private_inbox import InboxOwner

# Контракт квитанций версии, на которой снят этот эксперимент. Смена версии
# native требует новой перепроверки: молчание и idle могут означать иное.
CONTRACT = 'native 2.1.302 ordinary inbox receipt contract; re-verify on native version change'
_POSITIVE = ('delivered',)
_REFUSALS = ('denied', 'expired', 'refused', 'dropped')
_OUTCOMES = ('accepted', 'unknown', 'unavailable')


def classify(details):
    """Чистое суждение probe по контракту 2.1.302 (#785, вариант A).

    `observed` здесь значит «эксперимент дал ожидаемое по контракту», а не
    «native принял сообщение». Принятие требует явного `delivered`; обычная
    доставка честно остаётся `unknown` и не повторяется. Тишина, слитый write,
    idle и активность провайдера/UI исход не повышают.
    """
    problems = []
    events = [row for row in details.get('event_timeline') or [] if type(row) is dict]
    if details.get('native_version') != '2.1.302':
        problems.append('the receipt contract is tied to native 2.1.302 and must be re-verified '
                        'on a version change')
    recorded = details.get('native_outcome')
    if recorded not in _OUTCOMES:
        problems.append('native outcome is not a known terminal value')
    outcome = recorded if recorded in _OUTCOMES else 'unknown'
    statuses = [row.get('status') for row in events
        if row.get('event') == 'native_callback' and row.get('action') == 'peer_message_status'
        and row.get('correlation') == 'user']
    delivered = any(value in _POSITIVE for value in statuses)
    refused = any(value in _REFUSALS for value in statuses)
    if outcome == 'accepted' and not delivered:
        problems.append('accepted requires an explicit native delivered receipt')
    if outcome == 'unavailable' and not refused:
        problems.append('unavailable requires an explicit native refusal status')
    if outcome == 'unknown' and (delivered or refused):
        problems.append('native status contradicts the unknown outcome')
    writes = details.get('native_user_writes')
    if writes != 1:
        problems.append('exactly one drained native user write is required; no re-send')
    started = sum(row.get('event') == 'native_write_started' and row.get('frame_type') == 'user'
        for row in events)
    if started and started != writes:
        problems.append('native user frame was re-sent')
    for name, phrase in (('holder_cleared', 'holder was not cleared'),
                         ('tasks_drained', 'owned tasks were not drained'),
                         ('callback_removed', 'native callback was not removed')):
        if details.get(name) is not True:
            problems.append(phrase)
    if details.get('trace_dropped_events'):
        problems.append('trace dropped events; evidence is incomplete')
    if details.get('cold_home_onboarding'):
        problems.append('cold-boot HOME onboarding was not tested; the fixture HOME is prepared')
    if details.get('prepared_owned_fixture_only') is not True:
        problems.append('the evidence must state that the fixture HOME is prepared, not cold-boot proof')
    receipt = details.get('controller_receipt')
    if not (type(receipt) is str and 'controlled WSS peer' in receipt
            and 'not committed PostgreSQL' in receipt):
        problems.append('controller receipt source must name the controlled WSS peer, '
                        'not committed PostgreSQL')
    if details.get('diagnostic_idle_is_delivery_evidence'):
        problems.append('diagnostic idle is a separate observation, not delivery evidence')
    if details.get('native_idle') and outcome != 'accepted':
        problems.append('idle is a separate observation and cannot accompany a non-accepted outcome')
    observed = not problems
    return dict(outcome='observed' if observed else 'unknown', native_outcome=outcome,
        acceptance=observed and outcome == 'accepted' and delivered and not refused,
        idle=details.get('native_idle') is True, contract=CONTRACT, problems=problems)


class NativeTrace:
    """Конечная fixture-only проекция, никогда не acceptance adapter."""
    KINDS = frozenset(('native_write_started', 'native_write_drained', 'native_write_failed',
        'prewrite_guard_passed', 'native_callback_peer_verified',
        'native_callback', 'native_callback_invalid', 'native_delivery_result',
        'wss_bound', 'wss_delivery', 'wss_prewrite', 'wss_write_allowed',
        'wss_application_acceptance', 'wss_idle_observed', 'wss_receipt',
        'diagnostic_idle_started', 'diagnostic_idle_finished', 'owner_closed'))

    def __init__(self, *, clock=time.monotonic_ns):
        self.clock, self.origin = clock, clock()
        self.events, self.dropped_events = [], 0
        self.user_id = self.diagnostic_id = None
        self.diagnostic_idle = asyncio.Event()

    def event(self, kind, **fields):
        if kind not in self.KINDS:
            return
        if len(self.events) >= 128:
            self.dropped_events += 1
            return
        row = dict(event=kind, at_ns=self.clock()-self.origin)
        for name in ('message_id', 'subscription_id', 'delivery_id', 'orig_msg_id'):
            value = fields.get(name)
            try:
                if type(value) is str and str(UUID(value)) == value:
                    row[name] = value
            except ValueError:
                pass
        allowed = dict(action=('peer_message_status', 'peer_idle_notice', 'notify_when_idle'),
            status=('held', 'delivered', 'denied', 'expired', 'refused', 'dropped'),
            state=('idle', 'exited', 'unavailable', 'accepted', 'unknown'),
            outcome=('accepted', 'unknown', 'unavailable'), frame_type=('user', 'control'),
            correlation=('user', 'delivery-idle', 'diagnostic-only', 'unmatched'),
            failure=('OSError', 'TimeoutError', 'PermissionError', 'ValueError', 'CancelledError'))
        for name, values in allowed.items():
            if type(fields.get(name)) is str and fields[name] in values:
                row[name] = fields[name]
        for name in ('idle', 'matched', 'holder_cleared', 'tasks_drained', 'callback_removed'):
            if type(fields.get(name)) is bool:
                row[name] = fields[name]
        self.events.append(row)

    def callback(self, raw, subscriptions=()):
        """Вызывается только после физической проверки callback peer."""
        try:
            if len(raw) > 8192:
                raise ValueError('bounded callback')
            value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if (type(value) is not dict or type(value.get('msgV')) is not int or value['msgV'] != 1
                    or value.get('type') != 'control'):
                raise ValueError('callback shape')
            action, key = value.get('action'), value.get('orig_msg_id')
            if type(key) is not str or str(UUID(key)) != key:
                raise ValueError('callback correlation')
            if action == 'peer_message_status' and value.get('status') in (
                    'held', 'delivered', 'denied', 'expired', 'refused', 'dropped'):
                correlation = 'user' if key == self.user_id else 'unmatched'
            elif action == 'peer_idle_notice' and value.get('state') in ('idle', 'exited', 'unavailable'):
                correlation = ('diagnostic-only' if key == self.diagnostic_id else
                    'delivery-idle' if key in subscriptions else 'unmatched')
                if correlation == 'diagnostic-only' and value['state'] == 'idle':
                    self.diagnostic_idle.set()
            else:
                raise ValueError('callback action')
            self.event('native_callback', action=action, orig_msg_id=key, correlation=correlation,
                status=value.get('status'), state=value.get('state'))
        except (ValueError, TypeError, RecursionError):
            self.event('native_callback_invalid')


class ObservedBridge(LocalBridge):
    """Тот же production bridge и те же fences; наблюдение без изменения verdict."""
    def __init__(self, own, trace, **kwargs):
        super().__init__(own, **kwargs)
        self.trace = trace

    async def _write(self, frame, guard=None):
        fields = dict(frame_type=frame['type'], message_id=frame['msg_id'], action=frame.get('action'))
        self.trace.event('native_write_started', **fields)
        async def observed_guard():
            await guard()
            self.trace.event('prewrite_guard_passed', **fields)
        try:
            await super()._write(frame, observed_guard if guard is not None else None)
        except (OSError, TimeoutError, PermissionError, ValueError, asyncio.CancelledError) as error:
            self.trace.event('native_write_failed', **fields, failure=type(error).__name__)
            raise
        self.trace.event('native_write_drained', **fields)

    async def _receive(self, reader, writer):
        bridge = self
        class ProjectedReader:
            async def readline(self):
                raw = await reader.readline()
                # Повтор после read: failed physical proof не даёт fake evidence.
                bridge._peer(writer)
                bridge.trace.event('native_callback_peer_verified')
                bridge.trace.callback(raw, (pair[0] for pair in bridge._idle.values()))
                return raw
        await super()._receive(ProjectedReader(), writer)

    async def _deliver(self, frame, guard):
        result = await super()._deliver(frame, guard)
        self.trace.event('native_delivery_result', message_id=str(frame.message_id), outcome=result.outcome)
        return result


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
        trace = NativeTrace()
        trace.user_id = message
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
                trace.event('wss_delivery', delivery_id=delivery, message_id=message)
                await ws.send_str(inbox_encode(dict(schema_version=1,type='delivery',connection_nonce=nonce,
                    frame=dict(id=delivery,message_id=message,external_session_id=own.external_session_id,
                        text='probe:after-model owned WSS native inbox fixture '+message,
                        priority='now',notify_when_idle=True,reply_address=None))))
                async for raw in ws:
                    if raw.type!=WSMsgType.TEXT: break
                    value=inbox_decode(raw.data,direction='client')
                    if value['type']=='prewrite':
                        trace.event('wss_prewrite', delivery_id=delivery, message_id=message)
                        await ws.send_str(inbox_encode(dict(value,type='write_allowed')))
                        trace.event('wss_write_allowed', delivery_id=delivery, message_id=message)
                    elif value['type']=='application_acceptance':
                        trace.event('wss_application_acceptance', delivery_id=delivery, message_id=message, outcome=value['outcome'])
                        await evidence.put(dict(outcome=value['outcome']))
                        await ws.send_str(inbox_encode(dict(schema_version=1,type='receipt',connection_nonce=nonce,
                            result=dict(id=delivery,message_id=message,operation_request_id=None,state=value['outcome'],idle=False))))
                    elif value['type']=='idle_observed':
                        trace.event('wss_idle_observed', delivery_id=delivery, message_id=message)
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
        owner.bridge = ObservedBridge(own, trace, timeout=2)
        details=dict(native_version='2.1.302',trusted_provenance='owned Terminal launcher; exact PID/start/namespace/cwd/fixture conversation',
            verified_https=True,actual_wss=True,native_outcome='unknown',native_idle=False,
            controller_receipt='controlled WSS peer only; not committed PostgreSQL evidence',
            production_launcher=False,paid_provider_tested=False,
            cold_home_onboarding=False, prepared_owned_fixture_only=True,
            diagnostic_idle_is_delivery_evidence=False,
            event_timeline=trace.events,
            physical=dict(pid=own.pid, process_start=own.process_start, uid=own.uid,
                namespaces=own.namespaces, cwd=str(own.cwd), external_session_id=own.external_session_id,
                cgroup=Path('/proc/'+str(own.pid)+'/cgroup').read_text().strip()))
        try:
            await owner.start()
            details['bound']=owner.holder() is not None
            trace.event('wss_bound')
            # Независимый control, как в #729/#735: проверяет callback путь,
            # но не подтверждает user enqueue и не публикуется owner-ом как idle.
            trace.diagnostic_id = str(uuid4())
            trace.event('diagnostic_idle_started', subscription_id=trace.diagnostic_id)
            await owner.bridge._write(dict(msgV=1, type='control', msg_id=trace.diagnostic_id,
                session_id=own.external_session_id, action='notify_when_idle',
                **{'from': 'uds:'+str(owner.bridge._callback)}))
            end=asyncio.get_running_loop().time()+8
            while asyncio.get_running_loop().time()<end:
                await asyncio.to_thread(terminal.pump,.05)
                while not evidence.empty():
                    value=evidence.get_nowait()
                    if 'outcome' in value: details['native_outcome']=value['outcome']
                    if value.get('idle'): details['native_idle']=True
                if details['native_outcome']!='accepted' and details['native_outcome']!='unknown': break
                if details['native_idle']: break
            details['diagnostic_idle_observed']=trace.diagnostic_idle.is_set()
            trace.event('diagnostic_idle_finished', subscription_id=trace.diagnostic_id,
                idle=details['diagnostic_idle_observed'])
        finally:
            active=False
            await owner.aclose()
            details['holder_cleared']=owner.holder() is None
            details['tasks_drained']=not owner.tasks
            details['callback_removed']=owner.bridge._callback is None
            trace.event('owner_closed', holder_cleared=details['holder_cleared'],
                tasks_drained=details['tasks_drained'], callback_removed=details['callback_removed'])
            await runner.cleanup()
        details['trace_dropped_events']=trace.dropped_events
        details['native_user_writes']=sum(row['event']=='native_write_drained' and row.get('frame_type')=='user' for row in trace.events)
        # Принятие не заявляется здесь: чистую оценку по контракту 2.1.302
        # даёт classify(), и она же называет расхождения.
        return details
    return asyncio.run(experiment())
