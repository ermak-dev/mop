"""mop server controller <action>: isolated Django controller maintenance

Fresh-install foundation only; never imports old state or contacts HTTP/NATS.

  setup --venv PATH             install the declared server extra in a new venv
  schema [--app NAME] [--write]  offline migration check; --write generates migrations
  check                         Django system checks and schema readiness
  migrate                       apply standard Django migrations
  migrations-check              refuse uncommitted model changes
  initialize                    bind the empty schema to protected trust roots
  admin --username NAME         create initial admin; password is read privately
  serve --host host --port N    production ASGI server (one process)
    --hostname name             allowed public hostname (repeatable)
    --tls-cert PATH --tls-key PATH  native HTTPS; required for non-loopback bind
  worker                        run the bounded worker lifecycle
  backup --file PATH            protected PostgreSQL custom-format dump
  restore --file PATH           restore into an empty DB, reset epoch, disable effects

Every action except setup and offline schema requires an absolute MOP_CONTROLLER_BOOTSTRAP
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
    parser.add_argument('action', choices=('setup', 'schema', 'check', 'migrate', 'migrations-check',
                        'initialize', 'admin', 'serve', 'worker', 'backup', 'restore'))
    parser.add_argument('--venv')
    parser.add_argument('--username')
    parser.add_argument('--file')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--hostname', action='append', default=[])
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--app', action='append', default=[], choices=('controller', 'catalog', 'secrets'))
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'schema':
        from mop.server.controller.schema import migrations
        output = migrations(apps=args.app, write=args.write)
        if output:
            print(output, end='')
        return 0
    if args.action == 'setup':
        if not args.venv:
            parser.error('setup requires --venv')
        target = Path(args.venv).absolute()
        if target.exists():
            parser.error('setup requires a new venv path')
        subprocess.run([sys.executable, '-m', 'venv', str(target)], check=True)
        source = Path(__file__).resolve().parents[3]
        if not (source / 'pyproject.toml').is_file():
            raise RuntimeError('dependency setup requires a mop source tree')
        subprocess.run([str(target / 'bin/python'), '-m', 'pip', 'install',
                        str(source) + '[server]'], check=True)
        subprocess.run([str(target / 'bin/python'), '-I', '-c',
                        'import django, rest_framework, channels, psycopg, uvicorn'], check=True)
        return 0
    from mop.server.controller.runtime import configure, maintenance, serve, worker, create_admin, close_pools
    configure()
    if args.action == 'admin':
        import getpass
        from django.core.exceptions import ValidationError
        from django.db import DatabaseError
        if not args.username:
            parser.error('admin requires --username')
        try:
            password = getpass.getpass('Initial administrator password: ')
            if password != getpass.getpass('Confirm password: '):
                raise RuntimeError('administrator passwords do not match')
            create_admin(args.username, password)
        except ValidationError as exc:
            raise RuntimeError(f'administrator creation refused: {exc}') from exc
        except (EOFError, DatabaseError) as exc:
            raise RuntimeError(f'administrator creation refused: {type(exc).__name__}') from exc
        finally:
            close_pools()
        return 0
    if args.action == 'serve':
        return serve(args.host, args.port, hostnames=args.hostname,
                     tls_cert=args.tls_cert, tls_key=args.tls_key)
    if args.action == 'worker':
        return worker()
    return maintenance(args.action, username=args.username, file=args.file)
