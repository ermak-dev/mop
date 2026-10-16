"""Решение по каждому старому входу, не import/conversion/fallback API.

Здесь только имена из исходников; ни старые значения, ни .env не читаются.
Target указывает нового владельца; преобразование строк старой установки
не реализовано и не разрешено этим перечнем (fresh-install #624).
"""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Disposition:
    category: str
    target: str
    reason: str


_SETTINGS = {
    'MOP_SERVER_LAN': 'public_origin',
    'MOP_GIT_HOST': 'git_host',
    'MOP_HOME': 'pool_home',
    'MOP_USER': 'pool_user',
    'MOP_POOL_DC': 'pool_datacenter',
    'MOP_NOMAD_DATA': 'nomad_data',
    'MOP_NOMAD_VERSION': 'nomad_version',
    'MOP_NOMAD_MIRROR': 'nomad_mirror',
    'NOMAD_ADDR': 'nomad_api_url',
    'MOP_NOMAD_PORT': 'nomad_api_url',
    'MOP_NOMAD_HTTP_BIND': 'nomad_http_bind',
    'MOP_NOMAD_JOB_GC': 'nomad_job_gc_hours',
    'MOP_NOMAD_RPC_PORT': 'nomad_rpc_port',
    'MOP_NATS_PORT': 'nats_port',
    'MOP_NATS_VERSION': 'nats_version',
    'MOP_NATS_MONITOR_PORT': 'nats_monitor_port',
    'MOP_ANSIBLE_PROFILE': 'ansible_profile',
    'MOP_LOCALE': 'locale',
    'MOP_AUTH_PROVIDER': 'auth_provider',
    'MOP_LDAP_URL': 'ldap_url',
    'MOP_LDAP_BIND_DN': 'ldap_bind_dn',
    'MOP_LDAP_BASE': 'ldap_base',
    'MOP_LDAP_LOGIN_ATTR': 'ldap_login_attr',
    'MOP_LDAP_NAME_ATTR': 'ldap_name_attrs',
    'MOP_LDAP_EMAIL_ATTR': 'ldap_email_attr',
    'MOP_LDAP_GROUP_BASE': 'ldap_group_base',
    'MOP_LDAP_GROUP_FILTER': 'ldap_group_filter',
    'MOP_LDAP_ADMIN_GROUP': 'ldap_admin_group',
    'MOP_LDAP_ACCESS_GROUP': 'ldap_access_group',
    'MOP_LDAP_NESTED': 'ldap_nested',
    'MOP_LDAP_PROJECT_GROUP': 'ldap_project_group',
    'MOP_LDAP_STARTTLS': 'ldap_starttls',
    'MOP_LDAP_CA_FILE': 'ldap_ca_ref',
    'MOP_PUPPET_MEM_MB': 'puppet_memory_mb',
    'MOP_MEM_MB': 'memory_limit_mb',
    'MOP_PUPPET_PATH': 'puppet_path',
    'MOP_FALLBACK_MODEL': 'fallback_model',
    'MOP_PROXY_URL': 'proxy_url',
    'MOP_PUPPET_SEED': 'puppet_seed',
    'MOP_SWEEP_FREE_MIN_GB': 'sweep_free_min_gb',
    'MOP_SWEEP_MAX_TARGET': 'sweep_max_target_gb',
    'MOP_SWEEP_STALE_DAYS': 'sweep_stale_days',
    'MOP_SWEEP_TIMEOUT': 'sweep_timeout_s',
    'MOP_GC_FREE_MIN_GB': 'gc_free_min_gb',
    'MOP_GC_MAX_PER_RUN': 'gc_max_per_run',
    'MOP_WEB_PORT': 'http_port',
    'MOP_WEB_BIND': 'http_bind',
    'MOP_WEB_EFFECT_TIMEOUT': 'effect_timeout_s',
    'MOP_HTTPS_PORT': 'https_port',
    'MOP_LLM_PROXY_VERSION': 'proxy_version',
    'MOP_LLM_PANEL_VERSION': 'proxy_panel_version',
    'MOP_LLM_PROXY_PORT': 'proxy_port',
    'MOP_LLM_PROXY_LAN': 'proxy_lan_enabled',
    'MOP_LLM_REQUEST_MAX_MB': 'proxy_request_max_mb',
    'MOP_PVE_STORAGE': 'pve_storage',
    'MOP_PVE_TEMPLATE': 'pve_template',
    'MOP_PVE_BRIDGE': 'pve_bridge',
    'MOP_PVE_SUBNET': 'pve_subnet',
    'MOP_PVE_VMID_BASE': 'pve_vmid_base',
    'MOP_PVE_GATEWAY': 'pve_gateway',
    'MOP_DISK_GB': 'disk_gb',
    'MOP_CORES': 'cores',
    'MOP_BODY_MEM_CAP_MB': 'body_memory_cap_mb',
    'MOP_BODY_DISK_CAP_GB': 'body_disk_cap_gb',
    'MOP_BODY_CORES_CAP': 'body_cores_cap',
    'MOP_TLS_CERT': 'tls_cert_ref',
    'MOP_TLS_CA': 'tls_ca_ref',
    'MOP_DEPLOY_NEEDS_GREEN': 'deploy_needs_green',
}

