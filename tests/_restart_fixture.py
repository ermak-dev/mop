"""Только управляемый новый стенд: HTTPS server и зарегистрированный tmux layout."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import socket
import struct
import shlex
import sys
import subprocess
import time
from uuid import UUID, uuid4


class LoseAcceptance:
    """Fault только внешнего response stream, после реального domain COMMIT."""
    def __init__(self, app):
        self.app, self.used = app, False

    async def __call__(self, scope, receive, send):
        drop = False
        async def deliver(message):
            nonlocal drop
            if message['type']=='http.response.start' and message['status']==202 and not self.used:
                self.used, drop = True, True
                await send(message)
            elif not drop:
                await send(message)
        # Неполный stream закрывает настоящий HTTPS socket. Никакого второго POST.
        await self.app(scope,receive,deliver)


def https_server(listener, cert, key, grants, support):
    from django.conf import settings
    from django.db import connections
    connections.close_all()
    from mop.server.controller import machineviews, operationviews
    from mop.server.controller.machinebinding import compose, operations
    machineviews.execution = compose(grants=grants, restart_support=support)
    operationviews.restart_api = lambda: operations(grants=grants, restart_support=support)
    settings.ALLOWED_HOSTS = ['localhost', '127.0.0.1']
    from django.core.asgi import get_asgi_application
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(LoseAcceptance(get_asgi_application()), lifespan='off', log_level='error',
        ssl_certfile=str(cert), ssl_keyfile=str(key)))
    server.run(sockets=[listener])


APPLICATION = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, socket, subprocess, sys
root = pathlib.Path(sys.argv[1])
address = root/'app.sock'
try: address.unlink()
except FileNotFoundError: pass
identity = subprocess.check_output(['git','-C',str(root/'clone'),'var','GIT_AUTHOR_IDENT'], text=True)
server = socket.socket(socket.AF_UNIX)
server.bind(str(address)); os.chmod(address,0o600); server.listen(5)
while True:
    connection, _ = server.accept()
    with connection:
        request = json.loads(connection.recv(4096))
        connection.sendall(json.dumps({'nonce':request['nonce'], 'pid':os.getpid(), 'scope':os.getpgrp(),
            'identity':identity, 'ready':True}).encode()+b'\n')
'''


class Layout:
    """Зарегистрированный fixture layout; production не получает этого resolver."""
    def __init__(self, root, target, client=None):
        self.root, self.node_id, self.client = root, str(target.node_id), client
        self.old_pid = None
        self.new_pid = None
        self.pin = (target.id,target.current_incarnation_id,target.node_id,
            target.current_incarnation.executor_epoch,target.generation,target.role,target.bundle_digest,target.grant_id)
        self.validate_layout()

    def validate_layout(self):
        for name in ('launcher','application.py'):
            path = self.root/name
            if not path.is_file() or path.is_symlink():
                raise PermissionError('unregistered fixture layout')

    def require(self, binding):
        if (binding.target_id, binding.incarnation_id, binding.node_id, binding.executor_epoch,
                binding.generation, binding.role, binding.bundle_digest, binding.grant_id) != self.pin:
            raise PermissionError('fixture layout binding changed')

    async def resolve(self, work):
        self.validate_layout()
        from mop.node.execution_adapters import BoundTarget, ActionInput
        if (UUID(work.target_id),UUID(work.incarnation_id),UUID(work.node_id),UUID(work.executor_epoch),
                work.generation,work.role,work.bundle_digest,UUID(work.grant_id)) != self.pin:
            raise PermissionError('unregistered physical work binding')
        value = await self.client.action_input(work)
        target = BoundTarget(work.target_id, work.incarnation_id, work.node_id, work.executor_epoch,
            work.generation, work.role,work.bundle_digest,work.grant_id,self.root/'clone',
            self.root/'tmux.sock','%0',self.root/'launcher',self.root/'app.sock')
        return target, ActionInput(value['git_name'],value['git_email'])

    async def reconcile(self, node_id, previous_epoch, new_epoch):
        # В этом новом layout нет старых node executor effects; старая app не убивается.
        return self.old_pid is None and node_id == self.node_id

    async def probe(self):
        nonce = uuid4().hex
        reader, writer = await asyncio.open_unix_connection(str(self.root/'app.sock'))
        try:
            writer.write(json.dumps({'nonce':nonce}).encode()); await writer.drain()
            value = json.loads(await asyncio.wait_for(reader.readline(), 2))
            peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
            pid, uid, _ = struct.unpack('3i',peer)
            if (value['nonce'] != nonce or value['ready'] is not True or uid != os.geteuid()
                    or pid != value['pid'] or os.getpgid(pid) != value['scope']):
                raise RuntimeError('unaddressed application readiness or process scope')
            return value
        finally:
            writer.close(); await writer.wait_closed()

    async def confirmed(self, work, target):
        # Один explicit fixture port реализует два read-only confirmation контракта.
        deadline = time.monotonic()+5
        while True:
            try:
                value = await self.probe()
                if work.action != 'restart_session' or value['pid'] != self.old_pid:
                    break
            except (OSError, ConnectionError):
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(.02)
        if work.action == 'restart_session':
            try:
                os.killpg(self.old_pid, 0)
            except ProcessLookupError:
                pass
            else:
                return False
            if value['pid'] == self.old_pid or value['scope'] == self.old_pid:
                return False
            self.new_pid = value['pid']
        return (value['pid'] == self.new_pid and
            value['identity'].startswith('Fixture Owner <owner@example.invalid> '))


def new_layout(root, target):
    # Материализуем DTO до async; resolver не получает ленивый SQL.
    target.current_incarnation
    subprocess.run(['git','init','-q',str(root/'clone')], check=True)
    # До identity stage конфигурация явно отличается от владельца.
    subprocess.run(['git','-C',str(root/'clone'),'config','user.name','Old Fixture'],check=True)
    subprocess.run(['git','-C',str(root/'clone'),'config','user.email','old@example.invalid'],check=True)
    (root/'application.py').write_text(APPLICATION)
    (root/'launcher').write_text('#!/bin/sh\nexec '+shlex.join((sys.executable,str(root/'application.py'),str(root)))+'\n')
    (root/'launcher').chmod(0o700)
    subprocess.run(['tmux','-S',str(root/'tmux.sock'),'new-session','-d','-s','fixture',str(root/'launcher')],check=True)
    subprocess.run(['tmux','-S',str(root/'tmux.sock'),'set-option','-g','remain-on-exit','on'],check=True)
    return Layout(root,target)
