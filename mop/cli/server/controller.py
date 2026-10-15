"""mop server controller <action>: isolated Django controller maintenance

Fresh-install foundation only; never imports old state or contacts HTTP/NATS.

  setup --venv PATH             install the declared server extra in a new venv
  check                         Django system checks and schema readiness
  migrate                       apply standard Django migrations
  migrations-check              refuse uncommitted model changes
  initialize                    bind the empty schema to protected trust roots
  admin --username NAME         create initial admin; password is read privately
  serve --host HOST --port N    production ASGI server (one process)
  worker                        run the bounded worker lifecycle
  backup --file PATH            protected PostgreSQL custom-format dump
  restore --file PATH           restore into an empty DB, reset epoch, disable effects

Every action except setup requires an explicit absolute MOP_CONTROLLER_BOOTSTRAP
file. Maintenance uses its own DB principal with schema/backup rights. Web and
workers use distinct runtime principals without DDL/role/database creation rights;
only maintenance can read the encryption root. Django signing material is a
separate protected file. Supply secrets in these files, never in CLI arguments.
Restore requires stopped services and an empty target; no legacy import exists.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'check', 'migrate', 'migrations-check',
                        'initialize', 'admin', 'serve', 'worker', 'backup', 'restore'))
    parser.add_argument('--venv')
    parser.add_argument('--username')
    parser.add_argument('--file')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args(argv)
    if args.action == 'setup':
        if not args.venv:
            parser.error('setup requires --venv')
        target = Path(args.venv).absolute()
        if target.exists():
            parser.error('setup requires a new venv path')
        subprocess.run([sys.executable, '-m', 'venv', str(target)], check=True)
        source = Path(__file__).resolve().parents[3]
        subprocess.run([str(target / 'bin/python'), '-m', 'pip', 'install',
                        str(source) + '[server]'], check=True)
        return 0
    from mop.server.controller.runtime import configure, maintenance, serve, worker
    configure()
    if args.action == 'serve':
        return serve(args.host, args.port)
    if args.action == 'worker':
        return worker()
    return maintenance(args.action, username=args.username, file=args.file)
