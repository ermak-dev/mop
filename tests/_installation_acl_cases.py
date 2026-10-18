"""Поведенческие ACL-проверки: только отдельные PostgreSQL fixture базы."""
import json
import os
from pathlib import Path
import subprocess
import unittest

import psycopg
from psycopg import sql
from _installation_cases import FreshPostgreSQL as Fixture
from mop.server import installation


# BUG: runtime меняет restore trust; alias LOGIN обходит budget; ACL verify неполон.
# HYPOTHESIS: общий CRUD попадает на trust tables, а verifier смотрит только
# исходящие membership, schema-local defaults и существующие tables, не sequences.
# SOLUTION: trust ACL исключение при schema provisioning и completion restore;
# effective table/column/sequence ACL, объединённые defaults и замкнутый LOGIN scope.
# RESULT: native red на PG16/17; также после restore CREATE Installation снова
# получал общий CRUD (отдельный red), поэтому exception закреплён до ready COMMIT.
# STATUS: FIXED — see #644 (disposable DB gate, не rollout/live installation).
class InstallationACL(unittest.TestCase):
    setUp = Fixture.setUp
    def cleanup_database(self):
        Fixture.cleanup_database(self)
        for name in getattr(self, 'extra_roles', []):
            self.admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))

    provision = Fixture.provision
    connect = Fixture.connect

    def prepare(self):
        self.provision()
        (self.base / 'configuration').mkdir(mode=0o700)
        for name, value in [('root_trust/root.key', b'r' * 64),
                            ('configuration/maintenance/django.key', b'd' * 64)]:
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(value)
            path.chmod(0o600)
        boot = self.base / 'configuration/maintenance/bootstrap.json'
        boot.write_text(json.dumps(installation.bootstrap_document(self.spec, 'maintenance',
                                    password=self.passwords['maintenance'])))
        boot.chmod(0o600)
        spec = self.base / 'spec.json'
        spec.write_text(json.dumps({'installation_id': self.spec.identity,
            'database': dict(self.spec.database), 'paths': dict(self.spec.paths),
            'identities': dict(self.spec.identities)}))
        spec.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('MOP_', 'DJANGO_', 'PG'))}
        source = Path(__file__).resolve().parents[1]
        result = subprocess.run([str(source / 'bin/mop'), 'server', 'install', 'prepare',
            '--spec', str(spec), '--bootstrap', str(boot)], env=env, cwd=self.base,
            capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {**env, 'MOP_CONTROLLER_BOOTSTRAP': str(boot)}

    def assert_refused(self, connection, reason):
        with self.assertRaisesRegex(RuntimeError, reason):
            installation.verify_database(self.spec, connection)

    def alias(self, *, login=True):
        name = self.users['credentials']
        self.admin.execute(sql.SQL('CREATE ROLE {} {} PASSWORD {}').format(
            sql.Identifier(name), sql.SQL('LOGIN' if login else 'NOLOGIN'),
            sql.Literal('isolated-acl-negative-only-644')))
        self.extra_roles = [*getattr(self, 'extra_roles', []), name]
        return name

    def test_restore_authority_is_maintenance_only_and_real_django_locks_work(self):
        env = self.prepare()
        with self.connect('maintenance') as maintenance:
            self.assertTrue(installation.verify_database(self.spec, maintenance)['verified'])
            maintenance.execute("UPDATE controller_restoreguard SET state='restoring' WHERE id=1")
        for role in installation.RUNTIME_ROLES:
            with self.connect(role) as runtime:
                with self.subTest(role=role), runtime.transaction(force_rollback=True), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    runtime.execute("UPDATE controller_restoreguard SET state='ready' WHERE id=1")
                for table, statements in {
                    'controller_restoreguard': ["UPDATE controller_restoreguard SET generation=gen_random_uuid()",
                        'DELETE FROM controller_restoreguard',
                        "INSERT INTO controller_restoreguard VALUES (1, gen_random_uuid(), 'ready')"],
                    'controller_installation': ["UPDATE controller_installation SET root_digest='forged'",
                        'UPDATE controller_installation SET epoch=gen_random_uuid(), effects_enabled=true',
                        'DELETE FROM controller_installation',
                        'INSERT INTO controller_installation SELECT * FROM controller_installation'],
                }.items():
                    for statement in statements:
                        with self.subTest(role=role, statement=statement), runtime.transaction(force_rollback=True), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            runtime.execute(statement)
                    # UPDATE(id) позволяет FOR UPDATE, но singleton constraint запрещает смену id.
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        runtime.execute(sql.SQL('UPDATE {} SET id=2').format(sql.Identifier(table)))
        with self.connect('maintenance') as maintenance:
            self.assertEqual(maintenance.execute('SELECT state FROM controller_restoreguard').fetchone(), ('restoring',))
            maintenance.execute("UPDATE controller_restoreguard SET state='ready' WHERE id=1")
        source = Path(__file__).resolve().parents[1]
        for role in installation.RUNTIME_ROLES:
            boot = self.base / ('bootstrap-' + role + '.json')
            document = installation.bootstrap_document(self.spec, role, password=self.passwords[role], credential_uid=os.geteuid())
            key = Path(document['django_key'])
            key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            key.write_bytes(b'd' * 64)
            key.chmod(0o600)
            boot.write_text(json.dumps(document))
            boot.chmod(0o600)
            code = '''
from mop.server.controller.runtime import configure, short_transaction, require_restore_ready, operation_installation_state, close_pools
configure()
with short_transaction():
    assert require_restore_ready(lock=True)
    assert operation_installation_state().generation
close_pools()
'''
            result = subprocess.run(['python3', '-I', '-c', code], cwd=self.base,
                env={**env, 'MOP_CONTROLLER_BOOTSTRAP': str(boot)}, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_restore_recreation_cannot_reopen_installation_authority_from_defaults(self):
        env = self.prepare()
        source = Path(__file__).resolve().parents[1]
        command = [str(source / 'bin/mop'), 'server', 'controller']
        dump = self.base / 'acl.dump'
        result = subprocess.run([*command, 'backup', '--file', str(dump)], cwd=self.base,
            env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.dbname + '_restore'
        self.admin.execute(sql.SQL('CREATE DATABASE {} OWNER {} CONNECTION LIMIT 32').format(
            sql.Identifier(target), sql.Identifier(self.users['maintenance'])))
        self.addCleanup(lambda: self.admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(target))))
        self.admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(target)))
        db = dict(self.spec.database)

        def connect(role):
            return psycopg.connect(host=db['host'], port=db['port'], dbname=target, user=self.users[role],
                password=self.passwords[role], sslmode='verify-full', sslrootcert=db['tls_ca'],
                autocommit=True, connect_timeout=5)

        with connect('maintenance') as maintenance:
            maintenance.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
            for role in installation.RUNTIME_ROLES:
                user = sql.Identifier(self.users[role])
                maintenance.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(target), user))
                maintenance.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(user))
                maintenance.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}').format(user))
                maintenance.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT USAGE, SELECT ON SEQUENCES TO {}').format(user))
        boot = Path(env['MOP_CONTROLLER_BOOTSTRAP'])
        document = json.loads(boot.read_bytes())
        document['database']['NAME'] = target
        boot.write_text(json.dumps(document))
        result = subprocess.run([*command, 'restore', '--file', str(dump)], cwd=self.base,
            env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        for role in installation.RUNTIME_ROLES:
            with connect(role) as runtime:
                for statement in ["UPDATE controller_restoreguard SET state='restoring'",
                                  "UPDATE controller_installation SET root_digest='forged'"]:
                    with self.subTest(role=role, statement=statement), runtime.transaction(force_rollback=True), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        runtime.execute(statement)

    def test_extra_login_membership_cannot_claim_runtime_24(self):
        self.provision()
        name = self.alias()
        self.admin.execute(sql.SQL('GRANT {} TO {}').format(
            sql.Identifier(self.users['web']), sql.Identifier(name)))
        with self.connect('maintenance') as maintenance:
            self.assert_refused(maintenance, 'login|LOGIN|membership|budget')

    def test_transitive_set_role_even_without_inherit_is_rejected(self):
        self.provision()
        name = self.alias()
        bridge = self.users['builder']
        self.admin.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(bridge)))
        self.extra_roles.append(bridge)
        self.admin.execute(sql.SQL('GRANT {} TO {} WITH INHERIT FALSE, SET TRUE').format(
            sql.Identifier(self.users['worker']), sql.Identifier(bridge)))
        self.admin.execute(sql.SQL('GRANT {} TO {} WITH INHERIT FALSE, SET TRUE').format(
            sql.Identifier(bridge), sql.Identifier(name)))
        with self.connect('maintenance') as maintenance:
            maintenance.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                sql.Identifier(self.dbname), sql.Identifier(name)))
            self.assert_refused(maintenance, 'login|LOGIN|membership|budget')
        db = dict(self.spec.database)
        with psycopg.connect(host=db['host'], port=db['port'], dbname=db['name'], user=name,
                password='isolated-acl-negative-only-644', sslmode='verify-full', sslrootcert=db['tls_ca']) as alias:
            alias.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(self.users['worker'])))
            self.assertEqual(alias.execute('SELECT current_user').fetchone(), (self.users['worker'],))

    def test_direct_unconfigured_runtime_acl_is_rejected_read_only(self):
        self.provision()
        name = self.alias()
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE TABLE direct_acl (id int)')
            maintenance.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                sql.Identifier(self.dbname), sql.Identifier(name)))
            maintenance.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(name)))
            maintenance.execute(sql.SQL('GRANT SELECT, INSERT ON direct_acl TO {}').format(sql.Identifier(name)))
            self.assert_refused(maintenance, 'login|LOGIN|grants|budget')
            self.assertTrue(maintenance.execute("SELECT has_table_privilege(%s, 'direct_acl', 'INSERT')", (name,)).fetchone()[0])

    def test_global_public_future_table_privileges_are_effective(self):
        self.provision()
        with self.connect('maintenance') as maintenance:
            maintenance.execute('ALTER DEFAULT PRIVILEGES GRANT TRUNCATE ON TABLES TO PUBLIC')
            self.assert_refused(maintenance, 'default grants')

    def test_global_role_and_schema_public_defaults_cannot_hide_forbidden_rights(self):
        self.provision()
        with self.connect('maintenance') as maintenance:
            for clause in ['GRANT REFERENCES ON TABLES TO ' + self.users['web'],
                           'IN SCHEMA public GRANT TRIGGER ON TABLES TO PUBLIC']:
                with maintenance.transaction(force_rollback=True):
                    maintenance.execute('ALTER DEFAULT PRIVILEGES ' + clause)
                    self.assert_refused(maintenance, 'default grants')

    def test_global_and_schema_defaults_are_combined_not_checked_separately(self):
        self.provision()
        with self.connect('maintenance') as maintenance:
            maintenance.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                'REVOKE SELECT ON TABLES FROM {}').format(sql.Identifier(self.users['web'])))
            maintenance.execute(sql.SQL('ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {}').format(
                sql.Identifier(self.users['web'])))
            self.assertTrue(installation.verify_database(self.spec, maintenance)['verified'])
            maintenance.execute('CREATE TABLE combined_acl (id bigserial PRIMARY KEY)')
        for role in installation.RUNTIME_ROLES:
            with self.connect(role) as runtime:
                runtime.execute('INSERT INTO combined_acl DEFAULT VALUES')
                self.assertGreater(runtime.execute('SELECT count(*) FROM combined_acl').fetchone()[0], 0)

    def test_existing_sequence_missing_usage_is_rejected(self):
        self.provision()
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE TABLE sequence_acl (id bigserial PRIMARY KEY)')
            maintenance.execute(sql.SQL('REVOKE ALL ON SEQUENCE sequence_acl_id_seq FROM {}').format(
                sql.Identifier(self.users['web'])))
            self.assert_refused(maintenance, 'sequence grants')
        with self.connect('web') as runtime:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                runtime.execute('INSERT INTO sequence_acl DEFAULT VALUES')

    def test_sequence_existing_update_and_public_future_update_are_rejected(self):
        self.provision()
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE TABLE sequence_acl (id bigserial PRIMARY KEY)')
            for statement, reason in [
                ('GRANT UPDATE ON SEQUENCE sequence_acl_id_seq TO PUBLIC', 'sequence grants'),
                ('ALTER DEFAULT PRIVILEGES GRANT UPDATE ON SEQUENCES TO PUBLIC', 'default grants')]:
                with maintenance.transaction(force_rollback=True):
                    maintenance.execute(statement)
                    self.assert_refused(maintenance, reason)

    def test_trust_table_column_or_table_escalation_is_rejected(self):
        self.prepare()
        with self.connect('maintenance') as maintenance:
            for statement in [
                'GRANT UPDATE(state) ON controller_restoreguard TO ' + self.users['web'],
                'GRANT UPDATE ON controller_restoreguard TO PUBLIC',
                'GRANT UPDATE(root_digest) ON controller_installation TO ' + self.users['worker']]:
                with maintenance.transaction(force_rollback=True):
                    maintenance.execute(statement)
                    self.assert_refused(maintenance, 'trust|guard|table grants')

    def test_privileged_flags_owner_and_ddl_remain_negative(self):
        self.provision()
        for flag in ('SUPERUSER', 'CREATEDB', 'CREATEROLE', 'REPLICATION', 'BYPASSRLS'):
            self.admin.execute(sql.SQL('ALTER ROLE {} ' + flag).format(sql.Identifier(self.users['web'])))
            with self.connect('maintenance') as maintenance:
                self.assert_refused(maintenance, 'role privileges')
            self.admin.execute(sql.SQL('ALTER ROLE {} NO' + flag).format(sql.Identifier(self.users['web'])))
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE TABLE owner_negative (id int)')
        parameters = self.admin.info.get_parameters()
        parameters.update(dbname=self.dbname, password='isolated-test-only')
        with psycopg.connect(**parameters, autocommit=True) as admin:
            admin.execute(sql.SQL('ALTER TABLE owner_negative OWNER TO {}').format(sql.Identifier(self.users['web'])))
            with self.connect('maintenance') as maintenance:
                self.assert_refused(maintenance, 'own database objects|table grants')
            admin.execute(sql.SQL('ALTER TABLE owner_negative OWNER TO {}').format(sql.Identifier(self.users['maintenance'])))
            admin.execute(sql.SQL('GRANT CREATE ON SCHEMA public TO {}').format(sql.Identifier(self.users['web'])))
            with self.connect('maintenance') as maintenance:
                self.assert_refused(maintenance, 'schema grants')
            admin.execute(sql.SQL('REVOKE CREATE ON SCHEMA public FROM {}').format(sql.Identifier(self.users['web'])))
        self.admin.execute(sql.SQL('GRANT {} TO {}').format(
            sql.Identifier(self.users['web']), sql.Identifier(self.users['maintenance'])))
        with self.connect('maintenance') as maintenance:
            self.assert_refused(maintenance, 'membership')

    def test_external_verifier_is_read_only_and_rejects_nologin_acl_grantees(self):
        self.provision()
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE TABLE readonly_acl (id bigserial PRIMARY KEY)')
            maintenance.execute('SET default_transaction_read_only=on')
            self.assertTrue(installation.verify_database(self.spec, maintenance)['verified'])
            maintenance.execute('SET default_transaction_read_only=off')
            name = self.alias(login=False)
            maintenance.execute(sql.SQL('GRANT SELECT ON readonly_acl TO {}').format(sql.Identifier(name)))
            before = maintenance.execute("SELECT relacl FROM pg_class WHERE oid='readonly_acl'::regclass").fetchone()
            maintenance.execute('SET default_transaction_read_only=on')
            self.assert_refused(maintenance, 'unconfigured role')
            self.assertEqual(maintenance.execute("SELECT relacl FROM pg_class WHERE oid='readonly_acl'::regclass").fetchone(), before)

    def test_25_runtime_sessions_are_possible_without_alias_rejection(self):
        self.provision()
        name = self.alias()
        self.admin.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(self.users['web']), sql.Identifier(name)))
        db = dict(self.spec.database)
        active = []
        try:
            for _ in range(9):
                active.append(psycopg.connect(host=db['host'], port=db['port'], dbname=db['name'], user=name,
                    password='isolated-acl-negative-only-644', sslmode='verify-full', sslrootcert=db['tls_ca'],
                    autocommit=True, connect_timeout=5))
            for role in ('web', 'worker'):
                active.extend(self.connect(role) for _ in range(8))
            self.assertEqual(len(active), 25)
            for connection in active:
                self.assertTrue(connection.execute("SELECT has_schema_privilege(current_user, 'public', 'USAGE')").fetchone()[0])
            with self.connect('maintenance') as maintenance:
                self.assert_refused(maintenance, 'login|LOGIN|membership|budget')
        finally:
            for connection in active:
                connection.close()
