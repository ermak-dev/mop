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
    from .installation import (DATABASE_RUNTIME_ROLES, DATABASE_ROLES, DATABASE_CONNECTIONS,
                               CREDENTIAL_CONNECTIONS, database_password, database_identity)
    db = dict(spec.database)
    roles = DATABASE_ROLES
    identities = {role: database_identity(spec, role) for role in roles}
    if type(passwords) is not dict or set(passwords) != set(roles):
        raise ValueError('database provisioning requires five distinct role passwords')
    for role in roles:
        database_password(passwords[role])
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
    if capacity - reserved < DATABASE_CONNECTIONS:
        raise RuntimeError('PostgreSQL lacks 24 ordinary runtime plus 8 credential plus 8 maintenance connection capacity')
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
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER {} CONNECTION LIMIT {}').format(
            sql.Identifier(db['name']), sql.Identifier(identities['maintenance']), sql.Literal(DATABASE_CONNECTIONS)))
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
            for role in DATABASE_RUNTIME_ROLES:
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
            'credential_connections': CREDENTIAL_CONNECTIONS, 'total_connections': DATABASE_CONNECTIONS,
            'maintenance_connections': 8, 'connection_scope': 'managed-target-logins',
            'schema_owner': identities['maintenance']}


def verify(spec, connection):
    """Read-only acceptance внешней БД под maintenance, без CREATE ROLE/DB."""
    import psycopg
    from .installation import (DATABASE_RUNTIME_ROLES, DATABASE_ROLES, DATABASE_CONNECTIONS,
                               CREDENTIAL_CONNECTIONS, database_identity)
    db = dict(spec.database)
    identities = {role: database_identity(spec, role) for role in DATABASE_ROLES}
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
        if owner != identities['maintenance'] or limit != DATABASE_CONNECTIONS:
            raise RuntimeError('database owner or instance connection budget differs from installation')
        for role in DATABASE_ROLES:
            name = identities[role]
            row = connection.execute('SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, '
                'rolbypassrls, rolconnlimit, rolcanlogin FROM pg_roles WHERE rolname=%s', (name,)).fetchone()
            if row is None or row[1:] != (False, False, False, False, False, 8, True):
                raise RuntimeError('database role privileges or connection budget differ from installation')
            oid = row[0]
            # Запрет обоих концов покрывает и транзитивные пути: любой путь к
            # configured role обязан иметь последнее входящее ребро. Не пытаемся
            # считать membership как LOGIN limit; INHERIT FALSE не запрещает SET.
            if connection.execute('SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s',
                                  (oid, oid)).fetchone():
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
            _verify_relations(connection, name, identities['maintenance'])
            _verify_defaults(connection, oid, identities['maintenance'],
                             [identities[role] for role in DATABASE_RUNTIME_ROLES])
        _verify_login_scope(connection, [identities[role] for role in DATABASE_ROLES])
    except psycopg.Error as error:
        raise RuntimeError('database verification unavailable: ' + type(error).__name__) from None
    return {'verified': True, 'runtime_connections': 24, 'credential_connections': CREDENTIAL_CONNECTIONS,
            'maintenance_connections': 8, 'total_connections': DATABASE_CONNECTIONS,
            'connection_scope': 'managed-target-logins', 'services_started': False}


TRUST_TABLES = frozenset({'controller_restoreguard', 'controller_installation'})
TABLE_RIGHTS = frozenset({'SELECT', 'INSERT', 'UPDATE', 'DELETE'})
SEQUENCE_RIGHTS = frozenset({'USAGE', 'SELECT'})


def seal_trust_tables(connection):
    """Явное schema/restore provisioning; verifier никогда не вызывает этот путь."""
    from psycopg import sql
    with connection.cursor() as cursor:
        for table in sorted(TRUST_TABLES):
            # Table и column ACL независимы: REVOKE ALL ON TABLE не снимает
            # ранее выданный UPDATE(state). Owner сохраняет maintenance authority.
            cursor.execute('WITH recipients AS ('
                'SELECT c.relowner, a.grantee FROM pg_class c '
                'CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.oid=%s::regclass UNION ALL '
                'SELECT c.relowner, a.grantee FROM pg_class c JOIN pg_attribute col ON col.attrelid=c.oid '
                'CROSS JOIN LATERAL aclexplode(col.attacl) a WHERE c.oid=%s::regclass) '
                'SELECT DISTINCT a.grantee, r.rolname FROM recipients a '
                'LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE a.grantee<>a.relowner',
                ('public.' + table,) * 2)
            recipients = cursor.fetchall()
            cursor.execute('SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass '
                           'AND attnum>0 AND NOT attisdropped', ('public.' + table,))
            columns = sql.SQL(', ').join(sql.Identifier(row[0]) for row in cursor.fetchall())
            for oid, name in recipients:
                role = sql.Identifier(name) if oid else sql.SQL('PUBLIC')
                target = sql.Identifier('public', table)
                cursor.execute(sql.SQL('REVOKE ALL ON TABLE {} FROM {}').format(target, role))
                for privilege in ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES'):
                    cursor.execute(sql.SQL('REVOKE {} ({}) ON TABLE {} FROM {}').format(
                        sql.SQL(privilege), columns, target, role))
                cursor.execute(sql.SQL('GRANT SELECT, UPDATE (id) ON TABLE {} TO {}').format(target, role))