# Категории различают исчезнувший путь и нужный ввод другого владельца.
_OTHER = {
    # Tracker/CI — инструменты разработки, не runtime-потребители пула.
    # Их установка/credential transport не становятся Catalog policy.
    'GITLAB_SITE': Disposition('development_tool', 'GitLab', 'Development tracker/CI endpoint, outside controller runtime; #645 must preserve explicit tooling input.'),
    'GITLAB_PROJECT': Disposition('development_tool', 'GitLab', 'Development repository selection, not managed pool project identity.'),
    'GITLAB_USER': Disposition('development_tool', 'GitLab', 'Development tool login, not a Django pool principal.'),
    'GITLAB_PASSWORD': Disposition('development_tool', 'GitLab', 'Development tool credential must stay private and separate from pool settings.'),
    'GITLAB_TOKEN': Disposition('development_tool', 'GitLab', 'Development tracker/CI credential, never a controller/node runtime fallback.'),
    'MOP_OPERATORS': Disposition('removed', 'Django Auth', 'Obsolete comma-separated user list is rejected, not a user enrollment source.'),
    'MOP_ENV_FILE': Disposition('removed', 'Catalog', 'No environment file selector or fallback in the new runtime.'),
    'MOP_BRANCH': Disposition('request_context', 'WorkspaceBinding', 'Explicit command selection is not persistent integration-branch authority.'),
    'MOP_BUS_USER': Disposition('removed', 'Django Auth', 'Token-bound identity replaces a self-declared bus username.'),
    'MOP_DRIVER': Disposition('node_record', 'Node.driver', 'Driver belongs to the managed node identity.'),
    'MOP_SSH_PORT': Disposition('node_record', 'Node.ssh_port', 'Provisioning uses the node SSH endpoint, not an environment override.'),
    'MOP_BODY_EXTRA': Disposition('policy', 'Preparation', 'Installation preparation is accepted pinned policy, not ambient executable paths.'),
    'MOP_BODY_SEED': Disposition('policy', 'Preparation', 'Approved preparation and addressed secret delivery replace unrestricted home-file copying.'),
    'MOP_SOUL_DIRS': Disposition('policy', 'RoleBundle', 'Approved immutable bundles replace runtime directory discovery.'),
    'WINDOWS_MCP_HOST': Disposition('policy', 'RoleBundle', 'Desktop MCP configuration belongs to approved role preparation.'),
    'MAC_MCP_HOST': Disposition('policy', 'RoleBundle', 'Desktop MCP configuration belongs to approved role preparation.'),
    'MOP_PROXY_KEY': Disposition('secret', 'proxy-client', 'Purpose-bound encrypted version and addressed delivery, never an ordinary setting.'),
    'MOP_TLS_KEY': Disposition('secret', 'https-private-key', 'Private key custody belongs to Secrets, not certificate metadata.'),
    'MOP_GIT_KEY': Disposition('secret', 'git-clone-key', 'Explicit encrypted key reference replaces filesystem key discovery.'),
    'MOP_LDAP_BIND_PASSWORD': Disposition('secret', 'ldap-bind', 'Authentication receives an authorized secret reference separately from public LDAP policy.'),
    'NOMAD_TOKEN': Disposition('secret', 'nomad-controller', 'Controller scheduler adapter only; never delivered to clients or nodes.'),
    'MOP_OPERATORS_FILE': Disposition('removed', 'Django Auth', 'No file identity store; new users are enrolled into Django Auth.'),
    'MOP_SOUL_ROLES': Disposition('removed', 'Access', 'Roles are mandatory, not a legacy protocol switch.'),
    'MOP_NATS_WS_PORT': Disposition('removed', 'Channels', 'Humans use authenticated Django WebSockets, not direct management NATS.'),
    'MCP_PORT': Disposition('removed', 'DRF', 'No separate management MCP listener; MCP adapts authenticated API use cases.'),
    'MOP_BUS_CONFIG': Disposition('removed', 'DRF', 'No local bus credential discovery or human management NATS.'),
    'MOP_SERVER_DIR': Disposition('removed', 'ClientRegistration', 'No local persistent server profile directory authority.'),
    'MOP_PROJECT': Disposition('request_context', 'WorkspaceBinding', 'Explicit request selection cannot replace durable authorized workspace binding.'),
}

LEGACY_INPUTS = MappingProxyType({
    **{name: Disposition('setting', target,
        'Explicit typed ORM input; the old value is neither read nor converted.')
       for name, target in _SETTINGS.items()},
    **_OTHER,
})


def classify_legacy(name):
    """Неизвестное имя — пробел покрытия, а не молчаливое удаление."""
    if type(name) is not str or name not in LEGACY_INPUTS:
        raise ValueError('unclassified legacy input')
    return LEGACY_INPUTS[name]
