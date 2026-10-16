"""Конечная схема desired settings; без окружения, файлов и startup I/O.

None у default означает обязательный явный ввод потребителя, а не пустую
строку и не поиск на машине процесса. Bootstrap БД/root key сюда не входит.
Секретные значения не являются settings; публичные *_ref — только opaque
ссылки на версии, проверяемые Secrets при связывании потребителя (#637/#644).
"""
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from .legacy_inputs import classify_legacy


@dataclass(frozen=True)
class Definition:
    kind: str
    node_override: bool = False
    minimum: int = 0
    maximum: int = 0
    choices: tuple = ()
    default: str | int | bool | None = None
    max_length: int = 0
    shape: str = ''

    def validate(self, value):
        expected = {'int': int, 'text': str, 'bool': bool}[self.kind]
        if type(value) is not expected:
            raise ValueError('setting has incorrect type')
        if self.kind == 'int' and not self.minimum <= value <= self.maximum:
            raise ValueError('setting is outside supported bounds')
        if self.choices and value not in self.choices:
            raise ValueError('setting has unsupported value')
        if self.kind == 'text':
            if (len(value) > self.max_length or not value
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError('setting requires bounded nonempty text')
            _text_shape(value, self.shape)
        return value


def _text_shape(value, shape):
    """Проверка синтаксиса, не доступности адреса/файла и не исполнения шаблона."""
    try:
        if shape in ('https', 'origin', 'http', 'nats', 'ldap'):
            url = urlsplit(value)
            schemes = {'https': ('https',), 'origin': ('https',), 'http': ('http', 'https'),
                       'nats': ('nats', 'tls'), 'ldap': ('ldap', 'ldaps')}[shape]
            if (url.scheme not in schemes or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment
                    or any(c.isspace() for c in value)
                    or (url.port is not None and not 1 <= url.port <= 65535)
                    or (shape in ('origin', 'nats', 'ldap') and url.path not in ('', '/'))):
                raise ValueError
        elif shape == 'path':
            if not value.startswith('/') or '..' in value.split('/'):
                raise ValueError
        elif shape == 'network':
            if ip_network(value).num_addresses < 4:
                raise ValueError
        elif shape == 'ip':
            ip_address(value)
        elif shape == 'ref':
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,255}', value):
                raise ValueError
        elif shape == 'token':
            if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.:@/+{}*-]*', value):
                raise ValueError
        elif shape == 'host':
            if any(c.isspace() or c in '/@?#' for c in value) or value.startswith('-'):
                raise ValueError
    except ValueError:
        # Значения могут быть ошибочно заполнены секретами: не повторяем их.
        raise ValueError('setting has invalid text shape') from None


def integer(default, minimum, maximum, *, node=False):
    return Definition('int', node, minimum, maximum, default=default)


def text(default=None, *, maximum=256, shape='', choices=(), node=False):
    return Definition('text', node_override=node, choices=choices, default=default,
                      max_length=maximum, shape=shape)


def boolean(default, *, node=False):
    return Definition('bool', node_override=node, default=default)


