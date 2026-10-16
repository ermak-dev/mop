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
    # SOLUTION: общий guard отказывает также при get_autocommit() == False.
    # RESULT: red на обоих manual aliases; после fix все четыре schema checks green.
    # STATUS: FIXED — see #637.
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

    # HYPOTHESIS: параллельные leaves конфликтуют, offline schema теряет Builds/Gateway.
    # SOLUTION: явные merge migrations, единый namespace receipt и полная регистрация apps.
    # RESULT: сначала два red: конфликт leaves и потеря двух apps; после сборки оба green.
    # STATUS: FIXED — see #623.
    # Никаких ослаблений constraints или исключений models ради зелёной сборки.
    def test_offline_schema_registers_every_production_domain(self):
        schema = ast.parse((ROOT / 'mop/server/controller/schema.py').read_bytes())
        production = ast.parse((ROOT / 'mop/server/controller/settings.py').read_bytes())
        call = next(node for node in ast.walk(schema) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == 'configure')
        offline = ast.literal_eval(next(item.value for item in call.keywords if item.arg == 'INSTALLED_APPS'))
        registered = ast.literal_eval(next(node.value for node in production.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'INSTALLED_APPS'
                                                   for target in node.targets)))
        def domains(names):
            return {name.split('.apps.')[0] for name in names
                    if name.startswith('mop.server.') and '.adminapps.' not in name}
        self.assertEqual(domains(offline), domains(registered))

    def test_normal_migrations_match_models(self):
        if importlib.util.find_spec('django') is None:
            hermetic.skip('secret schema', 'declared server extra is required')
            return
        result = subprocess.run([str(ROOT / 'bin/mop'), 'server', 'controller', 'schema'], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
