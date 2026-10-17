"""Новый HTTP reference scheduler: CAS по revision и реальный дочерний процесс.

Это не Nomad daemon acceptance: fixture реализует только документированный
jobs register/readback protocol, не планировщик размещения контейнеров.
"""
import copy
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
from uuid import uuid4


def serve(listener, job, clone):
    import signal
    # unittest SIGINT handler нельзя наследовать: он лишь ставит shouldStop.
    signal.signal(signal.SIGINT,signal.default_int_handler)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],cwd=clone,start_new_session=True)
    state = {'job':job, 'writes':0, 'mode':'', 'all_allocations':False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/fixture':
                from urllib.parse import parse_qs
                return self.reply({'alive':child.poll() is None,'writes':state['writes'],'all_allocations':state['all_allocations'],
                    'metadata':state['job']['Meta'], 'nonce':parse_qs(urlsplit(self.path).query).get('nonce',[''])[0]})
            if path.endswith('/allocations'):
                from urllib.parse import parse_qs
                state['all_allocations'] = parse_qs(urlsplit(self.path).query).get('all')==['true']
                return self.reply([{'ID':'fixture-allocation','JobID':job['ID'],
                    'ClientStatus':'running' if child.poll() is None and state['mode']!='false-terminal' else 'complete',
                    'DesiredStatus':'run' if child.poll() is None and state['mode']!='false-terminal' else 'stop'}])
            return self.reply(state['job'])

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/fixture':
                state['mode'] = value['mode']
                if value['mode']=='replace':
                    state['job']['JobModifyIndex'] += 1
                return self.reply({})
            if self.path.startswith('/v1/jobs'):
                if (value['EnforceIndex'] is not True or
                        value['JobModifyIndex'] != state['job']['JobModifyIndex']):
                    return self.reply({'error':'Enforcing job modify index'},409)
                state['writes'] += 1
                state['job'] = copy.deepcopy(value['Job'])
                state['job']['JobModifyIndex'] += 1
                if state['mode'] not in ('live-after-stop','false-terminal'):
                    child.terminate()
                    child.wait(timeout=3)
                if state['mode']=='lose-response':
                    self.close_connection = True
                    return
                return self.reply({'JobModifyIndex':state['job']['JobModifyIndex']})
            self.reply({},404)
    server = HTTPServer(('127.0.0.1',0),Handler,bind_and_activate=False)
    server.socket = listener
    server.server_address = listener.getsockname()
    try:
        server.serve_forever(poll_interval=.05)
    except KeyboardInterrupt:
        pass
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=3)


class BodyInspector:
    def __init__(self, origin):
        self.origin = origin

    def inspect_stopped(self, ticket, allocation_ids):
        import urllib.request
        from datetime import datetime, timezone
        from mop.server.operations.scheduler import BodyStopProof, job_metadata
        nonce = uuid4().hex
        with urllib.request.urlopen(self.origin+'/fixture?nonce='+nonce,timeout=3) as response:
            value = json.load(response)
        target = ticket.job.target
        expected = {**job_metadata(ticket.job),'mop.stop_operation':str(ticket.operation_id),'mop.stop_token':str(ticket.token)}
        stopped = value['alive'] is False and value['nonce']==nonce and value['metadata']==expected
        return BodyStopProof(ticket.operation_id,ticket.token,target.target_id,target.incarnation_id,
            target.node_id,target.executor_epoch,allocation_ids,datetime.now(timezone.utc),stopped)


class SchedulerFixture:
    def __init__(self, target, epoch):
        import socket
        from mop.server.operations.scheduler import NomadStopScheduler, job_metadata
        self.directory = tempfile.TemporaryDirectory(prefix='mop-stop-owned-')
        self.root = Path(self.directory.name)
        self.clone = self.root/'clone'
        subprocess.run(['git','init','-q',str(self.clone)],check=True)
        (self.clone/'notes.txt').write_text('committed work')
        subprocess.run(['git','-C',str(self.clone),'add','notes.txt'],check=True)
        subprocess.run(['git','-C',str(self.clone),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            'commit','-qm','fixture baseline'],check=True)
        self.job_id = 'fixture-'+uuid4().hex
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1',0))
        self.listener.listen(8)
        self.origin = 'http://127.0.0.1:'+str(self.listener.getsockname()[1])
        from mop.server.operations.scheduler import JobPin
        from mop.server.operations.puppet import RestartTarget
        # DTO не пересекает ORM/async boundary лениво.
        binding = RestartTarget(target.id,target.project_id,target.node_id,target.current_incarnation_id,
            target.current_incarnation.executor_epoch,target.owner_id,target.role,target.bundle_digest,
            target.grant_id,target.generation,target.revision)
        scheduler_id = uuid4()
        from mop.server.controller.models import RestoreGuard
        pin = JobPin(epoch,binding,scheduler_id,self.job_id,'default',7,RestoreGuard.objects.get(pk=1).generation)
        job = {'ID':self.job_id,'Namespace':'default','JobModifyIndex':7,'Stop':False,
               'Meta':job_metadata(pin),'TaskGroups':[{'Name':'fixture','Tasks':[]}], 'Type':'service'}
        self.process = multiprocessing.get_context('spawn').Process(target=serve,args=(self.listener,job,str(self.clone)))
        self.process.start()
        self.adapter = NomadStopScheduler(origin=self.origin, scheduler_id=scheduler_id, token='fixture-only',
            body_inspector=BodyInspector(self.origin),timeout=2)

    def request(self, method, body=None):
        import urllib.request
        raw = None if body is None else json.dumps(body).encode()
        with urllib.request.urlopen(urllib.request.Request(self.origin+'/fixture', data=raw, method=method),timeout=4) as response:
            return json.load(response)

    def alive(self):
        return self.request('GET')['alive']

    def writes(self):
        return self.request('GET')['writes']

    def mode(self, value):
        self.request('POST', {'mode':value})

    def close(self):
        # Fixture daemon владеет дочерним процессом; graceful SIGINT завершает finally.
        import signal
        if self.process.is_alive():
            self.process.send_signal(signal.SIGINT) if hasattr(self.process,'send_signal') else __import__('os').kill(self.process.pid,signal.SIGINT)
        self.process.join(5)
        if self.process.is_alive():
            raise RuntimeError('owned scheduler fixture did not stop')
        self.listener.close()
        self.directory.cleanup()