# Группы — реальные владельцы потребления. Это не переименованный env table:
# типы/единицы/пределы заданы здесь, legacy имена ниже не разрешают чтение.
DEFINITIONS = MappingProxyType({
    # Composition/admission; caps из API.md можно только уменьшать.
    'effect_limit': integer(8, 1, 8),
    'queue_limit': integer(256, 1, 256),
    'project_queue_limit': integer(64, 1, 64),
    'node_effect_limit': integer(4, 1, 4, node=True),
    'blocking_limit': integer(8, 1, 8),
    'database_limit': integer(8, 1, 8),
    'db_pool_max': integer(8, 1, 8),
    'instance_db_connections': integer(24, 1, 24),
    'admin_db_headroom': integer(8, 8, 1024),
    'db_connect_timeout_s': integer(5, 1, 5),
    'db_statement_timeout_s': integer(5, 1, 5),
    'db_lock_timeout_s': integer(2, 1, 2),
    'transaction_timeout_s': integer(10, 1, 10),
    'effect_timeout_s': integer(900, 1, 3600),
    'log_level': text('info', choices=('debug', 'info', 'warning', 'error'), node=True),
    'telemetry_enabled': boolean(True, node=True),
    # Внешние endpoints задаются явно; никаких LAN/home/uid автора.
    'public_origin': text(maximum=2048, shape='origin'),
    'git_host': text(shape='host'),
    'nomad_api_url': text(maximum=2048, shape='http'),
    'nats_url': text(maximum=2048, shape='nats'),
    'proxy_url': text(maximum=2048, shape='https'),
    'pool_datacenter': text('default', shape='token'),
    'pool_home': text(maximum=4096, shape='path'),
    'pool_user': text(maximum=64, shape='token'),
    # Spec/preparation defaults; project snapshot задаёт запрос, узел — caps.
    'puppet_memory_mb': integer(8192, 128, 1048576),
    'memory_limit_mb': integer(12288, 128, 1048576),
    'puppet_path': text('/usr/local/bin:/usr/bin:/bin:{HOME}/.local/bin', maximum=4096),
    'puppet_seed': text('.env*,.providers', maximum=4096),
    'fallback_model': text('opus', maximum=128, shape='token'),
    'disk_gb': integer(120, 1, 1048576, node=True),
    'cores': integer(4, 1, 1024, node=True),
    'body_memory_cap_mb': integer(32768, 128, 1048576, node=True),
    'body_disk_cap_gb': integer(400, 1, 1048576, node=True),
    'body_cores_cap': integer(16, 1, 1024, node=True),
    # PVE — параметры конкретного узла. Нет чужой сети/хранилища по умолчанию.
    'pve_storage': text(shape='token', node=True),
    'pve_template': text(maximum=1024, shape='token', node=True),
    'pve_bridge': text(maximum=64, shape='token', node=True),
    'pve_subnet': text(shape='network', node=True),
    'pve_gateway': text(shape='ip', node=True),
    'pve_vmid_base': integer(9000, 100, 999999000, node=True),
    # Disk maintenance, единицы представлены числами, а не '15GB'.
    'sweep_free_min_gb': integer(60, 1, 1048576, node=True),
    'sweep_max_target_gb': integer(15, 1, 1048576, node=True),
    'sweep_stale_days': integer(14, 1, 3650, node=True),
    'sweep_timeout_s': integer(540, 1, 540, node=True),
    'gc_free_min_gb': integer(25, 1, 1048576),
    'gc_max_per_run': integer(1, 1, 100),
    # Provisioning integration, не authority внешних Nomad/CLIProxyAPI.
    'nats_port': integer(4222, 1, 65535),
    'nats_monitor_port': integer(8222, 1, 65535),
    'https_port': integer(443, 1, 65535),
    'http_bind': text('127.0.0.1', shape='host'),
    'http_port': integer(9000, 1, 65535),
    'nomad_rpc_port': integer(4647, 1, 65535),
    'nomad_http_bind': text('127.0.0.1', shape='host'),
    'nomad_job_gc_hours': integer(720, 1, 87600),
    'nomad_data': text(maximum=4096, shape='path'),
    'nomad_version': text('1.10.5', maximum=64, shape='token'),
    'nomad_mirror': text('https://releases.hashicorp.com', maximum=2048, shape='https'),
    'nats_version': text('2.14.6', maximum=64, shape='token'),
    'proxy_version': text('8.0.16', maximum=64, shape='token'),
    'proxy_panel_version': text('1.25.3', maximum=64, shape='token'),
    'proxy_port': integer(8317, 1, 65535),
    'proxy_lan_enabled': boolean(False),
    'proxy_request_max_mb': integer(64, 1, 1024),
    'locale': text('C.UTF-8', maximum=128, shape='token'),
    'ansible_profile': boolean(False),
    'deploy_needs_green': boolean(False),
    # Certificate/CA metadata — references, не локальные private file paths.
    'tls_cert_ref': text(shape='ref'),
    'tls_ca_ref': text(shape='ref'),
    # Django login routing (#633), без file identity backend/callout.
    'auth_provider': text('django', choices=('django', 'ldap', 'django,ldap', 'ldap,django')),
    'ldap_url': text(maximum=2048, shape='ldap'),
    'ldap_bind_dn': text(maximum=1024),
    'ldap_base': text(maximum=1024),
    'ldap_login_attr': text('uid', maximum=64, shape='token'),
    'ldap_name_attrs': text('displayName,cn'),
    'ldap_email_attr': text('mail', maximum=64, shape='token'),
    'ldap_group_base': text(maximum=1024),
    'ldap_group_filter': text('(|(member={dn})(uniqueMember={dn})(memberUid={login}))', maximum=4096),
    'ldap_admin_group': text(maximum=1024),
    'ldap_access_group': text(maximum=1024),
    'ldap_nested': text('direct', choices=('direct', 'ad')),
    'ldap_project_group': text('mop-{project}'),
    'ldap_starttls': boolean(False),
    'ldap_ca_ref': text(shape='ref'),
})


