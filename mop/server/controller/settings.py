"""Настройки framework до ORM-конфигурации предметных модулей."""
from datetime import timedelta
import os
from pathlib import Path
from .bootstrap import load, private_bytes

if 'DJANGO_ALLOW_ASYNC_UNSAFE' in os.environ:
    raise RuntimeError('DJANGO_ALLOW_ASYNC_UNSAFE is forbidden')
BOOTSTRAP = load(os.environ.get('MOP_CONTROLLER_BOOTSTRAP', ''))
if 'django_key' not in BOOTSTRAP:
    raise RuntimeError('bootstrap Django signing key is required')
SECRET_KEY = private_bytes(BOOTSTRAP['django_key']).decode('ascii')
DEBUG = False
ALLOWED_HOSTS = []
ROOT_URLCONF = 'mop.server.controller.urls'
ASGI_APPLICATION = 'mop.server.controller.asgi.application'
INSTALLED_APPS = [
    'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
    'django.contrib.messages', 'django.contrib.staticfiles',
    'mop.server.controller.adminapps.ControllerAdminConfig',
    'rest_framework', 'rest_framework.authtoken', 'channels',
    'mop.server.controller.apps.ControllerConfig',
    'mop.server.actor',
    'mop.server.catalog',
    'mop.server.access',
    'mop.server.secrets',
    'mop.server.preparation',
    'mop.server.operations',
    'mop.server.sessions.apps.ExecutionConfig',
    'mop.server.builds.apps.BuildsConfig',
    'mop.server.gateway',
    'axes',
]
# Один субъект #683: единый Actor вместо stock auth.User; все identity FK —
# swappable ссылки на него, второй user-модели нет.
AUTH_USER_MODEL = 'actor.Actor'
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # #695: статика до сессий и CSRF; nosniff ставит SecurityMiddleware выше.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Последним (#694): превращает блокировку axes в ответ до view.
    'axes.middleware.AxesMiddleware',
]
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'APP_DIRS': True, 'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request',
                  'django.contrib.auth.context_processors.auth',
                  'django.contrib.messages.context_processors.messages']}}]
from psycopg import IsolationLevel

# Ресурсные пределы foundation: предметные настройки позднее владеет Catalog.
# Пул общий для процесса, а не отдельный пул на каждый ASGI request/thread.
CONTROLLER_DB_TIMEOUT = 5
CONTROLLER_TRANSACTION_TIMEOUT = 10
DATABASES = {'default': {
    **BOOTSTRAP['database'], 'ENGINE': 'mop.server.controller.postgresql',
    'CONN_MAX_AGE': 0, 'ATOMIC_REQUESTS': False,
    'CONN_HEALTH_CHECKS': True,
    'OPTIONS': {**BOOTSTRAP['database']['OPTIONS'],
                'connect_timeout': 5,
                'isolation_level': IsolationLevel.READ_COMMITTED,
                'options': '-c statement_timeout=5000 -c lock_timeout=2000 '
                           '-c idle_in_transaction_session_timeout=10000',
                'pool': {'min_size': 0, 'max_size': 8, 'max_waiting': 16,
                         'timeout': 5, 'max_idle': 30}},
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
TIME_ZONE = 'UTC'
STATIC_URL = '/static/'
# #695: штатный collectstatic пишет в общий ролям каталог из bootstrap. Без него
# (изолированные проверки) хранилище простое: manifest требует собранной статики.
STATIC_ROOT = BOOTSTRAP.get('static_root')
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'
                    if STATIC_ROOT else 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
# Ассеты дашборда раздаются из пакета (#627) тем же origin по /assets/; индекс
# остаётся view приложения (no-store). Хеш в имени Vite -- immutable.
WHITENOISE_ROOT = str(Path(__file__).resolve().parent / 'dist')
# WhiteNoise ждёт здесь сами callable: строку immutable-теста он читает как
# регулярное выражение, а строку add_headers не вызывает вовсе.
from .dashboard import immutable as _immutable, static_headers as _static_headers  # noqa: E402
WHITENOISE_IMMUTABLE_FILE_TEST = _immutable
WHITENOISE_ADD_HEADERS_FUNCTION = _static_headers
# Блокировка axes проверяется ДО bind каталога (#694): токен-эндпоинт не
# становится онлайн-оракулом пароля против LDAP.
AUTHENTICATION_BACKENDS = ['axes.backends.AxesStandaloneBackend',
                           'mop.server.controller.authbackend.ControllerBackend']
# Логин ИЛИ адрес, как прежний самописный счётчик: 10 неудач за 5 минут.
AXES_FAILURE_LIMIT = 10
AXES_COOLOFF_TIME = timedelta(minutes=5)
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']
AXES_RESET_ON_SUCCESS = True
AXES_HTTP_RESPONSE_CODE = 429
# Приватность: логин в записях — HMAC-дайджест (блокировка по тому же ключу
# работает, логина в БД нет); журнал успешных входов не ведётся. IP остаётся
# открытым: поле axes типизировано как IP-адрес, тот же адрес уже есть в
# журнале веб-сервера; записи видны только в Admin и сбрасываются окном.
AXES_USERNAME_CALLABLE = 'mop.server.controller.authbackend.lockout_username'
AXES_DISABLE_ACCESS_LOG = True
AXES_ENABLE_ACCESS_FAILURE_LOG = False
CSRF_FAILURE_VIEW = 'mop.server.controller.authviews.csrf_failure'
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = True
X_FRAME_OPTIONS = 'DENY'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'mop.server.controller.views.exception_handler',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['mop.server.controller.authentication.CurrentActorRequired'],
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
    # Публичное подмножество схем #696: машинные корни исключены, конверт
    # Error описывает отказы каждого operation.
    'DEFAULT_SCHEMA_CLASS': 'mop.server.controller.openapi_schema.PublicAutoSchema',
}
