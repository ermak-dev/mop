"""mop server controller <action>: isolated Django controller maintenance

Fresh-install foundation only; never imports old state or contacts HTTP/NATS.

  setup --venv PATH             create or reuse the venv and install the declared server extra
  schema [--app NAME] [--write]  offline migration check; --write generates migrations
  check                         Django system checks and schema readiness
  migrate                       apply standard Django migrations
  migrations-check              refuse uncommitted model changes
  initialize                    bind the empty schema to protected trust roots
  collectstatic                 collect Admin static into the bootstrap static_root
  admin --username NAME         create initial admin; password is read privately
  serve --host host --port N    production ASGI server (one process)
    --hostname name             allowed public hostname (repeatable)
    --tls-cert PATH --tls-key PATH  native HTTPS; required for non-loopback bind
  worker                        run the bounded worker lifecycle
  backup --file PATH            protected PostgreSQL custom-format dump
  restore --file PATH           restore into an empty DB, reset epoch, disable effects
  enroll-node --node UUID --origin HTTPS_ORIGIN --ca-file NODE_CA_PATH
    --file NEW_PRIVATE_FOLDER/bootstrap.json --node-file NODE_OS_PATH
    [--enrollment UUID]         export a node service transport; exact live reexport only

Every action except setup and offline schema requires an absolute MOP_CONTROLLER_BOOTSTRAP
file. Maintenance uses its own DB principal with schema/backup rights. Web and
workers use distinct runtime principals without DDL/role/database creation rights;
only maintenance and the isolated credential service receive the root. Secret
use cases run in `mop server credentials`, never web/worker. Django signing material is a
separate protected file. Supply secrets in these files, never in CLI arguments.
Restore requires stopped services and an empty target; no legacy import exists.
"""
import argparse
import os
from pathlib import Path


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'schema', 'check', 'migrate', 'migrations-check',
                        'initialize', 'collectstatic', 'admin', 'serve', 'worker', 'backup', 'restore', 'enroll-node'))
    parser.add_argument('--venv')
    parser.add_argument('--username')
    parser.add_argument('--file')
    parser.add_argument('--node')
    parser.add_argument('--origin')
    parser.add_argument('--ca-file')
    parser.add_argument('--node-file')
    parser.add_argument('--enrollment')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--hostname', action='append', default=[])
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--app', action='append', default=[], choices=('actor', 'controller', 'catalog', 'secrets', 'access', 'preparation', 'operations', 'execution', 'builds', 'gateway'))
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
        # Тот же шаг, что зовёт установка (#807): venv вручную больше не
        # расходятся с установленным.
        from mop.server.install_runtime import create_venv
        create_venv(target, pip_extra='server')
        return 0
    from mop.server.controller.runtime import configure, maintenance, serve, worker, create_admin, close_pools
    if args.action == 'enroll-node':
        from uuid import UUID
        if not all((args.node, args.origin, args.ca_file, args.file, args.node_file)):
            parser.error('enroll-node requires --node, --origin, --ca-file, --file and --node-file')
        try:
            node = UUID(args.node)
            enrollment = UUID(args.enrollment) if args.enrollment else None
        except ValueError:
            parser.error('enroll-node requires node and enrollment UUIDs')
        from mop.server.controller.runtime import enroll_node
        try:
            configure()
            enroll_node(node_id=node, origin=args.origin, ca_file=args.ca_file,
                        file=args.file, node_file=args.node_file, enrollment_id=enrollment)
        finally:
            close_pools()
        return 0
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
