"""Новая установка: stdlib-план до framework; без чтения legacy inputs."""
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re

from .controller.bootstrap import private_bytes

IDENTITIES = ('maintenance', 'web', 'worker', 'gateway', 'credentials', 'builder')
RUNTIME_ROLES = ('web', 'worker', 'gateway')
DATABASE_RUNTIME_ROLES = (*RUNTIME_ROLES, 'credential')
DATABASE_ROLES = (*DATABASE_RUNTIME_ROLES, 'maintenance')
CREDENTIAL_CONNECTIONS = 8
DATABASE_CONNECTIONS = 40
CREDENTIAL_CLIENT_ROLES = ('web', 'worker')
ORDER = ('setup', 'postgresql', 'migrations', 'initialize', 'admin', 'configuration',
         'services', 'enrollment', 'image', 'body')
BLOCKERS = (
    (628, 'realtime subscriptions, ACL and socket shutdown composition'),
    (643, 'body launch and reboot-persistent host/PVE routing'),
    (648, 'privileged image executor socket and lifecycle'),
    (669, 'installation credential consumers and transport retirement composition'),
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
    runtime: tuple
    identities: tuple

    def __repr__(self):
        return '<fresh installation specification>'


def _system_package(value):
    """Имя apt-пакета без версии: пин `=X` -- это даунгрейд, а не установка."""
    if type(value) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(:[a-z0-9-]+)?', value):
        raise ValueError('system package names must be plain lowercase apt names')
    return value


def declared_runtime(data):
    """Объявленный runtime: python, приватный venv, apt-пакеты, pip extra (#807).

    Установка не даунгрейдит python: требование `>=X.Y` только расширяет
    допустимое, а системные пакеты не заменяют точные пины pip.
    """
    if type(data) is not dict:
        raise ValueError('runtime must be an object')
    allowed = {'python', 'venv', 'system_packages', 'pip_extra', 'wheels'}
    if set(data) - allowed or not {'python', 'venv', 'system_packages', 'pip_extra'} <= set(data):
        raise ValueError('unknown runtime keys or missing required keys')
    from .install_runtime import requirement_minimum
    requirement_minimum(data['python'])
    venv = _path(data['venv'])
    packages = data['system_packages']
    if type(packages) not in (list, tuple) or not packages:
        raise ValueError('runtime system packages must be a nonempty list')
    names = tuple(dict.fromkeys(_system_package(item) for item in packages))
    if data['pip_extra'] != 'server':
        raise ValueError('runtime pip extra must be server')
    result = {'python': data['python'], 'venv': venv,
              'system_packages': list(names), 'pip_extra': data['pip_extra']}
    if 'wheels' in data:
        result['wheels'] = _path(data['wheels'])
    return result


def specification(data):
    _keys(data, ('installation_id', 'database', 'paths', 'runtime', 'identities'), 'installation')
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
    _keys(paths, ('configuration', 'state', 'backup', 'root_trust'), 'path')
    for value in paths.values():
        _path(value)
    runtime = declared_runtime(data['runtime'])
    # Venv и offline wheels -- часть объявленного runtime, но дерево установки
    # всё равно обязано не пересекаться: private trees не лежат друг в друге.
    trees = {**paths, 'venv': runtime['venv']}
    if 'wheels' in runtime:
        trees['wheels'] = runtime['wheels']
    # Root custody и backup не могут оказаться в web/state/venv дереве.
    items = list(trees.values())
    if any(a == b or PurePosixPath(a) in PurePosixPath(b).parents or
           PurePosixPath(b) in PurePosixPath(a).parents for i, a in enumerate(items) for b in items[i+1:]):
        raise ValueError('installation paths must be distinct nonoverlapping trees')
    identities = data['identities']
    _keys(identities, IDENTITIES, 'identity')
    for value in identities.values():
        _name(value, 'OS')
    if len(set(identities.values())) != len(IDENTITIES) or 'root' in identities.values():
        raise ValueError('installation requires distinct non-root OS identities')
    return Specification(identity, tuple(db.items()), tuple(paths.items()),
                         tuple(runtime.items()), tuple(identities.items()))


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
                            'credential': CREDENTIAL_CONNECTIONS, 'total': DATABASE_CONNECTIONS,
                            'accounted_principals': 'managed-target-logins',
                            'roles': {role: 8 for role in DATABASE_ROLES}},
            'blockers': [{'owner': owner, 'reason': reason} for owner, reason in BLOCKERS],
            'runtime': dict(spec.runtime),
            # #811: не голый `python3 -m venv`: шаг venv -- это interpreter,
            # pip upgrade, extra и import check внутри установки.
            'setup': 'installation venv step (interpreter, pip upgrade, server extra, import check) at ' +
                     dict(spec.runtime)['venv'],
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
    return {'runtime': total, 'credential': CREDENTIAL_CONNECTIONS, 'maintenance': maintenance,
            'total': total + CREDENTIAL_CONNECTIONS + maintenance,
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
    shared = shared_directories(spec)
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
            {'name': 'Allow only rootless frontends to reach the credential socket', 'ansible.builtin.user':
                {'name': '{{ item }}', 'groups': users['credentials'], 'append': True},
             'loop': [users[role] for role in CREDENTIAL_CLIENT_ROLES]},
            {'name': 'Prepare separate root-owned parent trees', 'ansible.builtin.file':
                {'path': '{{ item }}', 'state': 'directory', 'owner': 'root', 'group': 'root', 'mode': '0755'},
             'loop': [paths['configuration'], paths['state']]},
            {'name': 'Prepare purpose-owned private directories', 'ansible.builtin.file':
                {'path': '{{ item.path }}', 'state': 'directory', 'owner': '{{ item.owner }}',
                 'group': '{{ item.owner }}', 'mode': '0700'}, 'loop': directories},
            {'name': 'Prepare narrowly shared static and credential socket directories', 'ansible.builtin.file':
                {'path': '{{ item.path }}', 'state': 'directory', 'owner': '{{ item.owner }}',
                 'group': '{{ item.group }}', 'mode': '0750'}, 'loop': shared},
            {'name': 'Install matching native PostgreSQL client tools', 'ansible.builtin.apt':
                {'name': 'postgresql-client-' + str(db['major']), 'state': 'present'}},
            # #807: системные зависимости из образа; python не пинится и не
            # даунгрейдится -- версия проверяется отдельно в конечном executor.
            {'name': 'Install declared system dependencies from the image', 'ansible.builtin.apt':
                {'name': dict(spec.runtime)['system_packages'], 'state': 'present',
                 'install_recommends': False}},
        ]}]
    return {'applied': False, 'receipt': None, 'units': [], 'playbook': playbook,
            'runtime': dict(spec.runtime),
            'accounts': [{'name': user, 'system': True, 'shell': '/usr/sbin/nologin',
                          'create_home': False} for user in users.values()],
            'directories': directories,
            'shared_directories': shared,
            'postgresql': {'host': db['host'], 'port': db['port'], 'major': db['major'],
                'package': ('postgresql-' if managed else 'postgresql-client-') + str(db['major']),
                'manage_service': managed, 'max_connections': DATABASE_CONNECTIONS + 3,
                'runtime_connections': 24, 'credential_connections': CREDENTIAL_CONNECTIONS,
                'maintenance_connections': 8,
                'reserved_connections': 3, 'tls': 'verify-full', 'tls_ca': db['tls_ca'],
                'network_access': 'operator must restrict to controller identities and backup hosts'},
            'unresolved': ['OS package repository and TLS server key provisioning are operator-owned',
                           'no runtime units until executor and realtime lifecycle contracts are bound']}


