"""Новая установка: stdlib-план до framework; без чтения legacy inputs."""
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re

from .controller.bootstrap import private_bytes

IDENTITIES = ('maintenance', 'web', 'worker', 'gateway', 'credentials', 'builder')
RUNTIME_ROLES = ('web', 'worker', 'gateway')
ORDER = ('setup', 'postgresql', 'migrations', 'initialize', 'admin', 'configuration',
         'services', 'enrollment', 'image', 'body')
BLOCKERS = (
    (628, 'realtime subscriptions, ACL and socket shutdown composition'),
    (643, 'body launch and reboot-persistent host/PVE routing'),
    (648, 'privileged image executor socket and lifecycle'),
    (669, 'credential executor socket, custody and lifecycle'),
)


def _keys(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('unknown ' + label + ' keys or missing required keys')


def _name(value, label):
    if type(value) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', value):
        raise ValueError('invalid ' + label + ' identity')
    return value


def _path(value):
    if type(value) is not str or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or \
            '..' in PurePosixPath(value).parts or str(PurePosixPath(value)) != value or value == '/':
        raise ValueError('installation paths must be absolute canonical paths')
    return value


@dataclass(frozen=True, repr=False)
class Specification:
    identity: str
    database: tuple
    paths: tuple
    identities: tuple

    def __repr__(self):
        return '<fresh installation specification>'


def specification(data):
    _keys(data, ('installation_id', 'database', 'paths', 'identities'), 'installation')
    identity = data['installation_id']
    if type(identity) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', identity):
        raise ValueError('invalid installation identity')
    db = data['database']
    _keys(db, ('topology', 'major', 'host', 'port', 'name', 'tls_ca'), 'database')
    if db['topology'] not in ('remote', 'provisioned') or type(db['major']) is not int or db['major'] not in (16, 17):
        raise ValueError('database requires explicit topology and PostgreSQL 16 or 17')
    if type(db['port']) is not int or not 1 <= db['port'] <= 65535:
        raise ValueError('database port must be within 1..65535')
    if type(db['host']) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}', db['host']):
        raise ValueError('database requires an explicit network host')
    _name(db['name'], 'database')
    _path(db['tls_ca'])
    paths = data['paths']
    _keys(paths, ('venv', 'configuration', 'state', 'backup', 'root_trust'), 'path')
    for value in paths.values():
        _path(value)
    # Root custody и backup не могут оказаться в web/state/venv дереве.
    items = list(paths.values())
    if any(a == b or PurePosixPath(a) in PurePosixPath(b).parents or
           PurePosixPath(b) in PurePosixPath(a).parents for i, a in enumerate(items) for b in items[i+1:]):
        raise ValueError('installation paths must be distinct nonoverlapping trees')
    identities = data['identities']
    _keys(identities, IDENTITIES, 'identity')
    for value in identities.values():
        _name(value, 'OS')
    if len(set(identities.values())) != len(IDENTITIES) or 'root' in identities.values():
        raise ValueError('installation requires distinct non-root OS identities')
    return Specification(identity, tuple(db.items()), tuple(paths.items()), tuple(identities.items()))


def load_specification(path):
    try:
        value = json.loads(private_bytes(path))
    except (ValueError, UnicodeError):
        raise ValueError('installation specification is not valid JSON') from None
    return specification(value)


def plan(spec):
    return {'installation_id': spec.identity, 'order': list(ORDER),
            'database': dict(spec.database), 'production_ready': False,
            'connections': {'runtime': 24, 'maintenance': 8, 'scope': 'instance-role-limits',
                            'roles': {role: 8 for role in (*RUNTIME_ROLES, 'maintenance')}},
            'blockers': [{'owner': owner, 'reason': reason} for owner, reason in BLOCKERS],
            'setup': 'mop server controller setup --venv ' + dict(spec.paths)['venv'],
            'provisioned_database': 'OS PostgreSQL package/service provisioning requires operator execution',
            'maintenance': 'direct configured remote PostgreSQL; HTTP and NATS are not prerequisites',
            'restore': 'native matching-major tools; stopped services; reconcile effects before enabling',
            'root_trust': 'separate protected custody; excluded from database backups',
            'legacy_inputs': False, 'legacy_units_removed': False}


