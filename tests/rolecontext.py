"""Контекст сервера живой роли без joined-профиля (#650).

HYPOTHESIS: lib.cluster ищет человеческий join, игнорируя выданное grant-подключение.
SOLUTION: владелец выбора адреса учитывает явный runtime grant JSON.
STATUS: FIXED — see #650.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import hermetic
from _lib import Checks, patched
from mop.common import config, context, creds


def main():
    c = Checks()
    with tempfile.TemporaryDirectory(prefix="mop-rolecontext-650-") as root:
        runtime = Path(root, "bus.json")
        node = Path(root, "node.env")
        node.write_text("")
        runtime.write_text(json.dumps({'url':'nats://role-server.example:4222',
            'user':'grant-'+'a'*32,'password':'fixture-private'}))
        env = dict(os.environ)
        for key in ('MOP_SERVER_LAN','MOP_SERVER_DIR','MOP_BUS_USER'):
            env.pop(key,None)
        env['MOP_BUS_CONFIG'] = str(runtime)
        with patch.dict(os.environ,env,clear=True), \
             patched(config,NODE_ENV_FILE=str(node)), \
             patched(creds,joined_dirs=lambda: []), \
             context.isolated(context.Context()), config.client_sources():
            config.forget()
            try:
                host = config.get('MOP_SERVER_LAN')
            except RuntimeError as error:
                host = str(error)
            c.expect('#650 a role uses its runtime server without a human join',
                     host,'role-server.example')
            for value in ('not-json', '[]', json.dumps({'user':'grant-'+'a'*32,
                         'url':'nats://user:fixture-private@wrong.example:4222'}),
                         json.dumps({'user':'grant-'+'a'*32,'url':'nats:///missing'})):
                runtime.write_text(value)
                try:
                    config.get('MOP_SERVER_LAN')
                    c.check('#650 corrupt runtime context refuses',False)
                except (RuntimeError,ValueError) as error:
                    c.check('#650 runtime errors contain no credentials',
                            'fixture-private' not in str(error),str(error))
            runtime.write_text(json.dumps({'url':'nats://role-server.example:4222',
                 'user':'grant-'+'a'*32,'password':'fixture-private'}))
            with context.isolated(context.Context(server='explicit.example')):
                c.expect('#650 explicit server context still wins',
                         config.get('MOP_SERVER_LAN'),'explicit.example')
            with patch.dict(os.environ,{'MOP_SERVER_LAN':'environment.example'}):
                c.expect('#650 explicit server environment still wins',
                         config.get('MOP_SERVER_LAN'),'environment.example')
            with patch.dict(os.environ,{},clear=True), patched(creds,joined_dirs=lambda:['human.example']):
                c.expect('#650 no runtime keeps human joined selection',
                         creds.joined_host(),'human.example')
    return c.report('rolecontext')


if __name__=='__main__':
    raise SystemExit(main())