def static_root(spec):
    """Собранная статика (#644/#695): общий каталог state/static.

    Пишет только maintenance (collectstatic), читает только web; остальным ролям
    он не нужен. Схема спецификации не расширяется -- путь выводится из state.
    """
    return dict(spec.paths)['state'] + '/static'


def credential_socket_directory(spec):
    """Один installer-owned путь; не private state/config самой credential роли."""
    return dict(spec.paths)['state'] + '/credential-socket'


def shared_directories(spec):
    """Одна definition для review playbook и конечного OS executor."""
    users = dict(spec.identities)
    return [{'path': static_root(spec), 'owner': users['maintenance'], 'group': users['web'], 'mode': '0750'},
            {'path': credential_socket_directory(spec), 'owner': users['credentials'],
             'group': users['credentials'], 'mode': '0750'}]


def database_password(password):
    """Проверка DB-only ввода не требует OS endpoint или root bootstrap."""
    if type(password) is not str or not password or any(c in password for c in '\x00\r\n'):
        raise ValueError('database password must be nonempty single-line text')
    return password


def database_identity(spec, role):
    """Purpose credential единственен; plural credentials -- только OS account/path."""
    if role not in DATABASE_ROLES:
        raise ValueError('this consumer must not receive a database bootstrap')
    return dict(spec.identities)['credentials' if role == 'credential' else role]