def _verify_relations(connection, name, owner):
    """Проверяем effective ACL, в том числе PUBLIC и column grants."""
    tables = connection.execute("SELECT c.oid, c.relname, pg_get_userbyid(c.relowner), c.relkind "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')").fetchall()
    for oid, table, table_owner, kind in tables:
        sequence = kind == 'S'
        trust = not sequence and table in TRUST_TABLES
        label = 'sequence grants' if sequence else 'trust table grants' if trust else 'table grants'
        expected = SEQUENCE_RIGHTS if sequence else {'SELECT'} if trust else TABLE_RIGHTS
        privileges = ['USAGE', 'SELECT', 'UPDATE'] if sequence else [
            'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']
        if not sequence and connection.info.server_version >= 170000:
            privileges.append('MAINTAIN')
        check = 'has_sequence_privilege' if sequence else 'has_table_privilege'
        rights = connection.execute('SELECT p, ' + check + '(%s, %s, p), ' + check +
            "(%s, %s, p || ' WITH GRANT OPTION') FROM unnest(%s::text[]) p",
            (name, oid, name, oid, privileges)).fetchall()
        if kind not in ('r', 'p', 'S'):
            column_access = connection.execute('SELECT bool_or(has_any_column_privilege(%s, %s, p)) '
                'FROM unnest(%s::text[]) p', (name, oid, ['SELECT', 'INSERT', 'UPDATE', 'REFERENCES'])).fetchone()[0]
            if column_access or any(enabled or grantable for _, enabled, grantable in rights):
                raise RuntimeError('runtime grants expose an unsupported relation')
            continue
        if table_owner != owner or {p for p, enabled, _ in rights if enabled} != expected or any(
                grantable for _, _, grantable in rights):
            raise RuntimeError('runtime existing ' + label + ' are not least privilege')
        if sequence:
            continue
        columns = connection.execute('SELECT a.attname, p, has_column_privilege(%s, a.attrelid, a.attnum, p), '
            "has_column_privilege(%s, a.attrelid, a.attnum, p || ' WITH GRANT OPTION') "
            "FROM pg_attribute a CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES']) p "
            'WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped', (name, name, oid)).fetchall()
        for column, privilege, enabled, grantable in columns:
            allowed = privilege == 'SELECT' or (privilege == 'UPDATE' and column == 'id') if trust else privilege in TABLE_RIGHTS
            if enabled != allowed or grantable:
                raise RuntimeError('runtime existing ' + label + ' have unsafe column privileges')


def _verify_defaults(connection, oid, owner, names):
    """Global и schema-local ACL складываются; ни один из них не вычитает другой."""
    for kind, expected in (('r', TABLE_RIGHTS), ('S', SEQUENCE_RIGHTS)):
        rights = connection.execute('SELECT a.grantee, r.rolname, a.privilege_type, a.is_grantable '
            'FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a '
            'LEFT JOIN pg_roles r ON r.oid=a.grantee '
            'WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=%s) '
            "AND d.defaclnamespace IN (0, 'public'::regnamespace) AND d.defaclobjtype=%s",
            (owner, kind)).fetchall()
        effective = {privilege for grantee, _, privilege, _ in rights if grantee in (0, oid)}
        if effective != expected or any(grantable or grantee == 0 for grantee, _, _, grantable in rights
                                        if grantee in (0, oid)):
            raise RuntimeError('runtime default grants are not least privilege for future migrations')
        # Дополнительный NOLOGIN grantee тоже нельзя оставить будущим входом в ACL.
        if any(grantee != owner and grantee not in names for _, grantee, _, _ in rights):
            raise RuntimeError('runtime default grants include an unconfigured role')


def _verify_login_scope(connection, names):
    """Только обычные LOGIN target DB; внешние superusers и другие БД вне budget."""
    if connection.execute('SELECT 1 FROM pg_roles WHERE rolcanlogin AND NOT rolsuper '
            "AND rolname<>ALL(%s) AND has_database_privilege(oid, current_database(), 'CONNECT') LIMIT 1",
            (names,)).fetchone():
        raise RuntimeError('database has an unconfigured LOGIN outside the managed connection budget')
    # Не только роли с CONNECT сегодня: NOLOGIN ACL может стать доступен через
    # membership завтра. PUBLIC и неописанные grantees не допускаются.
    grants = connection.execute("WITH scoped(acl) AS ("
        'SELECT datacl FROM pg_database WHERE datname=current_database() UNION ALL '
        "SELECT nspacl FROM pg_namespace WHERE nspname='public' UNION ALL "
        "SELECT c.relacl FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' UNION ALL "
        'SELECT a.attacl FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid '
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public') "
        'SELECT a.grantee, r.rolname, a.is_grantable FROM scoped '
        'CROSS JOIN LATERAL aclexplode(acl) a LEFT JOIN pg_roles r ON r.oid=a.grantee').fetchall()
    for oid, grantee, grantable in grants:
        if not oid or grantee not in (*names, 'pg_database_owner') or (grantable and grantee != names[-1]):
            raise RuntimeError('database grants include an unconfigured role or grant option')


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
