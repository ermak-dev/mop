"""Локальный portable schema check; не подменяет PostgreSQL gate."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _lib
import hermetic


def main():
    try:
        import django
    except ImportError:
        hermetic.skip('catalogue schema', 'declared server extra is not installed')
        return
    from django.conf import settings
    settings.configure(INSTALLED_APPS=['mop.server.controller', 'mop.server.catalog'],
                       DATABASES={}, DEFAULT_AUTO_FIELD='django.db.models.BigAutoField')
    import django
    django.setup()
    from django.apps import apps
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.state import ProjectState
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
    loader = MigrationLoader(None)
    changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps),
        NonInteractiveMigrationQuestioner()).changes(graph=loader.graph)
    assert not changes, f'Catalogue models require migrations: {changes}'
    protection_reauthorizes_after_external_wait()


def protection_reauthorizes_after_external_wait():
    # HYPOTHESIS: decrypt может ждать provider; прежний scope к возврату устареет.
    import hashlib
    import uuid
    from unittest.mock import patch
    from mop.server.catalog.models import ProjectPolicySnapshot
    from mop.server.catalog.services import Catalog
    from django.db import connection
    class Authority:
        revoked = False
        def authorize(self, *args, **kwargs):
            if self.revoked:
                raise PermissionError('scope revoked while waiting')
    authority = Authority()
    class Protector:
        def reveal(self, ciphertext, **kwargs):
            assert not connection.in_atomic_block, 'external protection runs inside SQL transaction'
            authority.revoked = True
            return b'private bytes'
    row = ProjectPolicySnapshot(project_id=uuid.uuid4(), sequence=1,
        content=b'ciphertext', confidential=True, protection_reference='secret:version',
        digest=hashlib.sha256(b'private bytes').hexdigest(), source_commit='a'*40,
        source_ref='main', provenance='test', cpu=100, memory_mb=256, disk_mb=1024)
    api = Catalog(authorizer=authority, protection=Protector())
    with patch.object(ProjectPolicySnapshot.objects, 'get', return_value=row):
        try:
            api.get_policy('trusted-actor', row.id)
        except PermissionError:
            pass
        else:
            raise AssertionError('private policy disclosed after scope revocation during protection I/O')


if __name__ == '__main__':
    main()
