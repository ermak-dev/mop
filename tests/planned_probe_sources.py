#!/usr/bin/env python3
"""Isolated planned-probe snapshot должен включать текущий transfer profile (#766)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import _lib  # noqa: F401 — общий lifetime checks
import hermetic  # noqa: F401
from mop.node.host_phase_probe import snapshot_sources


# HYPOTHESIS: новый initial_transfer отсутствует в explicit source closure.
# SOLUTION: переносить публичный common module, не брать ambient package.
# RESULT: actual probe и isolated import дают RED до исправления closure.
# STATUS: RED — see #766
class PlannedProbeSources(unittest.TestCase):
    def test_snapshot_imports_phase_budgets_without_ambient_package(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='mop-766-source-') as directory:
            root = Path(directory)
            context = root/'snapshot'
            neutral = root/'empty'
            neutral.mkdir()
            snapshot_sources(source, context)
            package = context/'package'
            script = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mop.node.host_inputs import Limits
from mop.common import initial_transfer
assert Path(initial_transfer.__file__).resolve() == Path(sys.argv[1])/'mop/common/initial_transfer.py'
limits = Limits()
assert limits.file_bytes('bootstrap_body') == initial_transfer.phase_limit('bootstrap_body')
assert limits.file_bytes('prepare_body') == initial_transfer.DEFAULT_BYTES
'''
            result = subprocess.run([sys.executable, '-ISB', '-c', script, str(package)],
                cwd=neutral, env={**os.environ, 'PYTHONPATH': str(source)},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertFalse(any(path.suffix == '.env' for path in context.rglob('*')))


if __name__ == '__main__':
    unittest.main()
