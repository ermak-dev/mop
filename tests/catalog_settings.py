"""Типизированное покрытие настроек новой установки (#634).

HYPOTHESIS: пять ключей не покрывают spec, drivers, provision и runtime;
старые строки нельзя ни отбросить без решения, ни читать как fallback.
SOLUTION: конечная схема, явная классификация старых имён и чистый resolver.
RESULT: исходные шесть сценариев дали behavioral red; затем 7/7 green.
STATUS: FIXED — see #634
"""
import ast
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import hermetic  # noqa: F401
import _lib  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mop.server.catalog import definitions as d


class SettingsCoverage(unittest.TestCase):
    def test_runtime_consumers_have_typed_bounded_definitions(self):
        # Это проверяемый контракт потребителей, не снимок старых строк.
        expected = {
            'public_origin': 'text', 'git_host': 'text', 'nomad_api_url': 'text',
            'pool_datacenter': 'text', 'puppet_memory_mb': 'int', 'memory_limit_mb': 'int',
            'puppet_path': 'text', 'puppet_seed': 'text', 'fallback_model': 'text',
            'proxy_url': 'text', 'proxy_port': 'int', 'proxy_request_max_mb': 'int',
            'pool_home': 'text', 'pool_user': 'text', 'pve_storage': 'text',
            'pve_template': 'text', 'pve_bridge': 'text', 'pve_subnet': 'text',
            'pve_gateway': 'text', 'pve_vmid_base': 'int', 'disk_gb': 'int',
            'cores': 'int', 'body_memory_cap_mb': 'int', 'body_disk_cap_gb': 'int',
            'body_cores_cap': 'int', 'sweep_free_min_gb': 'int',
            'sweep_max_target_gb': 'int', 'sweep_stale_days': 'int', 'sweep_timeout_s': 'int',
            'gc_free_min_gb': 'int', 'gc_max_per_run': 'int',
            'blocking_limit': 'int', 'database_limit': 'int', 'db_pool_max': 'int',
            'project_queue_limit': 'int', 'instance_db_connections': 'int',
            'admin_db_headroom': 'int', 'db_connect_timeout_s': 'int',
            'db_statement_timeout_s': 'int', 'db_lock_timeout_s': 'int',
            'transaction_timeout_s': 'int', 'effect_timeout_s': 'int',
            'auth_provider': 'text', 'ldap_url': 'text', 'ldap_bind_dn': 'text',
            'ldap_base': 'text', 'ldap_starttls': 'bool', 'ldap_ca_ref': 'text',
            'ldap_login_attr': 'text', 'ldap_name_attrs': 'text', 'ldap_email_attr': 'text',
            'ldap_group_base': 'text', 'ldap_group_filter': 'text',
            'ldap_admin_group': 'text', 'ldap_access_group': 'text',
            'ldap_nested': 'text', 'ldap_project_group': 'text',
            'nats_url': 'text', 'nats_port': 'int', 'https_port': 'int',
            'http_bind': 'text', 'http_port': 'int', 'nomad_rpc_port': 'int',
            'nomad_http_bind': 'text', 'nomad_job_gc_hours': 'int',
            'nomad_data': 'text', 'nomad_version': 'text', 'nomad_mirror': 'text',
            'nats_version': 'text', 'proxy_version': 'text', 'proxy_panel_version': 'text',
            'proxy_lan_enabled': 'bool', 'locale': 'text', 'ansible_profile': 'bool',
            'deploy_needs_green': 'bool', 'tls_cert_ref': 'text', 'tls_ca_ref': 'text',
        }
        self.assertFalse(set(expected) - d.DEFINITIONS.keys(),
                         f'missing consumer settings: {sorted(set(expected) - d.DEFINITIONS.keys())}')
        for key, kind in expected.items():
            definition = d.DEFINITIONS[key]
            self.assertEqual(definition.kind, kind, key)
            if kind == 'int':
                self.assertGreaterEqual(definition.maximum, definition.minimum, key)
            elif kind == 'text':
                self.assertGreater(definition.max_length, 0, key)

    def test_defaults_are_portable_missing_inputs_are_explicit_and_no_env_fallback(self):
        self.assertTrue(callable(getattr(d, 'resolve', None)), 'no effective-setting resolver')
        with patch.dict('os.environ', {'MOP_HOME': '/other/home', 'MOP_PROXY_URL': 'https://other.invalid'}):
            value = d.resolve({}, {})
        self.assertEqual(dict(value.values)['effect_limit'], 8)
        self.assertEqual(dict(value.values)['memory_limit_mb'], 12288)
        self.assertIn('public_origin', value.missing)
        self.assertIn('pool_home', value.missing)
        self.assertNotIn('pool_home', dict(value.values))
        self.assertNotIn('proxy_url', dict(value.values))
        self.assertEqual(dict(value.sources)['effect_limit'], 'default')
        self.assertEqual(d.resolve({}, {}), value)
        for definition in d.DEFINITIONS.values():
            if definition.default is not None:
                definition.validate(definition.default)

    def test_precedence_scope_strict_types_and_corrupt_rows_fail_closed(self):
        self.assertTrue(callable(getattr(d, 'resolve', None)), 'no effective-setting resolver')
        resolved = d.resolve({'log_level': 'warning', 'sweep_timeout_s': 400},
                             {'log_level': 'debug', 'sweep_timeout_s': 300})
        self.assertEqual(dict(resolved.values)['log_level'], 'debug')
        self.assertEqual(dict(resolved.sources)['log_level'], 'node')
        self.assertEqual(dict(resolved.values)['sweep_timeout_s'], 300)
        for installation, node in (({'unknown': 1}, {}), ({}, {'effect_limit': 1}),
                                   ({'effect_limit': True}, {}), ({'nats_port': '4222'}, {}),
                                   ({'nats_port': 65536}, {}), ({'effect_limit': 9}, {})):
            with self.assertRaises(ValueError):
                d.resolve(installation, node)
        with self.assertRaises(ValueError):
            d.resolve({}, {}, require=('public_origin',))
        with self.assertRaises(ValueError):
            d.resolve({}, {}, require=('typo',))

    def test_safe_text_shapes_and_deliberate_node_scope(self):
        for key in ('pool_home', 'pool_user', 'public_origin', 'memory_limit_mb', 'puppet_seed'):
            with self.assertRaises(ValueError, msg=key):
                d.validate_batch({key: '/example' if key != 'memory_limit_mb' else 100}, node=True)
        self.assertIn('pve_storage', d.DEFINITIONS, 'PVE consumer configuration missing')
        self.assertEqual(d.validate_batch({'pve_storage': 'nvme-data'}, node=True), {'pve_storage': 'nvme-data'})
        for key, value in (('public_origin', 'http://public.invalid'),
                           ('public_origin', 'https://user:password@public.invalid'),
                           ('proxy_url', 'https://proxy.invalid/?key=secret'),
                           ('pool_home', '~/pool'), ('pool_home', '/srv/pool\nextra'),
                           ('pve_subnet', 'not-a-network'), ('pve_subnet', '10.0.0.0/32'),
                           ('locale', 'x'*129), ('ldap_starttls', '1'),
                           ('auth_provider', 'file'), ('tls_ca_ref', '/old/secrets/ca.pem')):
            with self.assertRaises(ValueError, msg=key):
                d.validate_batch({key: value})
        for key in ('MOP_PROXY_KEY', 'MOP_TLS_KEY', 'root_key', 'database_password', 'ldap_bind_password'):
            with self.assertRaises(ValueError):
                d.validate_batch({key: 'do-not-echo'})

    def test_cross_field_requirements_apply_to_result_not_partial_batch(self):
        self.assertTrue(callable(getattr(d, 'resolve', None)), 'no effective-setting resolver')
        for settings in ({'puppet_memory_mb': 16384}, {'gc_free_min_gb': 70},
                         {'queue_limit': 10}, {'pve_subnet': '10.0.0.0/24', 'pve_gateway': '10.1.0.1'},
                         {'auth_provider': 'django,ldap'}, {'ldap_url': 'ldap://directory.invalid'}):
            with self.assertRaises(ValueError, msg=str(settings)):
                d.resolve(settings, {})
        valid = d.resolve({'puppet_memory_mb': 16384, 'memory_limit_mb': 24576,
                           'queue_limit': 10, 'project_queue_limit': 5,
                           'pve_subnet': '10.1.0.0/24'}, {})
        self.assertEqual(dict(valid.values)['pve_gateway'], '10.1.0.1')
        self.assertEqual(dict(valid.sources)['pve_gateway'], 'derived')

    def test_legacy_call_sites_outside_the_old_table_are_also_classified(self):
        self.assertTrue(callable(getattr(d, 'classify_legacy', None)))
        names = set()
        root = Path(__file__).resolve().parents[1] / 'mop'
        for path in root.rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'config'
                        and node.func.attr in ('get', 'num') and node.args
                        and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str
                        and node.args[0].value.startswith(('MOP_', 'GITLAB_', 'NOMAD_'))):
                    names.add(node.args[0].value)
        for name in names:
            self.assertTrue(d.classify_legacy(name).reason, name)

    def test_every_legacy_input_has_explicit_nonimporting_disposition(self):
        self.assertTrue(callable(getattr(d, 'classify_legacy', None)), 'no legacy-input disposition')
        # Только исходник, ни import config, ни .env, ни вызов старого resolver.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'mop/common/config.py').read_text())
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in
                    ('REQUIRED', 'DEFAULTS', 'OPTIONAL', 'DERIVED') for t in node.targets):
                names.update(k.value for k in node.value.keys if isinstance(k, ast.Constant))
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PROCESS_SCOPED' for t in node.targets):
                names.update(ast.literal_eval(node.value))
        for name in names:
            item = d.classify_legacy(name)
            self.assertTrue(item.reason, name)
            self.assertIn(item.category, ('setting', 'node_record', 'policy', 'secret', 'removed', 'request_context'))
            if item.category == 'setting':
                self.assertIn(item.target, d.DEFINITIONS, name)
        self.assertEqual(d.classify_legacy('MOP_PROXY_KEY').category, 'secret')
        self.assertEqual(d.classify_legacy('MOP_TLS_KEY').category, 'secret')
        self.assertEqual(d.classify_legacy('MOP_DRIVER').category, 'node_record')
        self.assertEqual(d.classify_legacy('MOP_OPERATORS_FILE').category, 'removed')
        self.assertEqual(d.classify_legacy('MOP_BUS_CONFIG').category, 'removed')
        with self.assertRaises(ValueError):
            d.classify_legacy('MOP_UNCLASSIFIED')


if __name__ == '__main__':
    unittest.main()
