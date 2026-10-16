"""Установочный PostgreSQL adapter: инфраструктурный SQL, не предметные queries."""
import base64
import hashlib
import hmac
import json
import secrets

from .controller.bootstrap import private_bytes


def _scram(password):
    """В server statement/log попадает verifier, а не cleartext password."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 4096)
    client = hmac.digest(salted, b'Client Key', 'sha256')
    stored, server = hashlib.sha256(client).digest(), hmac.digest(salted, b'Server Key', 'sha256')
    encode = lambda value: base64.b64encode(value).decode('ascii')
    return 'SCRAM-SHA-256$4096:' + encode(salt) + '$' + encode(stored) + ':' + encode(server)


def provision(spec, admin, *, passwords):
    """Только явно новая БД. Ошибка после CREATE DATABASE не откатывает внешний мир."""
    import psycopg
    from psycopg import sql
    from .installation import RUNTIME_ROLES, bootstrap_document
    db, identities = dict(spec.database), dict(spec.identities)
    roles = (*RUNTIME_ROLES, 'maintenance')
    if type(passwords) is not dict or set(passwords) != set(roles):
        raise ValueError('database provisioning requires four distinct role passwords')
    for role in roles:
        bootstrap_document(spec, role, password=passwords[role])
        if len(passwords[role]) < 24 or not passwords[role].isascii():
            raise ValueError('database role passwords require at least 24 random ASCII characters')
    if len(set(passwords.values())) != len(roles):
        raise ValueError('database roles require distinct credentials')
    if not admin.autocommit or admin.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError('database provisioning requires an idle autocommit connection')
    parameters = admin.info.get_parameters()
    if not admin.pgconn.ssl_in_use or parameters.get('sslmode') != 'verify-full' or \
            parameters.get('sslrootcert') != db['tls_ca']:
        raise RuntimeError('database provisioning requires verified installation TLS trust')
    if admin.info.host != db['host'] or admin.info.port != db['port']:
        raise RuntimeError('database provisioning connection does not match explicit topology')
    if admin.info.user in identities.values() or admin.info.dbname == db['name']:
        raise RuntimeError('provisioning authority must be separate from target database identities')
    if admin.info.server_version // 10000 != db['major']:
        raise RuntimeError('database server major does not match installation')
    capacity, reserved = admin.execute("SELECT current_setting('max_connections')::int, "
        "current_setting('superuser_reserved_connections')::int + "
        "coalesce(nullif(current_setting('reserved_connections', true), ''), '0')::int").fetchone()
    if capacity - reserved < 32:
        raise RuntimeError('PostgreSQL lacks 24 runtime plus 8 maintenance connection capacity')
    if admin.execute('SELECT 1 FROM pg_database WHERE datname=%s', (db['name'],)).fetchone():
        raise RuntimeError('installation database already exists; use maintenance, never reinitialize')
    names = [identities[role] for role in roles]
    if admin.execute('SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)', (names,)).fetchone():
        raise RuntimeError('installation database role already exists; explicit maintenance required')
    # Один advisory lock на имя БД сериализует установщиков, но повторная проверка
    # внутри lock обязательна. Hash lock не является auth/ownership доказательством.
    lock = int.from_bytes(hashlib.sha256(db['name'].encode()).digest()[:8], 'big', signed=True)
    admin.execute('SELECT pg_advisory_lock(%s)', (lock,))
    created = False
    try:
        if admin.execute('SELECT 1 FROM pg_database WHERE datname=%s', (db['name'],)).fetchone() or \
                admin.execute('SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)', (names,)).fetchone():
            raise RuntimeError('installation database or role already exists; explicit maintenance required')
        with admin.transaction():
            for role in roles:
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                    'NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8 PASSWORD {}').format(
                        sql.Identifier(identities[role]), sql.Literal(_scram(passwords[role]))))
            created = True
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER {} CONNECTION LIMIT 32').format(
            sql.Identifier(db['name']), sql.Identifier(identities['maintenance'])))
        admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(db['name'])))
        for role in roles:
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                sql.Identifier(db['name']), sql.Identifier(identities[role])))
        # Maintenance — schema owner; внешняя admin identity не сохраняется в БД
        # и не нужна runtime. Подключение к target не требует HTTP/NATS.
        with psycopg.connect(host=db['host'], port=db['port'], dbname=db['name'],
                user=identities['maintenance'], password=passwords['maintenance'],
                sslmode='verify-full', sslrootcert=db['tls_ca'], connect_timeout=5) as target:
            target.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
            for role in RUNTIME_ROLES:
                user = sql.Identifier(identities[role])
                target.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(user))
                target.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}').format(user))
                target.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT USAGE, SELECT ON SEQUENCES TO {}').format(user))
    except psycopg.Error as error:
        # libpq/server error может содержать SQL и verifier. Ни input, ни raw
        # traceback не выходят наружу; уже созданные ресурсы не удаляются молча.
        reason = 'database provisioning failed: ' + type(error).__name__
        if created:
            reason += '; partial database/roles retained; explicit maintenance required'
        raise RuntimeError(reason) from None
    finally:
        admin.execute('SELECT pg_advisory_unlock(%s)', (lock,))
    return {'database_created': True, 'services_started': False, 'runtime_connections': 24,
            'maintenance_connections': 8, 'schema_owner': identities['maintenance']}


def verify(spec, connection):
    """Read-only acceptance внешней БД под maintenance, без CREATE ROLE/DB."""
    import psycopg
    from .installation import RUNTIME_ROLES
    db, identities = dict(spec.database), dict(spec.identities)
    parameters = connection.info.get_parameters()
    if not connection.pgconn.ssl_in_use or parameters.get('sslmode') != 'verify-full' or \
            parameters.get('sslrootcert') != db['tls_ca']:
        raise RuntimeError('database verification requires installation TLS trust')
    if (connection.info.host, connection.info.port, connection.info.dbname, connection.info.user) != \
            (db['host'], db['port'], db['name'], identities['maintenance']):
        raise RuntimeError('database verification requires the explicit target maintenance identity')
    if connection.info.server_version // 10000 != db['major']:
        raise RuntimeError('database verification requires the configured PostgreSQL major')
    try:
        owner, limit = connection.execute('SELECT pg_get_userbyid(datdba), datconnlimit '
            'FROM pg_database WHERE datname=current_database()').fetchone()
        if owner != identities['maintenance'] or limit != 32:
            raise RuntimeError('database owner or instance connection budget differs from installation')
        for role in (*RUNTIME_ROLES, 'maintenance'):
            name = identities[role]
            row = connection.execute('SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, '
                'rolbypassrls, rolconnlimit, rolcanlogin FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
            if row is None or row[1:] != (False, False, False, False, False, 8, True):
                raise RuntimeError('database role privileges or connection budget differ from installation')
            oid = row[0]
            if connection.execute('SELECT 1 FROM pg_auth_members WHERE member=%s', (oid,)).fetchone():
                raise RuntimeError('database role has unsupported inherited or SET ROLE membership')
            if role == 'maintenance':
                continue
            privileges = connection.execute('SELECT has_database_privilege(%s, current_database(), \'CONNECT\'), '
                'has_database_privilege(%s, current_database(), \'CREATE\'), '
                'has_database_privilege(%s, current_database(), \'TEMP\'), '
                'has_schema_privilege(%s, \'public\', \'USAGE\'), '
                'has_schema_privilege(%s, \'public\', \'CREATE\')', (name,) * 5).fetchone()
            if privileges != (True, False, False, True, False):
                raise RuntimeError('runtime database/schema grants are not least privilege')
            if connection.execute('SELECT 1 FROM pg_class WHERE relowner=%s UNION ALL '
                    'SELECT 1 FROM pg_namespace WHERE nspowner=%s', (oid, oid)).fetchone():
                raise RuntimeError('runtime identity must not own database objects')
            table_checks = []
            for privilege in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'):
                table_checks.append((privilege, False))
            for privilege in ('TRUNCATE', 'REFERENCES', 'TRIGGER'):
                table_checks.append((privilege, True))
            for privilege, forbidden in table_checks:
                wrong = connection.execute('SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                    'WHERE n.nspname=\'public\' AND c.relkind IN (\'r\', \'p\') '
                    'AND has_table_privilege(%s, c.oid, %s)=%s LIMIT 1', (name, privilege, forbidden)).fetchone()
                if wrong:
                    raise RuntimeError('runtime existing table grants are not least privilege')
            for kind, expected in (('r', {'SELECT', 'INSERT', 'UPDATE', 'DELETE'}), ('S', {'USAGE', 'SELECT'})):
                rights = connection.execute('SELECT a.privilege_type FROM pg_default_acl d '
                    'CROSS JOIN LATERAL aclexplode(d.defaclacl) a '
                    'WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=%s) '
                    'AND d.defaclnamespace=\'public\'::regnamespace AND d.defaclobjtype=%s AND a.grantee=%s',
                    (identities['maintenance'], kind, oid)).fetchall()
                if {value[0] for value in rights} != expected:
                    raise RuntimeError('runtime default grants are not least privilege for future migrations')
    except psycopg.Error as error:
        raise RuntimeError('database verification unavailable: ' + type(error).__name__) from None
    return {'verified': True, 'runtime_connections': 24, 'maintenance_connections': 8,
            'services_started': False}


def connect_provisioner(spec, path):
    """Отдельный protected документ только с database; root/signing ему не нужны."""
    import psycopg
    from .controller.bootstrap import validate_database
    try:
        document = json.loads(private_bytes(path))
    except (ValueError, UnicodeError):
        raise ValueError('provisioning database document is not valid JSON') from None
    validate_database(document)
    db = dict(spec.database)
    if document['OPTIONS'].get('sslmode') != 'verify-full' or document['OPTIONS'].get('sslrootcert') != db['tls_ca']:
        raise ValueError('provisioning requires explicit installation TLS trust')
    if (document['HOST'], document['PORT']) != (db['host'], str(db['port'])):
        raise ValueError('provisioning database coordinates differ from installation topology')
    try:
        return psycopg.connect(host=document['HOST'], port=document['PORT'], dbname=document['NAME'],
            user=document['USER'], password=document['PASSWORD'], **document['OPTIONS'],
            connect_timeout=5, autocommit=True)
    except psycopg.Error as error:
        raise RuntimeError('provisioning database unavailable: ' + type(error).__name__) from None
