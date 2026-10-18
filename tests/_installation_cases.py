"""Нативный PostgreSQL: fresh DDL, ограниченные роли и отдельные root/DB files."""
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
import uuid

from django.conf import settings
import psycopg
from psycopg import sql

from mop.server import installation

# FEATURE: fresh install PostgreSQL и разрешённая offline maintenance граница.
# HYPOTHESIS: текущий bootstrap требует готовых DB roles; installer их не создаёт.
# SPEC: новая отдельная БД, verify-full, runtime не owner/superuser, 24+8 на instance.
# SOLUTION: отдельный PostgreSQL adapter создаёт новые least-privilege roles;
# роль ограничивает все соединения/процессы, не только один Python executor.
# RESULT: clean server extra, native TLS/bootstrap/migrations/admin/config,
# native dump/restore fence и отрицательные grants/connection checks в owned Docker.
# STATUS: FIXED — see #644 (не live-host/runtime acceptance).
class FreshPostgreSQL(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mop-install-db-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        ca = self.base / 'postgres-ca.pem'
        ca.write_bytes((Path(__file__).resolve().parents[1] / '.test-postgres-ca.pem').read_bytes())
        ca.chmod(0o644)
        suffix = uuid.uuid4().hex[:12]
        self.dbname = 'fresh_' + suffix
        self.users = {role: 'mi_' + role + '_' + suffix for role in installation.IDENTITIES}
        self.spec = installation.specification({
            'installation_id': 'install-' + suffix,
            'database': {'topology': 'remote', 'major': int(os.environ['POSTGRES_MAJOR']),
                         'host': settings.BOOTSTRAP['database']['HOST'], 'port': 5432,
                         'name': self.dbname, 'tls_ca': str(ca)},
            'paths': {name: str(self.base / name) for name in
                      ('venv', 'configuration', 'state', 'backup', 'root_trust')},
            'identities': self.users})
        self.users['credential'] = self.users['credentials']
        db = settings.BOOTSTRAP['database']
        self.admin = psycopg.connect(host=db['HOST'], port=db['PORT'], dbname=db['NAME'],
            user=db['USER'], password=db['PASSWORD'], sslmode='verify-full',
            sslrootcert=dict(self.spec.database)['tls_ca'], autocommit=True, connect_timeout=5)
        self.addCleanup(self.admin.close)
        self.passwords = {role: secrets.token_urlsafe(32) for role in (*installation.RUNTIME_ROLES, 'credential', 'maintenance')}
        self.addCleanup(self.cleanup_database)

    def cleanup_database(self):
        self.admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(self.dbname)))
        for role in self.passwords:
            self.admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(self.users['credentials' if role == 'credential' else role])))

    def provision(self):
        self.assertTrue(callable(getattr(installation, 'provision_database', None)),
                        'fresh PostgreSQL role/database provisioning is missing')
        return installation.provision_database(self.spec, self.admin, passwords=self.passwords)

    def connect(self, role, *, autocommit=True):
        db = dict(self.spec.database)
        return psycopg.connect(host=db['host'], port=db['port'], dbname=db['name'],
            user=self.users['credentials' if role == 'credential' else role], password=self.passwords[role], sslmode='verify-full',
            sslrootcert=db['tls_ca'], autocommit=autocommit, connect_timeout=5)

    # HYPOTHESIS: installer создаёт только четыре LOGIN, credential purpose не привязан.
    # SPEC: пятый отдельный DML LOGIN, 8 connections; DDL/ownership/maintenance запрещены.
    # SOLUTION: credential учитывается отдельно от трёх ordinary runtime pools.
    # RESULT: PG17 RED — four-password restriction; GREEN — 23 fresh DB/ACL/restore случаев.
    # STATUS: FIXED — see #644.
    def test_credential_login_is_distinct_restricted_and_budgeted(self):
        receipt = self.provision()
        self.assertEqual(receipt['credential_connections'], 8)
        self.assertEqual(receipt['total_connections'], 40)
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE TABLE credential_probe (id bigserial PRIMARY KEY, value text)')
            self.assertTrue(installation.verify_database(self.spec, maintenance)['verified'])
        with self.connect('credential') as daemon:
            self.assertEqual(daemon.execute('SELECT current_user').fetchone(), (self.users['credentials'],))
            daemon.execute("INSERT INTO credential_probe (value) VALUES ('fixture')")
            daemon.execute("UPDATE credential_probe SET value='changed'")
            self.assertEqual(daemon.execute('SELECT value FROM credential_probe').fetchone(), ('changed',))
            daemon.execute('DELETE FROM credential_probe')
            for statement in ('CREATE TABLE forbidden (id int)', 'ALTER TABLE credential_probe ADD COLUMN bad int',
                              'CREATE ROLE forbidden', 'CREATE DATABASE forbidden', 'TRUNCATE credential_probe',
                              'SET ROLE ' + self.users['maintenance']):
                with self.subTest(statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    daemon.execute(statement)
        self.admin.execute(sql.SQL('ALTER ROLE {} CREATEDB').format(sql.Identifier(self.users['credentials'])))
        with self.connect('maintenance') as maintenance:
            with self.assertRaisesRegex(RuntimeError, 'role privileges'):
                installation.verify_database(self.spec, maintenance)

    def test_fresh_database_runtime_roles_are_not_owner_or_superuser(self):
        receipt = self.provision()
        self.assertTrue(receipt['database_created'])
        self.assertFalse(receipt['services_started'])
        for role in installation.DATABASE_ROLES:
            row = self.admin.execute('SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, '
                'rolbypassrls, rolconnlimit FROM pg_roles WHERE rolname=%s', (self.users[role],)).fetchone()
            self.assertEqual(row, (False, False, False, False, False, 8))
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE TABLE install_probe (id bigserial PRIMARY KEY, content text)')
        for role in installation.DATABASE_RUNTIME_ROLES:
            with self.connect(role) as runtime:
                runtime.execute('INSERT INTO install_probe (content) VALUES (%s)', ('test',))
                self.assertGreater(runtime.execute('SELECT count(*) FROM install_probe').fetchone()[0], 0)
                for statement in ('CREATE TABLE forbidden (id int)', 'ALTER TABLE install_probe ADD COLUMN bad int',
                                  'CREATE ROLE forbidden', 'CREATE DATABASE forbidden', 'TRUNCATE install_probe'):
                    with self.subTest(role=role, statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        runtime.execute(statement)
        owner = self.admin.execute('SELECT pg_get_userbyid(datdba), datconnlimit FROM pg_database WHERE datname=%s',
                                  (self.dbname,)).fetchone()
        self.assertEqual(owner, (self.users['maintenance'], 40))
        self.assertNotIn(self.passwords['web'], repr(receipt))

    def test_budget_is_enforced_across_connections_not_thread_count(self):
        self.provision()
        active = []
        try:
            for role in installation.DATABASE_ROLES:
                for _ in range(8):
                    active.append(self.connect(role))
            self.assertEqual(len(active), 40)
            for role in installation.DATABASE_ROLES:
                with self.subTest(role=role), self.assertRaises(psycopg.OperationalError):
                    self.connect(role)
            self.assertEqual(active[-1].execute('SELECT 1').fetchone(), (1,))
        finally:
            for connection in active:
                connection.close()

    def test_repeated_provision_never_resets_existing_database_or_passwords(self):
        self.provision()
        with self.connect('maintenance') as db:
            db.execute('CREATE TABLE retained_install_work (id int)')
            db.execute('INSERT INTO retained_install_work VALUES (644)')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.provision()
        with self.connect('web') as runtime:
            self.assertEqual(runtime.execute('SELECT id FROM retained_install_work').fetchone(), (644,))

    def test_missing_tls_wrong_major_and_unencrypted_admin_refuse_before_ddl(self):
        self.assertTrue(callable(getattr(installation, 'provision_database', None)),
                        'fresh PostgreSQL role/database provisioning is missing')
        from dataclasses import replace
        other = dict(self.spec.database)
        other['major'] = 17 if other['major'] == 16 else 16
        with self.assertRaisesRegex(RuntimeError, 'major'):
            installation.provision_database(replace(self.spec, database=tuple(other.items())),
                                             self.admin, passwords=self.passwords)
        other = dict(self.spec.database)
        other['tls_ca'] = str(self.base / 'different-ca.pem')
        with self.assertRaisesRegex(RuntimeError, 'TLS|trust'):
            installation.provision_database(replace(self.spec, database=tuple(other.items())),
                                             self.admin, passwords=self.passwords)
        db = settings.BOOTSTRAP['database']
        with psycopg.connect(host=db['HOST'], port=db['PORT'], dbname=db['NAME'],
                user=db['USER'], password=db['PASSWORD'], sslmode='disable', autocommit=True) as plain:
            with self.assertRaisesRegex(RuntimeError, 'TLS'):
                installation.provision_database(self.spec, plain, passwords=self.passwords)
        self.assertIsNone(self.admin.execute('SELECT 1 FROM pg_database WHERE datname=%s', (self.dbname,)).fetchone())

    def test_external_database_verification_needs_no_admin_or_ddl_and_rejects_owner_membership(self):
        self.provision()
        self.assertTrue(callable(getattr(installation, 'verify_database', None)),
                        'restricted external database verification is missing')
        with self.connect('maintenance') as maintenance:
            result = installation.verify_database(self.spec, maintenance)
            self.assertTrue(result['verified'])
            self.assertEqual(result['runtime_connections'], 24)
            self.assertEqual(maintenance.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone(), (0,))
            maintenance.execute('CREATE TABLE verify_probe (id int)')
            maintenance.execute(sql.SQL('GRANT TRUNCATE ON verify_probe TO {}').format(sql.Identifier(self.users['web'])))
            with self.assertRaisesRegex(RuntimeError, 'table grants'):
                installation.verify_database(self.spec, maintenance)
            maintenance.execute(sql.SQL('REVOKE TRUNCATE ON verify_probe FROM {}').format(sql.Identifier(self.users['web'])))
            self.assertTrue(installation.verify_database(self.spec, maintenance)['verified'])
        self.admin.execute(sql.SQL('GRANT {} TO {}').format(
            sql.Identifier(self.users['maintenance']), sql.Identifier(self.users['web'])))
        with self.connect('maintenance') as maintenance:
            with self.assertRaisesRegex(RuntimeError, 'membership'):
                installation.verify_database(self.spec, maintenance)

    def test_migrations_initial_admin_and_catalog_offline_no_http_or_nats(self):
        self.provision()
        self.assertTrue(callable(getattr(installation, 'prepare_database', None)),
                        'ordered direct database initialization is missing')
        # Новый интерпретатор: чужое уже настроенное Django окружение не переиспользуется.
        from mop.server.controller.bootstrap import load
        import subprocess
        (self.base / 'configuration').mkdir(mode=0o700)
        for name, value in [('root_trust/root.key', secrets.token_bytes(64)),
                            ('configuration/maintenance/django.key', secrets.token_hex(64).encode())]:
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(value)
            path.chmod(0o600)
        boot = self.base / 'configuration/maintenance/bootstrap.json'
        boot.write_text(json.dumps(installation.bootstrap_document(self.spec, 'maintenance', password=self.passwords['maintenance'])))
        boot.chmod(0o600)
        load(boot)
        env = {key: value for key, value in os.environ.items() if not key.startswith(('MOP_', 'DJANGO_', 'PG'))}
        env['MOP_CONTROLLER_BOOTSTRAP'] = str(boot)
        code = '''
from mop.server.controller.runtime import configure, create_admin, close_pools
configure()
from mop.server.installation import prepare_database, initialize_configuration, render_catalog
from mop.server.access.services import issue_token, token_context
prepare_database()
create_admin('install_admin', 'Installation-only-admin-proof-644!')
from django.contrib.auth import get_user_model
user = get_user_model().objects.get(username='install_admin')
actor = token_context(issue_token(user.pk))
settings = initialize_configuration(actor, {'public_origin': 'https://fresh.example.invalid'})
assert settings.revision == 2 and settings.applied_revision is None
rendered = render_catalog(actor)
assert len(rendered['definition_keys']) == 83
assert rendered['desired_revision'] == 2 and rendered['applied'] is False
assert rendered['receipt'] is None and rendered['nodes'] == []
# #644/#695: prepare собрал статику Admin в общий каталог из bootstrap.
from django.conf import settings as django_settings
from pathlib import Path
assert Path(django_settings.STATIC_ROOT, 'staticfiles.json').is_file(), 'collectstatic manifest missing'
assert any(Path(django_settings.STATIC_ROOT, 'admin', 'css').glob('base.*.css')), 'admin static not collected'
close_pools()
'''
        venv = self.base / 'clean-server-venv'
        source = Path(__file__).resolve().parents[1]
        setup = subprocess.run([str(source / 'bin/mop'), 'server', 'controller', 'setup', '--venv', str(venv)],
                               env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        result = subprocess.run([str(venv / 'bin/python'), '-I', '-c', code], cwd=self.base,
                                env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.passwords['maintenance'], result.stdout + result.stderr)
        dump = self.base / 'fresh.dump'
        command = [str(venv / 'bin/mop'), 'server', 'controller']
        backup = subprocess.run([*command, 'backup', '--file', str(dump)], cwd=self.base,
                                env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(backup.returncode, 0, backup.stderr)
        self.assertEqual(dump.stat().st_mode & 0o777, 0o600)
        self.assertTrue(dump.read_bytes().startswith(b'PGDMP'))
        restored_name = self.dbname + '_restore'
        self.admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(
            sql.Identifier(restored_name), sql.Identifier(self.users['maintenance'])))
        self.addCleanup(lambda: self.admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(
            sql.Identifier(restored_name))))
        document = json.loads(boot.read_bytes())
        document['database']['NAME'] = restored_name
        boot.write_text(json.dumps(document))
        restored = subprocess.run([*command, 'restore', '--file', str(dump)], cwd=self.base,
                                 env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        check = '''
from mop.server.controller.runtime import configure, installation_snapshot, close_pools
configure()
assert installation_snapshot()['effects_enabled'] is False
from mop.server.controller.models import RestoreGuard
assert RestoreGuard.objects.get(pk=1).state == 'ready'
close_pools()
'''
        verified = subprocess.run([str(venv / 'bin/python'), '-I', '-c', check], cwd=self.base,
                                  env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(verified.returncode, 0, verified.stderr)
