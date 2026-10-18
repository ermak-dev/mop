"""Защита trust ACL в native restore, только собственные PostgreSQL fixtures."""
import json
from pathlib import Path
import subprocess
import unittest

import psycopg
from psycopg import sql
from _installation_cases import FreshPostgreSQL as Fixture
from _installation_acl_cases import InstallationACL
from mop.server import installation


# HYPOTHESIS: default CRUD появляется заново после archive COMMIT; поздний
# seal не защищает проверенное доверие. Owner view также обходит table ACL.
# SOLUTION: target trust schema/ACL не пересоздаётся; из archive берётся только
# Installation data после проверки migration history. Verifier отвергает доступные views.
# RESULT: PG16 red — runtime UPDATE доступен и view принят; PG16/17 по 22/22 green.
# STATUS: FIXED — see #644
class TrustRestoreBoundary(unittest.TestCase):
    setUp = Fixture.setUp
    cleanup_database = InstallationACL.cleanup_database
    provision = Fixture.provision
    connect = Fixture.connect
    prepare = InstallationACL.prepare

    def test_external_verification_rejects_owner_view_reopening_trust_writes(self):
        self.prepare()
        with self.connect('maintenance') as maintenance:
            maintenance.execute('CREATE VIEW trust_guard_alias AS SELECT * FROM controller_restoreguard')
            with maintenance.transaction():
                maintenance.execute('SET TRANSACTION READ ONLY')
                with self.assertRaises(RuntimeError):
                    installation.verify_database(self.spec, maintenance)

    def test_restore_never_recreates_runtime_writable_trust_table(self):
        env = self.prepare()
        source = Path(__file__).resolve().parents[1]
        command = [str(source / 'bin/mop'), 'server', 'controller']
        archive = self.base / 'protected.dump'
        backup = subprocess.run([*command, 'backup', '--file', str(archive)], cwd=self.base,
            env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(backup.returncode, 0, backup.stderr)
        target = self.dbname + '_trust_restore'
        self.admin.execute(sql.SQL('CREATE DATABASE {} OWNER {} CONNECTION LIMIT 40').format(
            sql.Identifier(target), sql.Identifier(self.users['maintenance'])))
        self.addCleanup(lambda: self.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target))))
        db = dict(self.spec.database)
        common = dict(host=db['host'], port=db['port'], dbname=target,
            sslmode='verify-full', sslrootcert=db['tls_ca'], autocommit=True, connect_timeout=5)
        with psycopg.connect(**common, user=self.users['maintenance'],
                             password=self.passwords['maintenance']) as maintenance:
            maintenance.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(target)))
            maintenance.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
            for role in installation.DATABASE_RUNTIME_ROLES:
                user = sql.Identifier(self.users[role])
                maintenance.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(target), user))
                maintenance.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(user))
                maintenance.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}').format(user))
                maintenance.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                    'GRANT USAGE, SELECT ON SEQUENCES TO {}').format(user))
        bootstrap = Path(env['MOP_CONTROLLER_BOOTSTRAP'])
        document = json.loads(bootstrap.read_bytes())
        document['database']['NAME'] = target
        bootstrap.write_text(json.dumps(document))
        runtime = dict(common, user=self.users['web'], password=self.passwords['web'])
        code = '''
import psycopg
from mop.server.controller.runtime import configure, close_pools
configure()
from mop.server.controller import backup
original = backup._verify_restored_secrets
observed = []
def check_runtime_acl(using):
    original(using)
    with psycopg.connect(**CONFIG) as runtime:
        observed.append(runtime.execute("SELECT has_column_privilege(current_user, 'controller_installation', 'root_digest', 'UPDATE')").fetchone()[0])
backup._verify_restored_secrets = check_runtime_acl
try:
    result = backup.restore(ARCHIVE)
    assert observed == [False], ('runtime trust write window', observed)
    assert result['effects_enabled'] is False
finally:
    close_pools()
'''.replace('CONFIG', repr(runtime)).replace('ARCHIVE', repr(str(archive)))
        restored = subprocess.run(['python3', '-I', '-c', code], cwd=self.base, env=env,
            capture_output=True, text=True, timeout=90)
        self.assertEqual(restored.returncode, 0, restored.stderr)
