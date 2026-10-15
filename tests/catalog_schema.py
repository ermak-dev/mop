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


if __name__ == '__main__':
    main()