def bootstrap_document(spec, role, *, password, credential_uid=None):
    """Credential bytes только в purpose-owned bootstrap, не в плане/артефакте узла."""
    from .controller.bootstrap import Bootstrap, credential_endpoint
    user = database_identity(spec, role)
    database_password(password)
    db, paths = dict(spec.database), dict(spec.paths)
    directory = paths['configuration'] + '/' + ('credentials' if role == 'credential' else role)
    result = Bootstrap(installation_id=spec.identity, role=role,
        django_key=directory + '/django.key',
        database={'ENGINE': 'django.db.backends.postgresql', 'HOST': db['host'],
                  'PORT': str(db['port']), 'NAME': db['name'], 'USER': user,
                  'PASSWORD': password, 'OPTIONS': {'sslmode': 'verify-full', 'sslrootcert': db['tls_ca']}})
    if role == 'maintenance':
        result['root_key'] = paths['root_trust'] + '/root.key'
    elif role == 'credential':
        result['root_key'] = directory + '/root.key'
    if role in ('maintenance', 'web'):
        result['static_root'] = static_root(spec)
    if role in CREDENTIAL_CLIENT_ROLES:
        result['credential_service'] = {'path': credential_socket_directory(spec) + '/service.sock',
                                        'server_uid': credential_uid}
        try:
            credential_endpoint(result)
        except RuntimeError as error:
            raise ValueError(str(error)) from None
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
    # #644/#695: статика Admin собирается тем же maintenance в общий каталог.
    maintenance('collectstatic')
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


def _credential_os_identity(credential_uid, credential_gid, frontend_uids):
    """Точная OS ACL; UID не заменяет Access и не доказывает готовность службы."""
    valid = lambda value: type(value) is int and 0 < value < 2 ** 32 - 1
    if not valid(credential_uid) or not valid(credential_gid) or type(frontend_uids) is not dict or \
            set(frontend_uids) != set(CREDENTIAL_CLIENT_ROLES) or \
            any(not valid(uid) for uid in frontend_uids.values()) or \
            len({credential_uid, *frontend_uids.values()}) != 3:
        raise ValueError('credential service requires distinct non-root OS identities and exact frontend UIDs')


def credential_os_coordinates(spec):
    """Только явный installer render; отсутствующий account не угадывается по caller."""
    import pwd
    import grp
    users = dict(spec.identities)
    try:
        credential = pwd.getpwnam(users['credentials'])
        group = grp.getgrnam(users['credentials'])
        peers = {role: pwd.getpwnam(users[role]).pw_uid for role in CREDENTIAL_CLIENT_ROLES}
    except KeyError:
        raise RuntimeError('credential service OS identities are not provisioned') from None
    if credential.pw_gid != group.gr_gid:
        raise RuntimeError('credential service primary group differs from the provisioned socket group')
    result = dict(credential_uid=credential.pw_uid, credential_gid=group.gr_gid, frontend_uids=peers)
    _credential_os_identity(**result)
    return result


