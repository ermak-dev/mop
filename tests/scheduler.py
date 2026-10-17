#!/usr/bin/env python3
"""Дедлайн stop intent перепроверяется после preflight GET, до POST (#679);
транспорт scheduler adapter делит один monotonic budget на весь публичный
вызов (#680): DNS/connect/TLS/статус/заголовки/тело и вложенные
preflight/readback запросы не получают каждый свой полный timeout.

HYPOTHESIS (#679): NomadStopScheduler.stop_job проверяет ticket.not_after
только на входе, затем выполняет preflight GET (живой сети — wall time не
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

HYPOTHESIS (#680): HTTPConnection(timeout=N) ограничивает каждый socket
wait, а не вызов: peer с drip-ответом (статус/заголовки/тело по капле,
каждое ожидание под timeout) держит observe/stop_job неограниченно, а
stop_job из вложенных preflight+POST+readback запросов получает каждый
свой полный timeout. RED: observe читает drip ~0.6s при timeout=0.3,
stop_job пишет при исчерпанном бюджете (5.47s wall).
SOLUTION: один monotonic budget = timeout на ПУБЛИЧНЫЙ вызов; DNS в
bounded потоке через _create_connection hook (hostname остаётся для
SNI/cert), connect с остатком бюджета, Timer+shutdown прерывает drip
статуса/заголовков/тела, финальная monotonic проверка; вложенные запросы
stop_job получают остаток того же бюджета. Body inspector — внешний порт
со своим адаптерным дедлайном, не в этом бюджете.
RESULT: RED доказан на том же unfixed scheduler (4 #680 проверки красные:
drip observe 0.62s при 0.3s budget, stop_job writes=1 при исчерпанном
бюджете, wall 5.47s); после правки 23 проверки зелёные, HTTPS cert
verification и SNI проверены отдельно (valid CA ok, wrong CA refused).
STATUS: FIXED — see #680
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
    BodyStopProof, JobPin, NomadStopScheduler, SchedulerUnavailable, StopProof,
    StopTicket, job_metadata)


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
            protocol_version = 'HTTP/1.1'

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
                    # Задержка только ПЕРВОГО job GET (preflight): #679 — про
                    # wall time до POST; readback GETs остаются быстрыми, чтобы
                    # весь публичный вызов укладывался в общий budget (#680).
                    delay = outer.state['get_delay']
                    outer.state['get_delay'] = 0.0
                    time.sleep(delay)
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


class DripPeer(Peer):
    """Ответ валиден, но тело капает порциями с паузой между ними.

    Каждое отдельное socket ожидание остаётся под client timeout — побеждает
    только aggregate budget. Status/headers приходят сразу: cap в теле.
    """

    def __init__(self, drip, slices=12, **options):
        self.slices = slices
        super().__init__(**options)
        self.state['drip'] = drip
        self._install()

    def _install(self):
        outer, slices = self, self.slices

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def _drip(self, value, status=200):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                step = max(1, len(raw) // slices)
                for offset in range(0, len(raw), step):
                    self.wfile.write(raw[offset:offset + step])
                    self.wfile.flush()
                    time.sleep(outer.state['drip'])

            def do_GET(self):
                path = urlsplit(self.path).path
                if path.endswith('/allocations'):
                    self._drip([{'ID': 'owned-alloc-1', 'JobID': outer.state['job']['ID'],
                                 'ClientStatus': 'complete', 'DesiredStatus': 'stop'}])
                    return
                outer.state['gets'] += 1
                self._drip(copy.deepcopy(outer.state['job']))

            def do_POST(self):
                length = int(self.headers['Content-Length'])
                value = json.loads(self.rfile.read(length))
                outer.state['posts'].append(value)
                if (value.get('EnforceIndex') is not True
                        or value.get('JobModifyIndex') != outer.state['job']['JobModifyIndex']):
                    self._drip({'error': 'Enforcing job modify index'}, 409)
                    return
                outer.state['writes'] += 1
                outer.state['job'] = copy.deepcopy(value['Job'])
                outer.state['job']['JobModifyIndex'] += 1
                self._drip({'JobModifyIndex': outer.state['job']['JobModifyIndex']})

        self.server.RequestHandlerClass = Handler


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

    # ── #680: бюджет ПУБЛИЧНОГО вызова ────────────────────────────────────
    # Один monotonic budget = timeout на весь публичный вызов: drip-ответ
    # (каждый socket wait под timeout) не может растянуть вызов за budget.
    # RED (текущий код): HTTPConnection(timeout) бьёт по каждому wait —
    # drip 0.05s x ~12 порций ≈ 0.6s проходит ЦЕЛИКОМ при timeout=0.3.
    # Контроль A: drip, укладывающийся в budget, читается успешно.
    peer = DripPeer(drip=0.01)
    try:
        scheduler_id = uuid4()
        value = ticket(peer, scheduler_id, 30)
        started = time.monotonic()
        index = adapter(peer, scheduler_id, timeout=0.5).observe(value.job)
        wall = time.monotonic() - started
        c.expect("#680 fitting drip observe reads the modify index", index, 7)
        c.check("#680 fitting drip observe completes (wall=%.2fs, timeout=0.5)" % wall,
                wall < 1.0)
    finally:
        peer.close()

    # Случай B: drip превышает budget — вызов обрывается В ПРЕДЕЛАХ budget
    # (SchedulerUnavailable), а не читается целиком за его границей.
    peer = DripPeer(drip=0.05)
    try:
        scheduler_id = uuid4()
        value = ticket(peer, scheduler_id, 30)
        started = time.monotonic()
        try:
            adapter(peer, scheduler_id, timeout=0.3).observe(value.job)
            c.fail("#680 over-budget drip observe must abort",
                   "observe returned; drip totals ~0.6s against a 0.3s budget")
        except SchedulerUnavailable:
            wall = time.monotonic() - started
            c.check("#680 over-budget drip observe aborts within budget (wall=%.2fs)" % wall,
                    wall < 0.45)
    finally:
        peer.close()

    # Вложенные запросы stop_job (preflight + POST + readback GETs) делят ТОТ
    # ЖЕ budget: полный успешный stop при дружелюбном peer остаётся быстрым,
    # drip в первом же preflight GET исчерпывает бюджет ВСЕГО вызова.
    # RED (текущий код): каждый вложенный _request получает свой полный
    # timeout; drip-подобная задержка накапливается за пределами timeout.
    peer = DripPeer(drip=0.08)
    try:
        scheduler_id = uuid4()
        value = ticket(peer, scheduler_id, 30)
        started = time.monotonic()
        proof = adapter(peer, scheduler_id, timeout=0.3).stop_job(value)
        wall = time.monotonic() - started
        c.expect("#680 drip stop_job stays unknown: budget spent in preflight",
                 (proof.outcome, proof.evidence), ('unknown', 'scheduler-uncertain'))
        c.check("#680 stop_job stays within the public-call budget (wall=%.2fs, timeout=0.3)" % wall,
                wall < 0.6)
        c.expect("#680 budget spent in preflight leaves zero writes", peer.state['writes'], 0)
    finally:
        peer.close()

    # Контроль: дружелюбный peer — полный успешный stop укладывается в бюджет.
    peer = Peer()
    try:
        scheduler_id = uuid4()
        started = time.monotonic()
        proof = adapter(peer, scheduler_id, timeout=2).stop_job(ticket(peer, scheduler_id, 30))
        wall = time.monotonic() - started
        c.expect("#680 friendly peer full stop stays succeeded",
                 (proof.outcome, proof.evidence), ('succeeded', 'job-quiescent'))
        c.check("#680 friendly peer full stop within budget (wall=%.2fs)" % wall, wall < 2.0)
    finally:
        peer.close()

    # DNS bounded strategy: hostname origin (не IP) резолвится в bounded
    # потоке и не вычитается из здоровья вызова; localhost — declared loopback,
    # резолв быстрый, fitting drip читается успешно через hostname origin.
    peer = DripPeer(drip=0.01)
    try:
        scheduler_id = uuid4()
        origin = peer.origin.replace('127.0.0.1', 'localhost')
        value = ticket(peer, scheduler_id, 30)

        class Inspector:
            def inspect_stopped(self, ticket_, allocation_ids):
                target = ticket_.job.target
                return BodyStopProof(ticket_.operation_id, ticket_.token, target.target_id,
                                     target.incarnation_id, target.node_id, target.executor_epoch,
                                     allocation_ids, datetime.now(timezone.utc), True)

        bounded = NomadStopScheduler(origin=origin, scheduler_id=scheduler_id, token='owned-check',
                                     body_inspector=Inspector(), timeout=0.5)
        started = time.monotonic()
        index = bounded.observe(value.job)
        wall = time.monotonic() - started
        c.expect("#680 hostname origin observe reads the index", index, 7)
        c.check("#680 hostname resolve + fitting drip completes (wall=%.2fs)" % wall, wall < 1.0)
    finally:
        peer.close()

    return c.report("scheduler")


if __name__ == "__main__":
    sys.exit(main())
