"""Публичный OpenAPI 3 артефакт #696: генерация drf-spectacular без bootstrap.

Схема описывает публичный контракт (человек/CI): subject endpoints DRF из
реального urls.py. Машинные протоколы (machine/*, sessions/telemetry|history,
terminal, endpoints, addresses, reports, deliveries, admin, gateway и сам
schema endpoint) в публичную схему не входят: их контрактом остаются
проверки tests/ и docs/API.md, а не published schema.

Wire-контракт закреплён явными сериализаторами (openapi_schema): публикация
не угадывает внутренние классы и переживает их переименование (#690). Общий
конверт отказа (schema_version + error{code, message, retryable}) добавляется
на каждый публичный operation расширением AutoSchema, без искажения
предметных сериализаторов.

Запуск: python -m mop.server.controller.openapi [путь] (--stdout печатает
артефакт в stdout). Без bootstrap, БД и сети; детерминирован.

Сервирующий endpoint -- controller.openapi_schema.SchemaView: тот же
документ, authenticated staff-only, никогда anonymous.
"""
import os
import sys


TITLE = 'mop controller API'
VERSION = '1'
DESCRIPTION = (
    'Public management contract of the mop controller: commands and queries '
    'for humans and CI. Machine and private runtime protocols (node executor, '
    'credential and endpoint machinery) are deliberately not part of this '
    'schema; their contract is enforced by tests and docs/API.md. Every '
    'operation can refuse with the shared error envelope: schema_version '
    'plus error{code, message, retryable}.')

# Машинные/приватные корни маршрутов: никогда не публикуются. Сам schema
# endpoint тоже корень своего же документа.
MACHINE_ROOTS = ('api/v1/machine/', 'api/v1/sessions/', 'api/v1/terminal/',
                 'api/v1/endpoints', 'api/v1/addresses', 'api/v1/reports',
                 'api/v1/deliveries', 'admin/', 'llm/', 'v8/', 'api/v1/schema')

# Коды отказа общего конверта: максимум того, что реально возникает в
# exception_handler и subject adapters.
ERROR_STATUSES = ('400', '401', '403', '404', '405', '409', '410', '429', '503')


def setup():
    """Offline-настройка Django по образцу controller/schema.py.

    Без bootstrap, БД и сети: генерации нужны только классы view из urls.py.
    Повторный вызов в уже настроенном процессе (живой контроллер, тесты) --
    no-op: глобальные настройки процесса не переопределяются.
    """
    import django
    from django.conf import settings
    if settings.configured or os.environ.get('DJANGO_SETTINGS_MODULE'):
        return
    settings.configure(SECRET_KEY='offline-schema-only', USE_TZ=True,
        ALLOWED_HOSTS=[], ROOT_URLCONF='mop.server.controller.urls',
        AUTH_USER_MODEL='actor.Actor',
        # Тот же состав приложений, что в живом settings.py: urls.py сам
        # обращается к admin site, а классы view тянут subject apps.
        INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes',
            'django.contrib.sessions', 'django.contrib.messages',
            'django.contrib.staticfiles',
            'mop.server.controller.adminapps.ControllerAdminConfig',
            'rest_framework', 'rest_framework.authtoken', 'channels',
            'mop.server.controller.apps.ControllerConfig',
            'mop.server.actor',
            'mop.server.catalog', 'mop.server.secrets', 'mop.server.access',
            'mop.server.preparation', 'mop.server.operations',
            'mop.server.sessions.apps.ExecutionConfig',
            'mop.server.builds.apps.BuildsConfig', 'mop.server.gateway'],
        DATABASES={}, DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
        SPECTACULAR_SETTINGS={'TITLE': TITLE, 'VERSION': VERSION,
                              'DESCRIPTION': DESCRIPTION},
        REST_FRAMEWORK={
            # Живой settings.py задаёт тот же DEFAULT_SCHEMA_CLASS; здесь --
            # автономная копия для offline-генерации без bootstrap.
            'DEFAULT_SCHEMA_CLASS': 'mop.server.controller.openapi_schema.PublicAutoSchema',
            'DEFAULT_AUTHENTICATION_CLASSES': [
                'rest_framework.authentication.TokenAuthentication',
                'rest_framework.authentication.SessionAuthentication'],
            'DEFAULT_PERMISSION_CLASSES': [
                'mop.server.controller.authentication.CurrentActorRequired'],
            'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer']})
    django.setup()


def schema():
    """Публичный документ (dict) из реальных маршрутов urls.py."""
    setup()
    from drf_spectacular.generators import SchemaGenerator
    generator = SchemaGenerator(urlconf='mop.server.controller.urls')
    return generator.get_schema(None, public=True)


def document():
    """YAML-строка артефакта: один вход -- один выход."""
    setup()
    from drf_spectacular.renderers import OpenApiYamlRenderer
    return OpenApiYamlRenderer().render(schema()).decode()


def write(path):
    with open(path, 'w') as stream:
        stream.write(document())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv == ['--stdout']:
        pass
    elif len(argv) == 1 and argv[0] not in ('-h', '--help'):
        pass
    else:
        sys.stderr.write('usage: python -m mop.server.controller.openapi [path|--stdout]\n')
        return 2
    if argv == ['--stdout']:
        sys.stdout.write(document())
    elif argv:
        write(argv[0])
    else:
        from mop.common import config
        write(os.path.join(config.PROJECT, 'docs', 'API.schema.yaml'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
