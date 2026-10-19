"""mop server install <action> --spec PATH: independent fresh-install preparation

  plan          validate protected topology and show ordered prerequisites (stdlib)
  provisioning  review OS accounts/directories and explicit PostgreSQL topology
    --apply     prepare a new local Debian host/PVE guest (sudo, external DB only)
  database      create a new DB and five least-privilege roles over verified TLS
    --provision-bootstrap PATH  separate protected database-only admin document
    --passwords-file PATH       protected JSON: web, worker, gateway, credential, maintenance
  database-check --provision-bootstrap PATH  verify an external DB as maintenance
  bootstrap --role NAME --password-file PATH  write a new private role bootstrap
  prepare --bootstrap PATH    direct maintenance: migrations then trust initialization
  configure --bootstrap PATH --settings-file PATH --username NAME
                authenticate privately and initialize fresh ORM settings once
  render --bootstrap PATH --username NAME  render ORM desired state and service units; no apply receipt
  retention --keep N [--delete]  preview/apply native dump retention in protected backup dir
  apply         refuse until runtime executors and realtime composition are accepted

The finite JSON specification contains only installation identity, PostgreSQL
topology, absolute disjoint paths, the declared runtime and six distinct OS
service identities. The runtime block names the python requirement, the private
venv, the system apt packages and the pip extra; the system python must satisfy
it and is never downgraded. Acceptance covers the >=3.12 requirement on
PostgreSQL 17: Python 3.12 in the owned CI fixture and Debian 13's 3.13.
It contains no passwords, root bytes or general settings. No legacy input is
read and no old service is stopped. Plan does not execute provisioning.
Provisioning installs the declared system packages from the image and then
creates the venv itself: system python, the declared server extra, offline
wheels when the spec names them, and an import check for django, rest_framework,
channels, psycopg, uvicorn, ldap and django_auth_ldap. `mop server controller
setup --venv PATH` is the same idempotent step for manual maintenance.
Explicit provisioning --apply prepares only fresh local accounts, private
roots, signing material and matching PostgreSQL client tools, then the venv.
It refuses existing trees/accounts and provisioned-DB topology: PostgreSQL server
TLS/network provisioning needs its own explicit operator input. It starts no service,
does not connect to the DB, does not invoke PVE and never claims runtime readiness.
The same local command can run inside a host or an explicitly created PVE guest.
Rendered units are review artifacts. Credential composition resolves the declared
credentials OS account and exact web/worker peer UIDs; its bootstrap purpose is
credential. Worker/realtime bindings and native lifecycle acceptance remain gated.
The separate credential pool adds 8 connections to 24 ordinary plus 8 maintenance;
PostgreSQL needs 40 ordinary slots plus its reserved capacity. No unit is installed
or started by rendering. Full runtime and host/PVE reboot acceptance is a later gate.
"""
import argparse
import json


def provision_target():
    """Привилегированный stdlib frontend того же installer, без другого commandlet."""
    import subprocess
    import sys
    from mop.server.install_provision import prepare
    try:
        prepare(json.load(sys.stdin))
    except (ValueError, RuntimeError) as error:
        print(json.dumps({'error': str(error)}))
        return 1
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({'error': 'OS preparation unavailable; partial resources retained; explicit maintenance required'}))
        return 1
    return 0


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
    parser.add_argument('--role', choices=('maintenance', 'credential', 'web', 'worker', 'gateway'))
    parser.add_argument('--bootstrap')
    parser.add_argument('--settings-file')
    parser.add_argument('--username')
    parser.add_argument('--keep', type=int, default=7)
    parser.add_argument('--delete', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.apply and args.action != 'provisioning':
        parser.error('--apply is only valid with provisioning')
    from mop.server import installation as install
    from mop.server.controller.bootstrap import private_bytes
    spec = install.load_specification(args.spec)
    if args.action == 'apply':
        install.require_runtime_integration()
    if args.action == 'plan':
        print(json.dumps(install.plan(spec), sort_keys=True))
    elif args.action == 'provisioning':
        if args.apply:
            from mop.server.install_provision import apply
            apply(spec, apply=True)
        else:
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
        if pwd.getpwuid(os.geteuid()).pw_name != install.database_identity(spec, args.role):
            raise RuntimeError('bootstrap must run as its declared OS service identity')
        try:
            password = private_bytes(args.password_file).decode('ascii').rstrip('\n')
        except UnicodeError:
            raise RuntimeError('database password file requires ascii text') from None
        credential_uid = None
        if args.role in install.CREDENTIAL_CLIENT_ROLES:
            try:
                credential_uid = pwd.getpwnam(dict(spec.identities)['credentials']).pw_uid
            except KeyError:
                raise RuntimeError('credential service OS identity is not provisioned') from None
        document = install.bootstrap_document(spec, args.role, password=password, credential_uid=credential_uid)
        directory = 'credentials' if args.role == 'credential' else args.role
        destination = dict(spec.paths)['configuration'] + '/' + directory + '/bootstrap.json'
        # Проверяем ключи до записи; signing key копирует защищённый OS provisioner,
        # не runtime и не root из HTTP. Существующие bytes никогда не заменяются.
        from mop.server.controller.bootstrap import private_bytes as read_key
        signing = read_key(document['django_key'])
        if len(signing) < 50 or not signing.isascii():
            raise RuntimeError('Django signing key is not provisioned as at least 50 ascii bytes')
        if args.role in ('maintenance', 'credential') and len(read_key(document['root_key'])) < 32:
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
                    print(json.dumps(install.render_catalog(actor, spec=spec), sort_keys=True))
        finally:
            close_pools()
    return 0
