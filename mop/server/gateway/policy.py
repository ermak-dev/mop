"""Чистые ограничения внешнего HTTP контракта, без ORM и секретных stores."""
import re
from urllib.parse import urlsplit
from mop.server.management_policy import response_headers, PANEL_MAX as MAX_BODY
UNSAFE = frozenset(('POST', 'PUT', 'PATCH', 'DELETE'))
METHODS = UNSAFE | {'GET', 'HEAD'}
# Внешний product API, не произвольный reverse proxy. Потомки ограничены
# каноническими segments; новый product root требует явного review списка.
ROOTS = frozenset(('config', 'config.yaml', 'credentials', 'plugins', 'logs',
    'observability', 'oauth', 'auth-files', 'usage', 'debug', 'request-log',
    'api-keys', 'gemini-api-key', 'claude-api-key', 'codex-api-key',
    'openai-compatibility', 'ampcode', 'routing', 'quota-exceeded',
    'request-retry', 'max-retry-interval', 'proxy-url', 'logging-to-file',
    'logs-max-total-size-mb', 'usage-statistics-enabled', 'ws-auth',
    'force-model-prefix', 'oauth-excluded-models', 'oauth-model-alias',
    'latest-version'))
# api-call (upstream SSRF helper) намеренно не входит в контракт.


def validate(method, target, body, *, panel=False):
    if method not in METHODS or panel and method != 'GET':
        return 405
    if type(body) is not bytes or len(body) > MAX_BODY:
        return 413
    if not isinstance(target, str) or len(target) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in target):
        return 400
    url = urlsplit(target)
    if url.scheme or url.netloc or url.fragment or len(url.query) > 4096:
        return 400
    if panel:
        return None if target == '/management.html' else 400
    if not url.path.startswith('/v8/management/'):
        return 400
    parts = url.path[len('/v8/management/'):].split('/')
    if 'secret-key' in parts or parts[:2] == ['config', 'management']:
        return 403
    if (not parts or parts[0] not in ROOTS or len(parts) > 12
            or any(not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', part)
                   or part in ('.', '..') for part in parts)):
        return 400
    return None


def request_headers(headers):
    result = {}
    for name, value in headers.items():
        canonical = {'content-type': 'Content-Type', 'accept': 'Accept'}.get(name.lower())
        if canonical and isinstance(value, str) and len(value) <= 1024 and not any(ord(c) < 32 or ord(c) == 127 for c in value):
            result[canonical] = value
    return result


def browser_headers(headers, *, panel=False):
    kept = response_headers(dict(headers))
    result = {name: value for name, value in kept.items()
              if isinstance(value, str) and len(value) <= 1024
              and not any(ord(c) < 32 or ord(c) == 127 for c in value)}
    # Только специальный panel route исполняет upstream HTML.
    if not panel and result.get('Content-Type', '').split(';', 1)[0].lower() not in (
            'application/json', 'text/plain', 'application/yaml', 'text/yaml', 'application/x-yaml'):
        result['Content-Type'] = 'text/plain; charset=utf-8'
    return tuple(result.items())
