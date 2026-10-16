"""Настройки framework до ORM-конфигурации предметных модулей."""
import os
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
    'mop.server.catalog',
    'mop.server.access',
    'mop.server.secrets',
    'mop.server.preparation',
    'mop.server.operations',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
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
AUTHENTICATION_BACKENDS = ['mop.server.controller.authbackend.ControllerBackend']
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
}
