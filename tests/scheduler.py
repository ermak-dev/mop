#!/usr/bin/env python3
"""Дедлайн stop intent перепроверяется после preflight GET, до POST (#679).

HYPOTHESIS (#679): NomadStopScheduler.stop_job проверяет ticket.not_after
только на входе, затем выполняет preflight GET (живой сеть — wall time не
ограничен), затем POST без повторной проверки. Intent, истёкший во время
GET, всё ещё останавливает тело: сигнал оператора теряет силу, а write
уходит. RED: writes=1 при истёкшем во время GET intent.
SOLUTION: неизменный UTC intent перепроверяется немедленно перед POST —
после ожидания preflight, до эффекта; определённо истёкший intent даёт
conditional-refusal без единого write, POST-таймаут остаётся unknown без
повторного POST (существующее поведение сохранено).
RESULT: RED воспроизведён на owned loopback peer (writes=1, 'succeeded'
при intent, истёкшем во время GET); после правки 13 проверок зелёные,
контроли (живой intent, CAS-замена, lost response) не изменились.
STATUS: FIXED — see #679
"""
import copy
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import hermetic  # noqa: F401 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from _lib import Checks  # noqa: E402

from mop.server.operations.scheduler import (  # noqa: E402
    BodyStopProof, JobPin, NomadStopScheduler, StopProof, StopTicket, job_metadata)


@dataclass(frozen=True)
class Target:
    """Duck-typed RestartTarget: scheduler читает только атрибуты."""
    target_id: UUID
    project_id: UUID
    node_id: UUID
    incarnation_id: UUID
    executor_epoch: UUID
    owner_id: int
    role: str
    bundle_digest: str
    grant_id: UUID
    generation: int
    revision: int


class Peer:
    """Owned loopback scheduler: задержка GET и точный счёт writes.

    Только собственные процессы проверки: 127.0.0.1, spawn-независимый,
    закрывается в finally. Это не Nomad acceptance — протокольный peer
    документированного jobs register/readback API.
    """

    def __init__(self, get_delay=0.0, replaced_index=None):
        self.state = {'job': None, 'writes': 0, 'gets': 0, 'posts': [],
                      'get_delay': get_delay, 'replaced_index': replaced_index}
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        self.listener = listener
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, value, status=200):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path.endswith('/allocations'):
                    self._reply([{'ID': 'owned-alloc-1', 'JobID': outer.state['job']['ID'],
                                  'ClientStatus': 'complete', 'DesiredStatus': 'stop'}])
                    return
                outer.state['gets'] += 1
                if outer.state['get_delay'] > 0:
                    time.sleep(outer.state['get_delay'])
                value = copy.deepcopy(outer.state['job'])
                if outer.state['replaced_index'] is not None:
                    value['JobModifyIndex'] = outer.state['replaced_index']
                self._reply(value)

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.state['posts'].append(value)
                if (value.get('EnforceIndex') is not True
                        or value.get('JobModifyIndex') != outer.state['job']['JobModifyIndex']):
                    self._reply({'error': 'Enforcing job modify index'}, 409)
                    return
                outer.state['writes'] += 1
                outer.state['job'] = copy.deepcopy(value['Job'])
                outer.state['job']['JobModifyIndex'] += 1
                self._reply({'JobModifyIndex': outer.state['job']['JobModifyIndex']})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
        self.server.socket = listener
        self.server.server_address = listener.getsockname()
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05},
                         daemon=True).start()
        self.origin = 'http://127.0.0.1:%d' % listener.getsockname()[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.listener.close()


def adapter(peer, scheduler_id, *, timeout=2):
    class Inspector:
        def inspect_stopped(self, ticket, allocation_ids):
            target = ticket.job.target
            return BodyStopProof(ticket.operation_id, ticket.token, target.target_id,
                                 target.incarnation_id, target.node_id, target.executor_epoch,
                                 allocation_ids, datetime.now(timezone.utc), True)

    return NomadStopScheduler(origin=peer.origin, scheduler_id=scheduler_id, token='owned-check',
                              body_inspector=Inspector(), timeout=timeout)


def ticket(peer, scheduler_id, seconds, *, index=7):
    target = Target(uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), 1, 'role', 'a' * 64, uuid4(), 3, 7)
    pin = JobPin(uuid4(), target, scheduler_id, 'owned-job', 'default', index, uuid4())
    peer.state['job'] = {'ID': 'owned-job', 'Namespace': 'default', 'JobModifyIndex': index,
                         'Stop': False, 'Meta': job_metadata(pin), 'Type': 'service'}
    return StopTicket(pin, uuid4(), uuid4(), datetime.now(timezone.utc) + timedelta(seconds=seconds))


def scenario(c, name, expect_proof, expect_writes, *, get_delay=0.0, replaced_index=None,
             seconds=30, gets=None, posts=None):
    """Один owned-loopback сценарий: peer, adapter, ticket, assert, cleanup."""
    peer = Peer(get_delay=get_delay, replaced_index=replaced_index)
    try:
        scheduler_id = uuid4()
        proof = adapter(peer, scheduler_id).stop_job(ticket(peer, scheduler_id, seconds))
        c.expect(name, (proof.outcome, proof.evidence), expect_proof)
        c.expect(name + " (writes)", peer.state['writes'], expect_writes)
        if gets is not None:
            c.expect(name + " (GETs)", peer.state['gets'], gets)
        if posts is not None:
            c.check(name + " (no POST left)", peer.state['posts'] == posts)
    finally:
        peer.close()


def main():
    c = Checks()

    # Intent истёк ВО ВРЕМЯ preflight GET: единственный GET живой сети длится
    # дольше остатка intent, каждый socket wait остаётся под timeout.
    # RED (текущий код): POST всё же уходит -- writes=1.
    scenario(c, "#679 intent expired during preflight GET refuses conditionally",
             ('failed', 'conditional-refusal'), 0, get_delay=1.0, seconds=0.4,
             posts=[])

    # Контроль: intent остаётся живым после того же ожидания GET — stop
    # по-прежнему доходит до эффекта (перепроверка не стреляет на живом intent).
    scenario(c, "#679 live intent after the same preflight wait still stops",
             ('succeeded', 'job-quiescent'), 1, get_delay=1.0, seconds=30)

    # Истёкший на входе intent отказывает до любого сетевого запроса.
    scenario(c, "#679 expired-at-entry intent refuses conditionally",
             ('failed', 'conditional-refusal'), 0, seconds=-1, gets=0, posts=[])

    # CAS-ограждение сохранено: замена job во время preflight (modify_index
    # ушёл) — conditional-refusal, новый job не останавливается.
    scenario(c, "#679 replaced job during preflight refuses conditionally",
             ('failed', 'conditional-refusal'), 0, get_delay=1.0, replaced_index=8, posts=[])

    # Потерянный ответ POST остаётся unknown и не повторяет write.
    peer = Peer()
    try:
        scheduler_id = uuid4()
        value = ticket(peer, scheduler_id, 30)
        peer.state['job'] = None  # следующий GET уронит peer -- ответ потерян
        proof = adapter(peer, scheduler_id).stop_job(value)
        c.expect("#679 lost POST response stays unknown without a second write",
                 (proof.outcome, proof.evidence), ('unknown', 'scheduler-uncertain'))
    finally:
        peer.close()

    return c.report("scheduler")


if __name__ == "__main__":
    sys.exit(main())