def _credential_unit(spec, *, credential_uid, credential_gid, frontend_uids):
    """Существующий daemon lifecycle; stale endpoint исправляет только оператор."""
    _credential_os_identity(credential_uid, credential_gid, frontend_uids)
    from .controller.bootstrap import credential_endpoint
    paths, users = dict(spec.paths), dict(spec.identities)
    socket_dir = credential_socket_directory(spec)
    try:
        credential_endpoint({'role': 'web', 'credential_service': {
            'path': socket_dir + '/service.sock', 'server_uid': credential_uid}})
    except RuntimeError as error:
        raise ValueError(str(error)) from None
    config, state = paths['configuration'] + '/credentials', paths['state'] + '/credentials'
    # Интерпретатор объявляет установка: venv из runtime, не литерал хоста (#807).
    python = dict(spec.runtime)['venv'] + '/bin/python'
    # -P: WorkingDirectory=<state> не попадает в sys.path, как в bin/mop
    # (#811); state принадлежит роли, но cwd не должен подменять модули.
    command = (python + ' -P -m mop.cli server credentials'
               ' --bootstrap ' + config + '/bootstrap.json' +
               ' --socket ' + socket_dir + '/service.sock --key-id root-v1 --socket-group ' + str(credential_gid))
    for role in CREDENTIAL_CLIENT_ROLES:
        command += ' --peer-uid ' + str(frontend_uids[role])
    hidden = [paths['root_trust'], paths['backup']]
    for role in ('maintenance', *RUNTIME_ROLES, 'builder'):
        hidden.extend(paths[base] + '/' + role for base in ('configuration', 'state'))
    return '\n'.join([
        '[Unit]', 'Description=Fresh mop credential service', 'After=network-online.target',
        '[Service]', 'Type=simple', 'User=' + users['credentials'], 'Group=' + users['credentials'],
        'WorkingDirectory=' + state, 'Environment=MOP_HOST_PYTHON=' + python,
        'ExecStart=' + command, 'Restart=on-failure', 'RestartSec=5',
        'TimeoutStartSec=30', 'TimeoutStopSec=30', 'KillSignal=SIGTERM',
        'NoNewPrivileges=true', 'ProtectSystem=strict', 'ProtectHome=read-only',
        'PrivateTmp=true', 'UMask=0077', 'TasksMax=64', 'LimitNOFILE=1024',
        'CapabilityBoundingSet=', 'AmbientCapabilities=',
        'ReadWritePaths=' + state + ' ' + socket_dir, 'InaccessiblePaths=' + ' '.join(hidden),
        '[Install]', 'WantedBy=multi-user.target', ''])


