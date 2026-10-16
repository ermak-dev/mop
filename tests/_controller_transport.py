"""Принадлежащий тесту TCP proxy: сервер жив, ответы теряются без FIN/RST."""
import copy
import selectors
import socket
import threading
import time

from django.db import connections


class Blackhole:
    def __init__(self, config):
        self.config = config
        self.drop = threading.Event()
        self.dropped = threading.Event()
        self.forwarded = threading.Event()
        self.stop = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(.05)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        peers = []
        with selectors.DefaultSelector() as poll:
            poll.register(self.listener, selectors.EVENT_READ, None)
            try:
                while not self.stop.is_set():
                    for key, _ in poll.select(.02):
                        if key.data is None:
                            client, _ = self.listener.accept()
                            server = socket.create_connection((self.config['HOST'], int(self.config['PORT'])), timeout=2)
                            peers.extend((client, server))
                            poll.register(client, selectors.EVENT_READ, (server, False))
                            poll.register(server, selectors.EVENT_READ, (client, True))
                            continue
                        target, response = key.data
                        try:
                            data = key.fileobj.recv(65536)
                            if not data:
                                poll.unregister(key.fileobj)
                                continue
                            if response and self.drop.is_set():
                                self.dropped.set()
                            else:
                                target.sendall(data)
                                if not response and self.drop.is_set():
                                    self.forwarded.set()
                        except OSError:
                            poll.unregister(key.fileobj)
            finally:
                for peer in peers:
                    peer.close()

    def close(self):
        self.stop.set()
        self.thread.join(3)
        self.listener.close()
        assert not self.thread.is_alive(), 'test proxy did not stop'

    def alias(self, name, *, pool):
        config = copy.deepcopy(self.config)
        config['HOST'], config['PORT'] = '127.0.0.1', str(self.port)
        config['OPTIONS'].pop('pool', None)
        if pool:
            config['OPTIONS']['pool'] = {'min_size': 0, 'max_size': 1, 'timeout': 1}
        connections.databases[name] = config
        return name


def wait_until(predicate, timeout):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(.005)
    return predicate()