def connection_budget(processes, *, runtime, maintenance):
    """Все process pools вместе; server-side role limits защищают от scale-out."""
    if set(processes) != set(RUNTIME_ROLES):
        raise ValueError('unknown or missing database process pool')
    if type(runtime) is not int or not 1 <= runtime <= 24 or type(maintenance) is not int or maintenance != 8:
        raise ValueError('installation connection budget exceeds 24 runtime plus 8 maintenance')
    total = 0
    for role, limits in processes.items():
        if type(limits) is not tuple or len(limits) != 2 or any(type(v) is not int or v < 1 for v in limits):
            raise ValueError('invalid database process pool')
        count, size = limits
        if size > 8 or count * size > 8:
            raise ValueError('process pool exceeds its instance role connection budget')
        total += count * size
    if total > runtime:
        raise ValueError('process pools exceed the instance database budget')
    return {'runtime': total, 'maintenance': maintenance, 'total': total + maintenance,
            'processes': {role: {'count': count, 'pool_max': size} for role, (count, size) in processes.items()}}


def provisioning_artifact(spec):
    """Ревьюируемая подготовка OS, не фиктивный installed receipt/playbook run."""
    paths, users, db = dict(spec.paths), dict(spec.identities), dict(spec.database)
    directories = []
    for role, user in users.items():
        for base in ('configuration', 'state'):
            directories.append({'path': paths[base] + '/' + role, 'owner': user, 'mode': '0700'})
    for base in ('backup', 'root_trust'):
        directories.append({'path': paths[base], 'owner': users['maintenance'], 'mode': '0700'})
    managed = db['topology'] == 'provisioned'
    playbook = [{'name': 'Prepare a separate fresh controller without starting services',
        'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'become': True,
        'tasks': [
            {'name': 'Create distinct service groups', 'ansible.builtin.group':
                {'name': '{{ item }}', 'system': True}, 'loop': list(users.values())},
            {'name': 'Create locked non-login service identities', 'ansible.builtin.user':
                {'name': '{{ item }}', 'group': '{{ item }}', 'system': True,
                 'shell': '/usr/sbin/nologin', 'create_home': False, 'password_lock': True},
             'loop': list(users.values())},
            {'name': 'Prepare separate root-owned parent trees', 'ansible.builtin.file':
                {'path': '{{ item }}', 'state': 'directory', 'owner': 'root', 'group': 'root', 'mode': '0755'},
             'loop': [paths['configuration'], paths['state']]},
            {'name': 'Prepare purpose-owned private directories', 'ansible.builtin.file':
                {'path': '{{ item.path }}', 'state': 'directory', 'owner': '{{ item.owner }}',
                 'group': '{{ item.owner }}', 'mode': '0700'}, 'loop': directories},
            {'name': 'Install matching native PostgreSQL client tools', 'ansible.builtin.apt':
                {'name': 'postgresql-client-' + str(db['major']), 'state': 'present'}},
        ]}]
    return {'applied': False, 'receipt': None, 'units': [], 'playbook': playbook,
            'accounts': [{'name': user, 'system': True, 'shell': '/usr/sbin/nologin',
                          'create_home': False} for user in users.values()],
            'directories': directories,
            'postgresql': {'host': db['host'], 'port': db['port'], 'major': db['major'],
                'package': ('postgresql-' if managed else 'postgresql-client-') + str(db['major']),
                'manage_service': managed, 'max_connections': 35,
                'runtime_connections': 24, 'maintenance_connections': 8,
                'reserved_connections': 3, 'tls': 'verify-full', 'tls_ca': db['tls_ca'],
                'network_access': 'operator must restrict to controller identities and backup hosts'},
            'unresolved': ['OS package repository and TLS server key provisioning are operator-owned',
                           'no runtime units until executor and realtime lifecycle contracts are bound']}


def bootstrap_document(spec, role, *, password):
    """Credential bytes только в purpose-owned bootstrap, не в плане/артефакте узла."""
    from .controller.bootstrap import Bootstrap
    if role not in (*RUNTIME_ROLES, 'maintenance'):
        raise ValueError('this consumer must not receive a database bootstrap')
    if type(password) is not str or not password or any(c in password for c in '\x00\r\n'):
        raise ValueError('database password must be nonempty single-line text')
    db, paths, users = dict(spec.database), dict(spec.paths), dict(spec.identities)
    result = Bootstrap(installation_id=spec.identity, role=role,
        django_key=paths['configuration'] + '/' + role + '/django.key',
        database={'ENGINE': 'django.db.backends.postgresql', 'HOST': db['host'],
                  'PORT': str(db['port']), 'NAME': db['name'], 'USER': users[role],
                  'PASSWORD': password, 'OPTIONS': {'sslmode': 'verify-full', 'sslrootcert': db['tls_ca']}})
    if role == 'maintenance':
        result['root_key'] = paths['root_trust'] + '/root.key'
    return result


