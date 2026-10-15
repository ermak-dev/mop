"""Offline migration metadata без bootstrap, соединения с установкой и runtime claims."""
import io


def migrations(*, apps=(), write=False):
    if write and not apps:
        raise ValueError('migration generation requires explicit app names')
    if any(app not in ('controller', 'catalog', 'secrets') for app in apps):
        raise ValueError('unknown controller schema app')
    from django.conf import settings
    from django.core.management import call_command
    import django
    settings.configure(SECRET_KEY='offline-schema-only', USE_TZ=True,
        INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes',
            'mop.server.controller', 'mop.server.catalog', 'mop.server.secrets'],
        DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
        DEFAULT_AUTO_FIELD='django.db.models.BigAutoField')
    django.setup()
    output = io.StringIO()
    call_command('makemigrations', *apps, interactive=False,
                 check=not write, dry_run=not write, verbosity=1 if write else 0,
                 stdout=output, stderr=output)
    return output.getvalue()
