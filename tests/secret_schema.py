"""Offline metadata/schema check, не PostgreSQL runtime acceptance #637."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import unittest
import _lib  # noqa: F401
import hermetic

ROOT = Path(__file__).resolve().parents[1]

# HYPOTHESIS: conditional unique без capability check может тихо исчезнуть.
# SOLUTION: явная проверка features и стандартного migration graph.
# RESULT: schema checks проходят локально и в PG16/17 jobs 29440/29441.
# STATUS: FIXED — see #637.


class Schema(unittest.TestCase):
    # HYPOTHESIS: in_atomic_block не видит ручной set_autocommit(False).
    # Проверяем все aliases без настройки Django/подключения к реальной установке.
    def test_provider_guard_rejects_manual_transaction_on_any_connection(self):
        from types import SimpleNamespace
        tree = ast.parse((ROOT / 'mop/server/secrets/services.py').read_bytes())
        guard = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == 'outside_transaction')
        boundary = type('TransactionBoundary', (ValueError,), {})
        def connection(atomic=False, autocommit=True):
            return SimpleNamespace(in_atomic_block=atomic, get_autocommit=lambda: autocommit)
        namespace = {'TransactionBoundary': boundary}
        exec(compile(ast.Module(body=[guard], type_ignores=[]), '<provider-guard>', 'exec'), namespace)
        for blocked in (connection(autocommit=False), connection(atomic=True)):
            for alias in ('default', 'secondary'):
                with self.subTest(alias=alias, atomic=blocked.in_atomic_block):
                    namespace['connections'] = {'default': connection(), 'secondary': connection()}
                    namespace['connections'][alias] = blocked
                    with self.assertRaises(boundary):
                        namespace['outside_transaction']()
        namespace['connections'] = {'default': connection(), 'secondary': connection()}
        namespace['outside_transaction']()

    def test_controller_secret_cases_are_valid_python(self):
        ast.parse((ROOT / 'tests/_secret_cases.py').read_bytes())

    def test_required_partial_constraints_fail_closed(self):
        if importlib.util.find_spec('django') is None:
            hermetic.skip('secret schema', 'declared server extra is required')
            return
        self.assertIsNotNone(importlib.util.find_spec('mop.server.secrets.checks'),
                             'conditional uniqueness requires explicit backend capability check')
        from unittest.mock import patch, Mock
        from mop.server.secrets.checks import secret_guarantees
        fake = Mock()
        fake.features.supports_partial_indexes = False
        with patch('mop.server.secrets.checks.connections', {'default': fake}):
            self.assertEqual([e.id for e in secret_guarantees(databases=['default'])], ['secrets.E001'])

    def test_normal_migrations_match_models(self):
        if importlib.util.find_spec('django') is None:
            hermetic.skip('secret schema', 'declared server extra is required')
            return
        result = subprocess.run([str(ROOT / 'bin/mop'), 'server', 'controller', 'schema'], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
