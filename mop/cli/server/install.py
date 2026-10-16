"""mop server install <action> --spec PATH: independent fresh-install preparation

  plan          validate protected topology and show ordered prerequisites (stdlib)
  provisioning  review OS accounts/directories and explicit PostgreSQL topology
  database      create a new DB and four least-privilege roles over verified TLS
    --provision-bootstrap PATH  separate protected database-only admin document
    --passwords-file PATH       protected JSON of four new DB role passwords
  database-check --provision-bootstrap PATH  verify an external DB as maintenance
  bootstrap --role NAME --password-file PATH  write a new private role bootstrap
  prepare --bootstrap PATH    direct maintenance: migrations then trust initialization
  configure --bootstrap PATH --settings-file PATH --username NAME
                authenticate privately and initialize fresh ORM settings once
  render --bootstrap PATH --username NAME  render ORM desired state; no apply receipt
  retention --keep N [--delete]  preview/apply native dump retention in protected backup dir
  apply         refuse until runtime executors and realtime composition are accepted

The finite JSON specification contains only installation identity, PostgreSQL
16/17 topology, absolute disjoint paths and six distinct OS service identities.
It contains no passwords, root bytes or general settings. No legacy input is
read and no old service is stopped. Plan does not execute provisioning.
Dependencies use mop server controller setup --venv PATH and its declared server
extra; PostgreSQL OS provisioning is an operator action, never an inferred host.
Full installed runtime and host/PVE reboot acceptance is a later integration gate.
"""
import argparse
import json


def main(argv):
    try:
        return _main(argv)
    except (ValueError, PermissionError) as error:
        raise RuntimeError(str(error)) from None


def _main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'provisioning', 'database', 'database-check', 'bootstrap',
                        'prepare', 'configure', 'render', 'retention', 'apply'))
    parser.add_argument('--spec', required=True)
    parser.add_argument('--provision-bootstrap')
    parser.add_argument('--passwords-file')
    parser.add_argument('--password-file')
    parser.add_argument('--role', choices=('maintenance', 'web', 'worker', 'gateway'))
    parser.add_argument('--bootstrap')
    parser.add_argument('--settings-file')
    parser.add_argument('--username')
    parser.add_argument('--keep', type=int, default=7)
    parser.add_argument('--delete', action='store_true')
    args = parser.parse_args(argv)
    from mop.server import installation as install
    from mop.server.controller.bootstrap import private_bytes
    spec = install.load_specification(args.spec)
    if args.action == 'apply':
        install.require_runtime_integration()
    if args.action == 'plan':
        print(json.dumps(install.plan(spec), sort_keys=True))
    elif args.action == 'provisioning':
        print(json.dumps(install.provisioning_artifact(spec), sort_keys=True))
    elif args.action == 'retention':
        result = install.retain_backups(dict(spec.paths)['backup'], keep=args.keep, apply=args.delete)
        if not args.delete:
            print(json.dumps(result, sort_keys=True))
    elif args.action in ('database', 'database-check'):
        if not args.provision_bootstrap:
            parser.error(args.action + ' requires --provision-bootstrap')
        from mop.server.install_postgresql import connect_provisioner
        if args.action == 'database-check':
            with connect_provisioner(spec, args.provision_bootstrap) as connection:
                install.verify_database(spec, connection)
        else:
            if not args.passwords_file:
                parser.error('database requires --passwords-file')
            try:
                passwords = json.loads(private_bytes(args.passwords_file))
            except (ValueError, UnicodeError):
                raise RuntimeError('database password document is not valid JSON') from None
            with connect_provisioner(spec, args.provision_bootstrap) as admin:
                install.provision_database(spec, admin, passwords=passwords)
    elif args.action == 'bootstrap':
        if not args.role or not args.password_file:
            parser.error('bootstrap requires --role and --password-file')
        import pwd
        import os
        if pwd.getpwuid(os.geteuid()).pw_name != dict(spec.identities)[args.role]:
            raise RuntimeError('bootstrap must run as its declared OS service identity')
        try:
            password = private_bytes(args.password_file).decode('ascii').rstrip('\n')
        except UnicodeError:
            raise RuntimeError('database password file requires ascii text') from None
        document = install.bootstrap_document(spec, args.role, password=password)
        destination = dict(spec.paths)['configuration'] + '/' + args.role + '/bootstrap.json'
        # Проверяем ключи до записи; signing key копирует защищённый OS provisioner,
        # не runtime и не root из HTTP. Существующие bytes никогда не заменяются.
        from mop.server.controller.bootstrap import private_bytes as read_key
        signing = read_key(document['django_key'])
        if len(signing) < 50 or not signing.isascii():
            raise RuntimeError('Django signing key is not provisioned as at least 50 ascii bytes')
        if args.role == 'maintenance' and len(read_key(document['root_key'])) < 32:
            raise RuntimeError('separate root trust is not provisioned')
        install.write_private_artifact(destination, json.dumps(document).encode())
    else:
        if not args.bootstrap:
            parser.error(args.action + ' requires --bootstrap')
        import os
        from mop.server.controller.bootstrap import load
        bootstrap = load(args.bootstrap)
        expected = dict(spec.database)
        db = bootstrap['database']
        if bootstrap['role'] != 'maintenance' or bootstrap['installation_id'] != spec.identity or \
                (db['HOST'], db['PORT'], db['NAME'], db['USER']) != (expected['host'], str(expected['port']),
                    expected['name'], dict(spec.identities)['maintenance']) or \
                db['OPTIONS'].get('sslmode') != 'verify-full' or db['OPTIONS'].get('sslrootcert') != expected['tls_ca']:
            raise RuntimeError('maintenance bootstrap does not match the fresh installation')
        os.environ['MOP_CONTROLLER_BOOTSTRAP'] = args.bootstrap
        from mop.server.controller.runtime import configure, close_pools
        configure()
        try:
            if args.action == 'prepare':
                from django.db import connection
                connection.ensure_connection()
                install.verify_database(spec, connection.connection)
                install.prepare_database()
            else:
                if not args.username:
                    parser.error(args.action + ' requires --username')
                import getpass
                from django.contrib.auth import authenticate
                from mop.server.access.services import token_context, issue_token
                user = authenticate(username=args.username, password=getpass.getpass('Administrator password: '),
                                    remote_address='local')
                if user is None:
                    raise RuntimeError('maintenance authentication refused')
                actor = token_context(issue_token(user.pk))
                if args.action == 'configure':
                    if not args.settings_file:
                        parser.error('configure requires --settings-file')
                    try:
                        values = json.loads(private_bytes(args.settings_file))
                    except (ValueError, UnicodeError):
                        raise RuntimeError('initial settings document is not valid JSON') from None
                    install.initialize_configuration(actor, values)
                else:
                    print(json.dumps(install.render_catalog(actor), sort_keys=True))
        finally:
            close_pools()
    return 0
