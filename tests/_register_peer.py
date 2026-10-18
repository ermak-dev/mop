"""Owned loopback scheduler для регистрации job (#643): только jobs register/readback.

Это не Nomad acceptance: peer реализует документированный протокол
GET /v1/job/<id> (404, если job нет) и POST /v1/jobs с EnforceIndex, точный счёт
записей и потерю ответа после COMMIT. Только 127.0.0.1, закрывается владельцем.
"""
import copy
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


class RegisterPeer:
    def __init__(self):
        self.jobs, self.posts, self.lose_post = {}, [], False
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
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
                job_id = unquote(urlsplit(self.path).path.rsplit('/', 1)[-1])
                job = outer.jobs.get(job_id)
                if job is None:
                    return self._reply({'error': 'job not found'}, 404)
                return self._reply(copy.deepcopy(job))

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.posts.append(copy.deepcopy(value))
                job = value['Job']
                current = outer.jobs.get(job['ID'], {}).get('JobModifyIndex', 0)
                if value.get('EnforceIndex') is not True or value.get('JobModifyIndex') != current:
                    return self._reply({'error': 'Enforcing job modify index'}, 409)
                outer.jobs[job['ID']] = {**copy.deepcopy(job), 'JobModifyIndex': current + 1, 'Stop': False}
                if outer.lose_post:
                    self.close_connection = True
                    return
                return self._reply({'JobModifyIndex': current + 1})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
        self.server.socket = listener
        self.server.server_address = listener.getsockname()
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
        self.origin = 'http://127.0.0.1:%d' % listener.getsockname()[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.listener.close()