def service_artifact(spec, values, *, credential_uid=None, credential_gid=None, frontend_uids=None):
    """Производные units; не install/start и не proof готовности executors."""
    from urllib.parse import urlsplit
    from .catalog.definitions import DEFINITIONS
    keys = ('public_origin', 'http_bind', 'http_port', 'db_pool_max',
            'instance_db_connections', 'admin_db_headroom')
    if type(values.get('public_origin')) is not str or not values['public_origin'].startswith('https://'):
        raise ValueError('service composition requires an explicit HTTPS public origin')
    settings = {key: DEFINITIONS[key].validate(values[key]) for key in keys}
    origin = urlsplit(settings['public_origin'])
    budget = connection_budget({role: (1, settings['db_pool_max']) for role in RUNTIME_ROLES},
        runtime=settings['instance_db_connections'], maintenance=settings['admin_db_headroom'])
    paths, users = dict(spec.paths), dict(spec.identities)
    # #807: юнит исполняет интерпретатор объявленного venv, а не литерал
    # машины и не `bin/mop` (чей shebang -- python образа, не установки).
    python = dict(spec.runtime)['venv'] + '/bin/python'
    units = {}
    for role in ('web', 'worker'):
        config, state = paths['configuration'] + '/' + role, paths['state'] + '/' + role
        command = python + ' -P -m mop.server.controller '
        if role == 'web':
            command += ('serve --host ' + settings['http_bind'] + ' --port ' + str(settings['http_port']) +
                        ' --hostname ' + origin.hostname + ' --tls-cert ' + config + '/tls.crt' +
                        ' --tls-key ' + config + '/tls.key')
        else:
            command += 'worker'
        units[role] = '\n'.join([
            '[Unit]', 'Description=Fresh mop ' + role, 'After=network-online.target',
            '[Service]', 'Type=simple', 'User=' + users[role], 'Group=' + users[role],
            'SupplementaryGroups=' + users['credentials'],
            'WorkingDirectory=' + state,
            'Environment=MOP_CONTROLLER_BOOTSTRAP=' + config + '/bootstrap.json',
            # Установка объявляет интерпретатор для HOST-входов (#789/#804);
            # канал -- process environment юнита, никогда не node.env.
            'Environment=MOP_HOST_PYTHON=' + python,
            'ExecStart=' + command, 'Restart=on-failure', 'RestartSec=5',
            'TimeoutStartSec=30', 'TimeoutStopSec=30', 'KillSignal=SIGTERM',
            'NoNewPrivileges=true', 'ProtectSystem=strict', 'ProtectHome=read-only',
            'PrivateTmp=true', 'UMask=0077', 'TasksMax=64', 'LimitNOFILE=1024',
            'CapabilityBoundingSet=' + ('CAP_NET_BIND_SERVICE' if role == 'web' and settings['http_port'] < 1024 else ''),
            'AmbientCapabilities=' + ('CAP_NET_BIND_SERVICE' if role == 'web' and settings['http_port'] < 1024 else ''),
            'ReadWritePaths=' + state,
            'InaccessiblePaths=' + paths['root_trust'] + ' ' + paths['backup'] + ' ' +
                paths['configuration'] + '/maintenance ' + paths['configuration'] + '/credentials ' +
                paths['state'] + '/credentials',
            '[Install]', 'WantedBy=multi-user.target', ''])
    required = {'web': ['bootstrap.json', 'django.key', 'tls.crt', 'tls.key'],
                'worker': ['bootstrap.json', 'django.key']}
    blocked = {'worker': [625], 'credentials': [669], 'builder': [648], 'gateway': [628]}
    if any(value is not None for value in (credential_uid, credential_gid, frontend_uids)):
        units['credentials'] = _credential_unit(spec, credential_uid=credential_uid,
            credential_gid=credential_gid, frontend_uids=frontend_uids)
        required['credentials'] = ['bootstrap.json', 'django.key', 'root.key']
        del blocked['credentials']
    return {'units': units, 'connections': budget, 'applied': False, 'runtime_bound': False,
            'required_artifacts': required, 'blocked_services': blocked, 'services_started': False}


def render_catalog(actor, *, spec=None):
    """Только derived desired artifact: никакого record_*_application за рендер."""
    from .access.services import AuthorizedCatalog
    from .catalog.definitions import DEFINITIONS
    from .controller.runtime import _maintenance_role, verify_installation, short_transaction
    _maintenance_role()
    verify_installation()
    catalog = AuthorizedCatalog()
    coordinates = credential_os_coordinates(spec) if spec is not None else None
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
        result = {'definition_keys': sorted(DEFINITIONS), 'desired_revision': effective.installation_revision,
                  'settings': values, 'connections': budget, 'nodes': nodes, 'applied': False,
                  'receipt': None, 'runtime_bound': False,
                  'blockers': [{'owner': owner, 'reason': reason} for owner, reason in BLOCKERS]}
        if spec is not None:
            result['services'] = service_artifact(spec, values, **coordinates)
        return result


def require_runtime_integration():
    raise RuntimeError('runtime integration is not available: ' + ', '.join('#' + str(owner) for owner, _ in BLOCKERS))
