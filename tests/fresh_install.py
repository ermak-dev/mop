"""Fresh install: stdlib plan, explicit topology и честные late blockers."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from _lib import run_command
import _lib  # noqa: F401 -- общий временный корень процесса (#338)
import hermetic  # noqa: F401
from _fresh_install_fixture import FreshPlanFixture, ROOT

sys.path.insert(0, str(ROOT))

# HYPOTHESIS: deploy запускает прежние роли и не имеет отдельного fresh пути.
# SPEC: пустая установка не читает legacy inputs и не обещает runtime без owners.
# План не выполняет setup, SQL, ansible или systemctl; secrets не входят в stdout.
# SOLUTION: независимый stdlib entrypoint, finite spec, protected artifacts,
# purpose-scoped DB bootstrap и явные blockers вместо фиктивного runtime.
# RESULT: render/права/no-leak/cold path проверены без установки служб.
# STATUS: FIXED — see #644 (installation-only slice, не полный runtime).
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
        # #675: захват stdout -- общий run_command(out=...), а не свой
        # redirect_stdout; патчи легаси-читателей остаются вокруг вызова.
        from unittest.mock import patch
        from mop import cli
        from mop.common import config, context
        output = io.StringIO()
        with patch.object(context, 'here', side_effect=AssertionError('legacy context read')), \
                patch.object(context, 'strip_server', side_effect=AssertionError('legacy selector read')), \
                patch.object(config, 'get', side_effect=AssertionError('legacy setting read')), \
                patch.object(cli, 'run', side_effect=lambda fn, argv: fn(argv)):
            text, _, code = run_command(cli.main,
                ['server', 'install', 'plan', '--spec', str(self.path)], out=output)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(text)['legacy_inputs'])

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
    # HYPOTHESIS: plural OS identity ошибочно исключает singular credential purpose.
    # SPEC: отдельный DML login и private root/signing refs, не maintenance/rootless client.
    # SOLUTION: явное соответствие credential -> credentials, без второго bootstrap schema.
    # RESULT: RED — unsupported credential / missing budget; узкий pure прогон GREEN.
    # STATUS: FIXED — see #644.
    def test_credential_bootstrap_has_its_own_login_and_private_root_custody(self):
        module = self.library()
        data = module.bootstrap_document(module.specification(self.spec), 'credential',
                                         password='private-credential-password')
        config = self.spec['paths']['configuration'] + '/credentials'
        self.assertEqual(set(data), {'installation_id', 'role', 'database', 'root_key', 'django_key'})
        self.assertEqual(data['role'], 'credential')
        self.assertEqual(data['database']['USER'], self.spec['identities']['credentials'])
        self.assertNotEqual(data['database']['USER'], self.spec['identities']['web'])
        self.assertEqual(data['root_key'], config + '/root.key')
        self.assertEqual(data['django_key'], config + '/django.key')
        self.assertNotIn(self.spec['paths']['root_trust'], data['root_key'])
        self.assertNotIn('private-credential-password', repr(data))

    def test_credential_pool_is_explicit_and_does_not_consume_ordinary_or_maintenance_budget(self):
        module = self.library()
        spec = module.specification(self.spec)
        budget = module.connection_budget({role: (1, 8) for role in module.RUNTIME_ROLES},
                                          runtime=24, maintenance=8)
        self.assertEqual(budget['credential'], 8)
        self.assertEqual(budget['total'], 40)
        planned = module.plan(spec)['connections']
        self.assertEqual(planned['roles']['credential'], 8)
        self.assertEqual(planned['credential'], 8)
        self.assertEqual(planned['total'], 40)
        postgres = module.provisioning_artifact(spec)['postgresql']
        self.assertEqual(postgres['credential_connections'], 8)
        self.assertEqual(postgres['max_connections'], 43)

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
        self.assertEqual(managed['postgresql']['max_connections'], 43)

    # HYPOTHESIS: installer не передаёт endpoint/UID, factory всегда unbound.
    # SPEC: один derived path для renderer/provisioner, web/worker получают
    # только координаты; UID явен после OS provisioning и не приходит из env.
    # SOLUTION: явный credential_uid в renderer, OS lookup только в installer CLI.
    # RESULT: RED -- отсутствовали endpoint и shared directory; GREEN без service start.
    # STATUS: FIXED — see #644.
    def test_rootless_credential_bootstrap_uses_installation_path_and_exact_os_uid(self):
        import inspect
        module = self.library()
        self.assertIn('credential_uid', inspect.signature(module.bootstrap_document).parameters,
                      'installer cannot pin the protected credential service UID')
        spec = module.specification(self.spec)
        for role in ('web', 'worker'):
            with self.subTest(role=role):
                data = module.bootstrap_document(spec, role, password='own-db-password', credential_uid=1944)
                self.assertEqual(data['credential_service'], {'path': '/var/lib/mop-fresh/credential-socket/service.sock',
                                                              'server_uid': 1944})
                self.assertNotIn('root_key', data)
                self.assertEqual(set(data['credential_service']), {'path', 'server_uid'})
                for uid in (None, True, 0, -1, '1944', 2 ** 32 - 1):
                    with self.subTest(uid=uid), self.assertRaisesRegex(ValueError, 'credential service'):
                        module.bootstrap_document(spec, role, password='own-db-password', credential_uid=uid)
        for role in ('maintenance', 'gateway'):
            self.assertNotIn('credential_service', module.bootstrap_document(spec, role, password='own-db-password'))

    def test_credential_socket_group_only_allows_web_worker_traversal_not_private_custody(self):
        module = self.library()
        artifact = module.provisioning_artifact(module.specification(self.spec))
        socket_dirs = [item for item in artifact['shared_directories'] if item['owner'] == 'mop_credentials']
        self.assertEqual(socket_dirs, [{'path': '/var/lib/mop-fresh/credential-socket',
                                      'owner': 'mop_credentials', 'group': 'mop_credentials', 'mode': '0750'}])
        tasks = artifact['playbook'][0]['tasks']
        memberships = [task for task in tasks if task.get('ansible.builtin.user', {}).get('groups') == 'mop_credentials']
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0]['loop'], ['mop_web', 'mop_worker'])
        self.assertTrue(memberships[0]['ansible.builtin.user']['append'])
        for item in artifact['directories']:
            if item['owner'] == 'mop_credentials':
                self.assertEqual(item['mode'], '0700')

    def test_runtime_bootstrap_render_never_exports_maintenance_root(self):
        module = self.library()
        self.assertTrue(callable(getattr(module, 'bootstrap_document', None)),
                        'purpose-scoped bootstrap renderer is missing')
        spec = module.specification(self.spec)
        for role in ('web', 'worker', 'gateway', 'maintenance'):
            document = module.bootstrap_document(spec, role, password='secret-password-marker', credential_uid=1944)
            self.assertEqual(document['role'], role)
            self.assertEqual(document['database']['OPTIONS']['sslmode'], 'verify-full')
            self.assertEqual(document['database']['HOST'], 'db.example.invalid')
            self.assertEqual(document['database']['USER'], self.spec['identities'][role])
            self.assertEqual('root_key' in document, role == 'maintenance')
        for role in ('builder', 'credentials', 'node', 'body'):
            with self.assertRaisesRegex(ValueError, 'database bootstrap'):
                module.bootstrap_document(spec, role, password='secret-password-marker')

    # #695/#644: собранная статика -- общий каталог state/static: пишет
    # maintenance (collectstatic), раздаёт web; остальным ролям он не нужен.
    def test_collected_static_root_is_shared_only_by_maintenance_and_web(self):
        module = self.library()
        spec = module.specification(self.spec)
        expected = self.spec['paths']['state'] + '/static'
        for role in ('maintenance', 'web'):
            self.assertEqual(module.bootstrap_document(spec, role, password='p', credential_uid=1944)['static_root'], expected)
        for role in ('worker', 'gateway'):
            self.assertNotIn('static_root', module.bootstrap_document(spec, role, password='p', credential_uid=1944))
        artifact = module.provisioning_artifact(spec)
        self.assertEqual(artifact['shared_directories'], [{'path': expected,
            'owner': self.spec['identities']['maintenance'], 'group': self.spec['identities']['web'],
            'mode': '0750'}, {'path': self.spec['paths']['state'] + '/credential-socket',
            'owner': self.spec['identities']['credentials'], 'group': self.spec['identities']['credentials'],
            'mode': '0750'}])
        tasks = artifact['playbook'][0]['tasks']
        self.assertTrue(any(task.get('ansible.builtin.file', {}).get('mode') == '0750' for task in tasks))

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
        self.spec['identities']['credentials'] = 'nobody'
        expected_uid = pwd.getpwnam('nobody').pw_uid
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
        self.assertEqual(data['credential_service'], {
            'path': paths['state'] + '/credential-socket/service.sock', 'server_uid': expected_uid})
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

    def test_bootstrap_cli_maps_credential_purpose_to_plural_os_account_and_private_files(self):
        import pwd
        self.spec['identities']['credentials'] = pwd.getpwuid(os.geteuid()).pw_name
        self.spec['paths'] = {name: str(Path(self.tmp.name) / name) for name in self.spec['paths']}
        directory = Path(self.spec['paths']['configuration']) / 'credentials'
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        for name, value in [('django.key', b'd' * 64), ('root.key', b'r' * 32)]:
            path = directory / name
            path.write_bytes(value)
            path.chmod(0o600)
        ca = Path(self.tmp.name) / 'fixture-ca.pem'
        ca.write_bytes(b'explicit-fixture-trust-reference')
        ca.chmod(0o644)
        self.spec['database']['tls_ca'] = str(ca)
        password = directory / 'database.password'
        password.write_bytes(b'private-credential-db-password-644')
        password.chmod(0o600)
        self.write()
        result = self.call('bootstrap', '--role', 'credential', '--password-file', str(password))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        bootstrap = directory / 'bootstrap.json'
        self.assertEqual(bootstrap.stat().st_mode & 0o777, 0o600)
        from mop.server.controller.bootstrap import load
        data = load(bootstrap)
        self.assertEqual(data['role'], 'credential')
        self.assertEqual(data['database']['USER'], self.spec['identities']['credentials'])
        self.assertNotIn('private-credential-db-password-644', result.stdout + result.stderr)
        repeated = self.call('bootstrap', '--role', 'credential', '--password-file', str(password))
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn('new file', repeated.stderr)
        bootstrap.unlink()
        (directory / 'root.key').write_bytes(b'short')
        refused = self.call('bootstrap', '--role', 'credential', '--password-file', str(password))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('root trust', refused.stderr)
        self.assertFalse(bootstrap.exists())

    def test_instance_budget_counts_all_processes_and_rejects_hidden_pool(self):
        module = self.library()
        self.assertTrue(callable(getattr(module, 'connection_budget', None)),
                        'instance-wide process budget is missing')
        self.assertEqual(module.connection_budget({'web': (1, 8), 'worker': (1, 8),
                         'gateway': (1, 8)}, runtime=24, maintenance=8)['total'], 40)
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
