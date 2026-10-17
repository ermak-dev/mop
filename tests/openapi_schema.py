"""Дрейф публичного OpenAPI артефакта #696: генерация, контракт, детерминизм."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import _lib  # noqa: F401 — lifetime временных файлов
import hermetic  # noqa: E402 — первым отделяет настройки старой машины
from _lib import Checks

ARTEFACT = ROOT / 'docs' / 'API.schema.yaml'


def generate():
    """Тот же артефакт в строку: один вход -- один выход (#696).

    Генерация идёт subprocess'ом серверного модуля: файл проверок не
    импортирует django в своём процессе, как и командлет mop dev openapi.
    """
    result = subprocess.run([sys.executable, '-m', 'mop.server.controller.openapi', '--stdout'],
                            cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip()
                            or f'exit {result.returncode}'))
    return result.stdout


# Публичный контракт #696: маршруты человека и CI. Машинные протоколы
# (machine/*, sessions/telemetry|history, terminal, endpoints, addresses,
# reports, deliveries, admin, gateway и сам schema endpoint) в схему не
# входят. Plain-Django маршруты вне DRF (auth/csrf) генератор не выводит --
# их контрактом остаётся docs/API.md.
PUBLIC = {
    '/api/v1/health': {'get'},
    '/api/v1/usage': {'get'},
    '/api/v1/auth/login': {'post'},
    '/api/v1/auth/logout': {'post'},
    '/api/v1/auth/token': {'post'},
    '/api/v1/auth/token/rotate': {'post'},
    '/api/v1/auth/token/revoke': {'post'},
    '/api/v1/identity': {'get'},
    '/api/v1/projects': {'get', 'post'},
    '/api/v1/projects/{project_id}': {'get', 'put'},
    '/api/v1/projects/{project_id}/commands/build': {'post'},
    '/api/v1/nodes/{node_id}/commands/drain': {'post'},
    '/api/v1/nodes/{node_id}/commands/enable': {'post'},
    '/api/v1/puppets/{target_id}/commands/restart': {'post'},
    '/api/v1/puppets/{target_id}/commands/stop-job': {'post'},
    '/api/v1/puppets/{target_id}/commands/wipe': {'post'},
    '/api/v1/messages': {'post'},
    '/api/v1/dispatches': {'post'},
    '/api/v1/operations': {'get'},
    '/api/v1/operations/{operation_id}': {'get'},
    '/api/v1/puppets': {'get'},
    '/api/v1/puppets/{target_id}': {'get'},
    '/api/v1/snapshot': {'get'},
    '/api/v1/events': {'get'},
}

FORBIDDEN = ('/api/v1/machine/', '/api/v1/sessions/', '/api/v1/terminal/',
             '/api/v1/endpoints', '/api/v1/addresses', '/api/v1/reports',
             '/api/v1/deliveries', '/admin/', '/llm/', '/v8/', '/api/v1/schema')

AUTHENTICATED = [{'tokenAuth': []}, {'cookieAuth': []}]
ANONYMOUS = [{}]


def check_drift(c, text):
    committed = ARTEFACT.read_text() if ARTEFACT.exists() else None
    c.check("#696 docs/API.schema.yaml matches its regeneration: run `mop dev openapi` and commit it",
            committed == text,
            'committed artefact is missing' if committed is None else 'artefact differs from generation')


def check_public_contract(c, document):
    paths = document.get('paths', {})
    for route, methods in sorted(PUBLIC.items()):
        if not c.check(f"#696 public route {route} is described", route in paths,
                       f"paths hold {sorted(paths)}"):
            continue
        present = {method for method in paths[route] if not method.startswith('x-')}
        c.expect(f"#696 route {route} methods", sorted(present), sorted(methods))
    extra = sorted(set(paths) - set(PUBLIC))
    c.check("#696 no route outside the public contract is published", not extra, f"leaked {extra}")
    for route in sorted(paths):
        for fragment in FORBIDDEN:
            c.check(f"#696 machine or private path {route} stays out of the schema",
                    not route.startswith(fragment))
            break


def check_error_envelope(c, document):
    components = document.get('components', {}).get('schemas', {})
    envelope = components.get('Error', {})
    properties = envelope.get('properties', {})
    c.check("#696 Error envelope component is present", bool(envelope),
            f"components hold {sorted(components)}")
    if not envelope:
        return
    c.expect("#696 Error envelope fields", sorted(properties), ['error', 'schema_version'])
    error = properties.get('error', {}).get('properties', {})
    c.expect("#696 Error.error fields", sorted(error), ['code', 'message', 'retryable'])
    version = properties.get('schema_version', {})
    if version.get('$ref'):
        # spectacular выносит enum в именованный компонент (default hook
        # postprocess_schema_enums): литерал читается по ссылке.
        resolved = components.get(version['$ref'].split('/')[-1], {})
        c.expect("#696 Error.schema_version is the literal 1",
                 resolved.get('enum'), [1])
    else:
        c.expect("#696 Error.schema_version is the literal 1",
                 version.get('enum'), [1])
    # Ссылка из операции: конверт описывает отказы, а не висит мёртвым грузом.
    referenced = document.get('paths', {})
    used = any(status.get('content', {}).get('application/json', {}).get('schema', {})
               .get('$ref') == '#/components/schemas/Error'
               for operations in referenced.values()
               for operation in operations.values() if isinstance(operation, dict)
               for status in operation.get('responses', {}).values() if isinstance(status, dict))
    c.check("#696 Error envelope is referenced by public operations", used)


def check_security(c, document):
    schemes = document.get('components', {}).get('securitySchemes', {})
    c.check("#696 token and session security schemes are declared",
            {'tokenAuth', 'cookieAuth'} <= set(schemes), f"schemes hold {sorted(schemes)}")
    for route, method in (('/api/v1/identity', 'get'), ('/api/v1/projects', 'get'),
                          ('/api/v1/health', 'get'), ('/api/v1/snapshot', 'get')):
        security = document['paths'].get(route, {}).get(method, {}).get('security')
        c.check(f"#696 {route} requires authentication", security == AUTHENTICATED,
                f"security {security}")
    for route in ('/api/v1/auth/login', '/api/v1/auth/logout'):
        security = document['paths'].get(route, {}).get('post', {}).get('security')
        c.check(f"#696 {route} is the credential bootstrap and stays anonymous",
                security == ANONYMOUS, f"security {security}")
    # token принимает session или явную пару: ни одна не обязательна.
    token = document['paths'].get('/api/v1/auth/token', {}).get('post', {}).get('security')
    c.check("#696 auth/token accepts a session or an explicit credential pair, neither required",
            token == [{'cookieAuth': []}, {}], f"security {token}")


def check_deterministic(c, text):
    again = generate()
    c.check("#696 regeneration is deterministic", text == again,
            'two runs of the same tree differ')


def main():
    if importlib.util.find_spec('django') is None:
        hermetic.skip('openapi schema', 'declared server extra is not installed')
        return 0
    c = Checks()
    try:
        text = generate()
    except RuntimeError as error:
        c.fail('#696 openapi generation must run offline from the tree', str(error))
        return c.report('openapi_schema')
    import yaml
    document = yaml.safe_load(text)
    check_drift(c, text)
    check_public_contract(c, document)
    check_error_envelope(c, document)
    check_security(c, document)
    check_deterministic(c, text)
    return c.report('openapi_schema')


if __name__ == '__main__':
    sys.exit(main())
