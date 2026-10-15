"""Offline metadata/schema check, не PostgreSQL runtime acceptance #637."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import unittest
import _lib  # noqa: F401
import hermetic

ROOT = Path(__file__).resolve().parents[1]


class Schema(unittest.TestCase):
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