def node_artifact(node, settings):
    """Только materialized Catalog snapshots, явный allowlist без DB/root/secret refs."""
    if not node.deployment_member:
        raise ValueError('node is not a desired deployment member')
    if node.driver not in ('host', 'pve') or type(node.ssh_port) is not int or not 1 <= node.ssh_port <= 65535:
        raise ValueError('invalid node deployment coordinates')
    from .catalog.definitions import DEFINITIONS
    safe = {'log_level', 'telemetry_enabled'}
    values = {key: DEFINITIONS[key].validate(value) for key, value in settings.values if key in safe}
    return {'node_id': str(node.id), 'desired_revision': node.revision, 'applied': False, 'receipt': None,
            'installation_settings_revision': settings.installation_revision,
            'node_settings_revision': settings.node_revision,
            'machine_url': node.control_origin,
            'bootstrap_ssh': {'host': node.ssh_host, 'port': node.ssh_port},
            'bootstrap_address': node.bootstrap_address,
            'lan_route': {'address': node.lan_address, 'driver': node.driver},
            'settings': values, 'consumer_bound': False,
            'blockers': [628, 643, 648, 669]}


def provision_database(spec, admin, *, passwords):
    from .install_postgresql import provision
    return provision(spec, admin, passwords=passwords)


def verify_database(spec, connection):
    from .install_postgresql import verify
    return verify(spec, connection)


def write_private_artifact(path, data):
    from .install_files import write_new
    return write_new(path, data)


def retain_backups(directory, *, keep, apply=False):
    from .install_files import retain
    return retain(directory, keep=keep, apply=apply)


def prepare_database():
    """Schema → installation trust, напрямую через maintenance principal, без API."""
    from .controller.runtime import maintenance, installation_snapshot
    maintenance('migrate')
    maintenance('initialize')
    maintenance('check')
    return installation_snapshot()


def initialize_configuration(actor, values):
    """Первый новый ввод только через авторизованный Catalog, без bootstrap store."""
    from .access.services import AuthorizedCatalog
    from .controller.runtime import _maintenance_role, verify_installation, short_transaction
    _maintenance_role()
    verify_installation()
    catalog = AuthorizedCatalog()
    with short_transaction():
        current = catalog.get_installation_settings(actor)
        if current.revision != 1 or current.values:
            raise RuntimeError('installation configuration already exists; use authorized catalog changes')
        return catalog.set_installation_settings(actor, values, expected_revision=1)


def render_catalog(actor):
    """Только derived desired artifact: никакого record_*_application за рендер."""
    from .access.services import AuthorizedCatalog
    from .catalog.definitions import DEFINITIONS
    from .controller.runtime import _maintenance_role, verify_installation, short_transaction
    _maintenance_role()
    verify_installation()
    catalog = AuthorizedCatalog()
    with short_transaction():
        effective = catalog.get_effective_settings(actor)
        values = dict(effective.values)
        budget = connection_budget({role: (1, values['db_pool_max']) for role in RUNTIME_ROLES},
            runtime=values['instance_db_connections'], maintenance=values['admin_db_headroom'])
        nodes = []
        for node in catalog.list_nodes(actor):
            if node.deployment_member:
                local = catalog.get_effective_settings(actor, node_id=node.id)
                current = catalog.get_node(actor, node.id)
                if current.revision != node.revision:
                    raise RuntimeError('node changed while rendering; retry from a fresh catalog snapshot')
                nodes.append(node_artifact(current, local))
        return {'definition_keys': sorted(DEFINITIONS), 'desired_revision': effective.installation_revision,
                'settings': values, 'connections': budget, 'nodes': nodes, 'applied': False,
                'receipt': None, 'runtime_bound': False,
                'blockers': [{'owner': owner, 'reason': reason} for owner, reason in BLOCKERS]}


def require_runtime_integration():
    raise RuntimeError('runtime integration is not available: ' + ', '.join('#' + str(owner) for owner, _ in BLOCKERS))
