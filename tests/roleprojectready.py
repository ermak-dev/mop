"""Ролевое подключение достаточно для клиентского preflight (#653).

HYPOTHESIS: project_ready требует joined-профиль человека даже с выданным grant.
SOLUTION: фактический principal шины признаётся источником подключения.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import hermetic
from _lib import Checks, patched
from mop.cli import lib
from mop.common import bus, config, context, creds


def main():
    c=Checks()
    with tempfile.TemporaryDirectory(prefix='mop-role-ready-653-') as root:
        runtime=Path(root,'bus.json')
        runtime.write_text(json.dumps({'url':'nats://runtime.example:4222',
            'user':'grant-'+'a'*32,'password':'fixture-private'}))
        node=Path(root,'node.env')
        node.write_text('')
        env=dict(os.environ)
        for key in ('MOP_SERVER_LAN','MOP_SERVER_DIR','MOP_BUS_USER'):
            env.pop(key,None)
        env['MOP_BUS_CONFIG']=str(runtime)
        with patch.dict(os.environ,env,clear=True), patched(bus,FILE=str(runtime),_conn=None), \
             patched(config,NODE_ENV_FILE=str(node)), \
             patched(creds,joined_dirs=lambda:[],ROOT=str(Path(root,'no-joined'))), \
             context.isolated(context.Context()), config.client_sources():
            config.forget()
            c.check('#653 actual runtime grant does not require a human joined profile',
                    lib.project_ready('example'))
        with patched(bus,principal=lambda:None), \
             patched(creds,server_dir=lambda:'fixture',snapshot=lambda path:('human',{})):
            c.check('#653 human joined readiness stays available',lib.project_ready('example'))
        with patched(bus,principal=lambda:None), \
             patched(creds,server_dir=lambda:'fixture',snapshot=lambda path:(None,None)):
            c.check('#653 absent human credentials remain not ready',not lib.project_ready('example'))
    return c.report('roleprojectready')


if __name__=='__main__':
    raise SystemExit(main())
