"""Общий fixture свежей установки (#675): FreshPlanFixture и ROOT.

Не проверка: `mop dev test` и hermetic пропускают `_*.py`. До #675
fixture жил в fresh_install, и install_provision импортировал проверку
из проверки (запрещено tests_lib, #269). Хелпер не импортирует ни одну
проверку и не тянет mop: только stdlib.
"""
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
