"""Сторож импортов Django и предметного DAG #632."""
from pathlib import Path
import tempfile
import unittest
import _layers as layers

# HYPOTHESIS: старый scanner отбрасывает внешние и динамические импорты,
# поэтому Django в common и скрытый цикл Access/Secrets остаются зелёными.
class ImportTests(unittest.TestCase):
    def edges(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe.py'
            path.write_text(source)
            return layers.imports_of(str(path), 'mop.server.secrets.probe', {})

    def test_external_imports_are_visible(self):
        self.assertIn('django.db', self.edges('from django.db import transaction'))
        self.assertIn('channels.db', self.edges('import channels.db'))

    def test_dynamic_and_type_checking_edges_are_visible(self):
        for code in (
            'import importlib as il\nil.import_module("mop.server.access.models")',
            'from importlib import import_module as load\nload("mop.server.access.models")',
            '__import__("mop.server.access.models")',
            'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n from mop.server.access import models',
        ):
            with self.subTest(code=code):
                edges = self.edges(code)
                self.assertTrue(any(x.startswith('mop.server.access') for x in edges), edges)

    def test_subject_graph_rejects_reverse_edges_and_framework_leaks(self):
        from _lib import Checks
        with tempfile.TemporaryDirectory() as directory:
            for name, source in (
                ('mop.common.leak', 'from django.db import models'),
                ('mop.server.secrets.leak', 'from mop.server.access import models'),
                ('mop.server.catalog.leak', 'import mop.server.controller.runtime'),
                ('mop.server.access.leak', 'import importlib\nimportlib.import_module(target)'),
                ('mop.server.secrets.leak', 'import importlib as il\nloader = il.import_module\nloader("mop.server.access.models")'),
            ):
                with self.subTest(name=name):
                    path = Path(directory) / 'probe.py'
                    path.write_text(source)
                    checks = Checks()
                    layers.violations(checks, {name: str(path)})
                    self.assertGreater(checks.failed, 0)

if __name__ == '__main__':
    unittest.main()
