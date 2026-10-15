"""Owned loopback HTTP adapter: без proxy env, redirects и SQL во время I/O."""
import http.client
import socket
import threading
import time
from .policy import MAX_BODY, validate, request_headers
from .values import Reply


class TransportUnavailable(RuntimeError):
    def __init__(self, *, receipt_status=None):
        super().__init__('proxy management transport unavailable')
        self.receipt_status = receipt_status


class LoopbackHTTP:
    def __init__(self, port, *, timeout=10):
        if type(port) is not int or not 1 <= port <= 65535 or not 0 < timeout <= 10:
            raise ValueError('invalid trusted loopback configuration')
        self.port, self.timeout = port, timeout

    def call(self, method, path, body, headers, credential):
        # Даже ошибочная композиция не превращает port в URL или public /v1.
        if validate(method, path, body, panel=path == '/management.html'):
            raise TransportUnavailable()
        if not isinstance(credential.value, str) or not 1 <= len(credential.value) <= 4096 or any(
                ord(char) < 33 or ord(char) > 126 for char in credential.value):
            raise TransportUnavailable()
        forwarded = request_headers(headers)
        forwarded['Authorization'] = 'Bearer ' + credential.value
        forwarded['Accept-Encoding'] = 'identity'
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=min(5, self.timeout))
        deadline = time.monotonic() + self.timeout
        timer, status = None, None
        try:
            connection.connect()
            sock = connection.sock
            def abort():
                # shutdown будит getresponse/read даже при медленном drip body.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(max(.001, deadline - time.monotonic()), abort)
            timer.daemon = True
            timer.start()
            connection.request(method, path, body=body or None, headers=forwarded)
            response = connection.getresponse()
            status = response.status
            headers = tuple(response.getheaders())
            if (len(headers) > 64 or sum(len(k) + len(v) for k, v in headers) > 16384
                    or response.getheader('Content-Encoding', 'identity').lower() != 'identity'):
                raise TransportUnavailable(receipt_status=status)
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > MAX_BODY):
                raise TransportUnavailable(receipt_status=status)
            got = response.read(MAX_BODY + 1)
            if (len(got) > MAX_BODY or time.monotonic() >= deadline
                    or length is not None and len(got) != int(length)):
                raise TransportUnavailable(receipt_status=status)
            return Reply(status, got, headers)
        except TransportUnavailable:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise TransportUnavailable(receipt_status=status) from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            connection.close()
