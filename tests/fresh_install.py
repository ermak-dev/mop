"""Fresh install: stdlib plan, explicit topology и честные late blockers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import _lib  # noqa: F401
import hermetic  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# HYPOTHESIS: deploy запускает прежние роли и не имеет отдельного fresh пути.
# SPEC: пустая установка не читает legacy inputs и не обещает runtime без owners.
# План не выполняет setup, SQL, ansible или systemctl; secrets не входят в stdout.
# SOLUTION: независимый stdlib entrypoint, finite spec, protected artifacts,
# purpose-scoped DB bootstrap и явные blockers вместо фиктивного runtime.
# RESULT: render/права/no-leak/cold path проверены без установки служб.
# STATUS: FIXED — see #644 (installation-only slice, не полный runtime).
class FreshPlanFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mop-fresh-plan-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'install.json'
        self.spec = {
            'installation_id': 'fresh-proof',
            'database': {'topology': 'remote', 'major': 16, 'host': 'db.example.invalid',
                         'port': 5543, 'name': 'fresh_db', 'tls_ca': '/etc/mop-db/ca.pem'},
            'paths': {'venv': '/opt/mop-fresh/venv', 'configuration': '/etc/mop-fresh',
                      'state': '/var/lib/mop-fresh', 'backup': '/var/backups/mop-fresh',
                      'root_trust': '/var/lib/mop-trust'},
            'identities': {'maintenance': 'mop_maintenance', 'web': 'mop_web',
                           'worker': 'mop_worker', 'gateway': 'mop_gateway',
                           'credentials': 'mop_credentials', 'builder': 'mop_builder'},
        }
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.spec))
        self.path.chmod(0o600)

    def call(self, *args):
        return subprocess.run([str(ROOT / 'bin/mop'), 'server', 'install', *args,
                               '--spec', str(self.path)], cwd=self.tmp.name,
                              capture_output=True, text=True)

class FreshPlan(FreshPlanFixture, unittest.TestCase):
    def test_stdlib_cold_plan_has_dependency_order_and_explicit_blockers(self):
        result = self.call('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['order'], ['setup', 'postgresql', 'migrations', 'initialize',
            'admin', 'configuration', 'services', 'enrollment', 'image', 'body'])
        self.assertFalse(plan['production_ready'])
        self.assertEqual({item['owner'] for item in plan['blockers']}, {628, 643, 648, 669})
        self.assertEqual(plan['database']['topology'], 'remote')
        self.assertEqual(plan['database']['port'], 5543)
        self.assertEqual(plan['connections']['runtime'], 24)
        self.assertEqual(plan['connections']['maintenance'], 8)
        self.assertEqual(plan['connections']['scope'], 'instance-role-limits')
        self.assertNotIn('site.yml', result.stdout)

    def test_cold_command_bypasses_legacy_context_and_environment_readers(self):
        import contextlib
        import io
        from unittest.mock import patch
        from mop import cli
        from mop.common import config, context
        output = io.StringIO()
        with patch.object(context, 'here', side_effect=AssertionError('legacy context read')), \
                patch.object(context, 'strip_server', side_effect=AssertionError('legacy selector read')), \
                patch.object(config, 'get', side_effect=AssertionError('legacy setting read')), \
                patch.object(cli, 'run', side_effect=lambda fn, argv: fn(argv)), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['server', 'install', 'plan', '--spec', str(self.path)]), 0)
        self.assertFalse(json.loads(output.getvalue())['legacy_inputs'])

    def test_provisioning_command_emits_review_only_artifact(self):
        result = self.call('provisioning')
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(result.stdout)
        self.assertFalse(artifact['applied'])
        self.assertIsNone(artifact['receipt'])
        self.assertEqual(artifact['units'], [])

    def test_provisioned_topology_is_explicit_not_localhost_inference(self):
        self.spec['database']['topology'] = 'provisioned'
        self.spec['database']['major'] = 17
        self.write()
        result = self.call('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['database']['topology'], 'provisioned')

    def test_no_universal_bootstrap_or_private_input_echo(self):
        self.spec['old_env'] = 'sensitive-marker-never-echo'
        self.write()
        result = self.call('plan')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown installation keys', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('sensitive-marker-never-echo', result.stderr + result.stdout)

    def test_roles_paths_and_postgresql_versions_are_not_implicit(self):
        for key, value in [('major', 18), ('port', True), ('topology', 'auto')]:
            with self.subTest(key=key):
                original = self.spec['database'][key]
                self.spec['database'][key] = value
                self.write()
                result = self.call('plan')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('database', result.stderr)
                self.spec['database'][key] = original
        self.spec['identities']['web'] = self.spec['identities']['maintenance']
        self.write()
        result = self.call('plan')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('distinct', result.stderr)

    def test_public_spec_and_symlink_are_refused_before_execution(self):
        self.path.chmod(0o644)
        result = self.call('plan')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('private', result.stderr)
        self.path.chmod(0o600)
        original = self.path
        self.path = Path(self.tmp.name) / 'link.json'
        self.path.symlink_to(original)
        result = self.call('plan')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlink', result.stderr)

    def test_full_apply_refuses_unbound_runtime_not_fake_installed_units(self):
        result = self.call('apply')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runtime integration is not available', result.stderr)
        self.assertIn('#669', result.stderr)

class PreparationTests(FreshPlanFixture, unittest.TestCase):
    def library(self):
        from mop.server import installation
        return installation

    def test_provisioning_artifact_has_scoped_identities_no_service_start(self):
        module = self.library()
        self.assertTrue(callable(getattr(module, 'provisioning_artifact', None)),
                        'fresh installation provisioning renderer is missing')
        artifact = module.provisioning_artifact(module.specification(self.spec))
        self.assertFalse(artifact['applied'])
        self.assertEqual(len(artifact['accounts']), 6)
        self.assertFalse(any(account['name'] == 'root' for account in artifact['accounts']))
        self.assertTrue(all(account['shell'] == '/usr/sbin/nologin' for account in artifact['accounts']))
        self.assertTrue(all(item['mode'] == '0700' for item in artifact['directories']))
        self.assertEqual(artifact['units'], [])
        self.assertIn('playbook', artifact, 'reviewable controller provisioning playbook is missing')
        tasks = artifact['playbook'][0]['tasks']
        self.assertTrue(any('ansible.builtin.user' in task for task in tasks))
        self.assertTrue(any('ansible.builtin.file' in task for task in tasks))
        self.assertFalse(any('ansible.builtin.systemd' in task for task in tasks))
        self.assertEqual(artifact['postgresql']['package'], 'postgresql-client-16')
        self.assertFalse(artifact['postgresql']['manage_service'])
        self.spec['database']['topology'] = 'provisioned'
        managed = module.provisioning_artifact(module.specification(self.spec))
        self.assertEqual(managed['postgresql']['package'], 'postgresql-16')
        self.assertTrue(managed['postgresql']['manage_service'])
        self.assertEqual(managed['postgresql']['max_connections'], 35)

    def test_runtime_bootstrap_render_never_exports_maintenance_root(self):
        module = self.library()
        self.assertTrue(callable(getattr(module, 'bootstrap_document', None)),
                        'purpose-scoped bootstrap renderer is missing')
        spec = module.specification(self.spec)
        for role in ('web', 'worker', 'gateway', 'maintenance'):
            document = module.bootstrap_document(spec, role, password='secret-password-marker')
            self.assertEqual(document['role'], role)
            self.assertEqual(document['database']['OPTIONS']['sslmode'], 'verify-full')
            self.assertEqual(document['database']['HOST'], 'db.example.invalid')
            self.assertEqual(document['database']['USER'], self.spec['identities'][role])
            self.assertEqual('root_key' in document, role == 'maintenance')
        for role in ('builder', 'credentials', 'node', 'body'):
            with self.assertRaisesRegex(ValueError, 'database bootstrap'):
                module.bootstrap_document(spec, role, password='secret-password-marker')

    def test_node_delivery_allowlist_addressing_and_desired_not_applied(self):
        module = self.library()
        self.assertTrue(callable(getattr(module, 'node_artifact', None)),
                        'ORM-derived node renderer is missing')
        from types import SimpleNamespace
        node = SimpleNamespace(**dict(id='node-id', revision=3, name='fresh_node', driver='pve',
            ssh_host='ssh.example.invalid', ssh_port=2229, control_origin='https://api.example.invalid',
            lan_address='10.40.0.4', bootstrap_address='boot.example.invalid',
            deployment_member=True))
        values = (('public_origin', 'https://api.example.invalid'), ('log_level', 'info'),
                  ('telemetry_enabled', True), ('db_pool_max', 8), ('nomad_api_url', 'https://nomad.invalid'),
                  ('ldap_bind_secret_ref', 'secret-ref-marker'), ('pve_subnet', '10.40.0.0/24'))
        settings = SimpleNamespace(installation_revision=4, node_revision=2, values=values,
                                   missing=(), installation_applied_revision=None, node_applied_revision=None)
        result = module.node_artifact(node, settings)
        self.assertEqual(result['bootstrap_ssh']['port'], 2229)
        self.assertEqual(result['machine_url'], 'https://api.example.invalid')
        self.assertEqual(result['lan_route']['address'], '10.40.0.4')
        self.assertEqual(result['desired_revision'], 3)
        self.assertFalse(result['applied'])
        self.assertIsNone(result['receipt'])
        self.assertEqual(result['settings'], {'log_level': 'info', 'telemetry_enabled': True})
        rendered = json.dumps(result)
        for secret in ('db_pool_max', 'nomad_api_url', 'ldap_bind_secret_ref', 'secret-ref-marker', 'root_key'):
            self.assertNotIn(secret, rendered)

    def test_bootstrap_command_creates_only_own_private_runtime_document(self):
        import pwd
        self.spec['identities']['web'] = pwd.getpwuid(os.geteuid()).pw_name
        self.assertNotEqual(self.spec['identities']['web'], 'root',
                            'installation permission proof requires an ordinary test user')
        paths = {name: str(Path(self.tmp.name) / name) for name in self.spec['paths']}
        self.spec['paths'] = paths
        directory = Path(paths['configuration']) / 'web'
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        key = directory / 'django.key'
        key.write_bytes(b'k' * 64)
        key.chmod(0o600)
        password = Path(self.tmp.name) / 'password'
        password.write_bytes(b'private-db-password-marker-644')
        password.chmod(0o600)
        self.write()
        result = self.call('bootstrap', '--role', 'web', '--password-file', str(password))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        path = directory / 'bootstrap.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        data = json.loads(path.read_bytes())
        self.assertNotIn('root_key', data)
        self.assertNotIn('private-db-password-marker-644', result.stdout + result.stderr)
        repeated = self.call('bootstrap', '--role', 'web', '--password-file', str(password))
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn('new file', repeated.stderr)
        path.unlink()
        key.write_bytes(b'\xff' * 64)
        refused = self.call('bootstrap', '--role', 'web', '--password-file', str(password))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('signing key', refused.stderr)
        self.assertFalse(path.exists())

    def test_instance_budget_counts_all_processes_and_rejects_hidden_pool(self):
        module = self.library()
        self.assertTrue(callable(getattr(module, 'connection_budget', None)),
                        'instance-wide process budget is missing')
        self.assertEqual(module.connection_budget({'web': (1, 8), 'worker': (1, 8),
                         'gateway': (1, 8)}, runtime=24, maintenance=8)['total'], 32)
        for pools in ({'web': (2, 8), 'worker': (1, 8), 'gateway': (1, 8)},
                      {'web': (1, 8), 'worker': (1, 8), 'gateway': (1, 8), 'hidden': (1, 1)}):
            with self.assertRaisesRegex(ValueError, 'budget|process'):
                module.connection_budget(pools, runtime=24, maintenance=8)

class ProtectedArtifacts(unittest.TestCase):
    def test_new_private_artifact_refuses_replacement_and_unsafe_ancestors(self):
        from mop.server import installation
        self.assertTrue(callable(getattr(installation, 'write_private_artifact', None)),
                        'protected artifact writer is missing')
        with tempfile.TemporaryDirectory(prefix='mop-install-private-') as directory:
            path = Path(directory) / 'bootstrap.json'
            installation.write_private_artifact(path, b'private-marker')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(RuntimeError, 'new'):
                installation.write_private_artifact(path, b'replacement')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(RuntimeError):
                installation.write_private_artifact(link, b'replacement')
            Path(directory).chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'private'):
                installation.write_private_artifact(Path(directory) / 'other', b'private-marker')
            self.assertEqual(path.read_bytes(), b'private-marker')

    def test_retention_keeps_newest_dumps_never_touches_root_and_refuses_public_dump(self):
        from mop.server import installation
        self.assertTrue(callable(getattr(installation, 'retain_backups', None)),
                        'protected native backup retention is missing')
        with tempfile.TemporaryDirectory(prefix='mop-install-backup-') as directory:
            root = Path(directory) / 'root.key'
            root.write_bytes(b'separate-root-marker')
            root.chmod(0o600)
            for index in range(4):
                path = Path(directory) / (str(index) + '.dump')
                path.write_bytes(b'PGDMPfixture-only')
                path.chmod(0o600)
                os.utime(path, ns=(index * 1000000000, index * 1000000000))
            preview = installation.retain_backups(directory, keep=2)
            self.assertFalse(preview['applied'])
            self.assertEqual(preview['remove'], ['0.dump', '1.dump'])
            self.assertTrue((Path(directory) / '0.dump').exists())
            installation.retain_backups(directory, keep=2, apply=True)
            self.assertFalse((Path(directory) / '0.dump').exists())
            self.assertTrue(root.exists())
            (Path(directory) / '3.dump').chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'private'):
                installation.retain_backups(directory, keep=2, apply=True)
            self.assertTrue((Path(directory) / '2.dump').exists())

if __name__ == '__main__':
    unittest.main()