@dataclass(frozen=True)
class Effective:
    values: tuple
    sources: tuple
    missing: tuple


def validate_batch(batch, *, node=False):
    if type(batch) is not dict or not batch:
        raise ValueError('settings batch must be a nonempty mapping')
    return _validate(batch, node=node)


def _validate(batch, *, node):
    if type(batch) is not dict:
        raise ValueError('settings must be a mapping')
    result = {}
    for key, value in batch.items():
        definition = DEFINITIONS.get(key) if type(key) is str else None
        if definition is None or (node and not definition.node_override):
            raise ValueError('unknown setting or invalid scope')
        result[key] = definition.validate(value)
    return result


def resolve(installation, node=None, *, require=()):
    """Только accepted rows > portable defaults; ошибочная БД не становится {}.

    require — конечные ключи конкретного потребителя. Отсутствующий необязательный
    ввод виден в missing; запуск нуждающегося в нём потребителя обязан require.
    """
    installation = _validate(installation, node=False)
    node = _validate({} if node is None else node, node=True)
    if any(type(key) is not str or key not in DEFINITIONS for key in require):
        raise ValueError('unknown required setting')
    values = {key: definition.default for key, definition in DEFINITIONS.items()
              if definition.default is not None}
    sources = {key: 'default' for key in values}
    for batch, source in ((installation, 'installation'), (node, 'node')):
        values.update(batch)
        sources.update({key: source for key in batch})
    if 'pve_subnet' in values and 'pve_gateway' not in values:
        values['pve_gateway'] = str(ip_network(values['pve_subnet']).network_address + 1)
        sources['pve_gateway'] = 'derived'
    _cross_fields(values)
    missing = tuple(sorted(DEFINITIONS.keys() - values.keys()))
    required_missing = set(require) & set(missing)
    if required_missing:
        raise ValueError('required settings are absent: ' + ', '.join(sorted(required_missing)))
    return Effective(tuple(sorted(values.items())), tuple(sorted(sources.items())), missing)


def _cross_fields(values):
    for low, high in (('puppet_memory_mb', 'memory_limit_mb'),
                      ('memory_limit_mb', 'body_memory_cap_mb'), ('disk_gb', 'body_disk_cap_gb'),
                      ('cores', 'body_cores_cap'), ('project_queue_limit', 'queue_limit'),
                      ('db_pool_max', 'instance_db_connections')):
        if values[low] > values[high]:
            raise ValueError('inconsistent settings: ' + low + ' exceeds ' + high)
    if values['gc_free_min_gb'] >= values['sweep_free_min_gb']:
        raise ValueError('GC threshold must be below sweep threshold')
    if 'pve_gateway' in values:
        if 'pve_subnet' not in values:
            raise ValueError('PVE gateway requires a subnet')
        network, gateway = ip_network(values['pve_subnet']), ip_address(values['pve_gateway'])
        if gateway not in network or gateway in (network.network_address, network.broadcast_address):
            raise ValueError('PVE gateway must be a usable subnet address')
    if 'ldap' in values['auth_provider'].split(','):
        if not all(key in values for key in ('ldap_url', 'ldap_base', 'ldap_bind_dn')):
            raise ValueError('LDAP routing requires URL, base and bind identity')
    if 'ldap_url' in values and values['ldap_url'].startswith('ldap://') and not values['ldap_starttls']:
        raise ValueError('LDAP requires verified TLS or StartTLS')
