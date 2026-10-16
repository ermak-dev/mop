"""Типы и scopes полного каталога настроек; схема заморожена, не runtime import."""
from django.db import migrations, models


# Только структурные ограничения; формы текста и межстрочные требования
# проверяет общий domain service в той же короткой transaction.
DEFINITIONS = (
    ('effect_limit', 'int', False, 1, 8, ()),
    ('queue_limit', 'int', False, 1, 256, ()),
    ('project_queue_limit', 'int', False, 1, 64, ()),
    ('node_effect_limit', 'int', True, 1, 4, ()),
    ('blocking_limit', 'int', False, 1, 8, ()),
    ('database_limit', 'int', False, 1, 8, ()),
    ('db_pool_max', 'int', False, 1, 8, ()),
    ('instance_db_connections', 'int', False, 1, 24, ()),
    ('admin_db_headroom', 'int', False, 8, 1024, ()),
    ('db_connect_timeout_s', 'int', False, 1, 5, ()),
    ('db_statement_timeout_s', 'int', False, 1, 5, ()),
    ('db_lock_timeout_s', 'int', False, 1, 2, ()),
    ('transaction_timeout_s', 'int', False, 1, 10, ()),
    ('effect_timeout_s', 'int', False, 1, 3600, ()),
    ('log_level', 'text', True, 0, 0, ('debug', 'info', 'warning', 'error')),
    ('telemetry_enabled', 'bool', True, 0, 0, ()),
    ('public_origin', 'text', False, 0, 0, ()),
    ('git_host', 'text', False, 0, 0, ()),
    ('nomad_api_url', 'text', False, 0, 0, ()),
    ('nats_url', 'text', False, 0, 0, ()),
    ('proxy_url', 'text', False, 0, 0, ()),
    ('pool_datacenter', 'text', False, 0, 0, ()),
    ('pool_home', 'text', False, 0, 0, ()),
    ('pool_user', 'text', False, 0, 0, ()),
    ('puppet_memory_mb', 'int', False, 128, 1048576, ()),
    ('memory_limit_mb', 'int', False, 128, 1048576, ()),
    ('puppet_path', 'text', False, 0, 0, ()),
    ('puppet_seed', 'text', False, 0, 0, ()),
    ('fallback_model', 'text', False, 0, 0, ()),
    ('disk_gb', 'int', True, 1, 1048576, ()),
    ('cores', 'int', True, 1, 1024, ()),
    ('body_memory_cap_mb', 'int', True, 128, 1048576, ()),
    ('body_disk_cap_gb', 'int', True, 1, 1048576, ()),
    ('body_cores_cap', 'int', True, 1, 1024, ()),
    ('pve_storage', 'text', True, 0, 0, ()),
    ('pve_template', 'text', True, 0, 0, ()),
    ('pve_bridge', 'text', True, 0, 0, ()),
    ('pve_subnet', 'text', True, 0, 0, ()),
    ('pve_gateway', 'text', True, 0, 0, ()),
    ('pve_vmid_base', 'int', True, 100, 999999000, ()),
    ('sweep_free_min_gb', 'int', True, 1, 1048576, ()),
    ('sweep_max_target_gb', 'int', True, 1, 1048576, ()),
    ('sweep_stale_days', 'int', True, 1, 3650, ()),
    ('sweep_timeout_s', 'int', True, 1, 540, ()),
    ('gc_free_min_gb', 'int', False, 1, 1048576, ()),
    ('gc_max_per_run', 'int', False, 1, 100, ()),
    ('nats_port', 'int', False, 1, 65535, ()),
    ('nats_monitor_port', 'int', False, 1, 65535, ()),
    ('https_port', 'int', False, 1, 65535, ()),
    ('http_bind', 'text', False, 0, 0, ()),
    ('http_port', 'int', False, 1, 65535, ()),
    ('nomad_rpc_port', 'int', False, 1, 65535, ()),
    ('nomad_http_bind', 'text', False, 0, 0, ()),
    ('nomad_job_gc_hours', 'int', False, 1, 87600, ()),
    ('nomad_data', 'text', False, 0, 0, ()),
    ('nomad_version', 'text', False, 0, 0, ()),
    ('nomad_mirror', 'text', False, 0, 0, ()),
    ('nats_version', 'text', False, 0, 0, ()),
    ('proxy_version', 'text', False, 0, 0, ()),
    ('proxy_panel_version', 'text', False, 0, 0, ()),
    ('proxy_port', 'int', False, 1, 65535, ()),
    ('proxy_lan_enabled', 'bool', False, 0, 0, ()),
    ('proxy_request_max_mb', 'int', False, 1, 1024, ()),
    ('locale', 'text', False, 0, 0, ()),
    ('ansible_profile', 'bool', False, 0, 0, ()),
    ('deploy_needs_green', 'bool', False, 0, 0, ()),
    ('tls_cert_ref', 'text', False, 0, 0, ()),
    ('tls_ca_ref', 'text', False, 0, 0, ()),
    ('auth_provider', 'text', False, 0, 0, ('django', 'ldap', 'django,ldap', 'ldap,django')),
    ('ldap_url', 'text', False, 0, 0, ()),
    ('ldap_bind_dn', 'text', False, 0, 0, ()),
    ('ldap_base', 'text', False, 0, 0, ()),
    ('ldap_login_attr', 'text', False, 0, 0, ()),
    ('ldap_name_attrs', 'text', False, 0, 0, ()),
    ('ldap_email_attr', 'text', False, 0, 0, ()),
    ('ldap_group_base', 'text', False, 0, 0, ()),
    ('ldap_group_filter', 'text', False, 0, 0, ()),
    ('ldap_admin_group', 'text', False, 0, 0, ()),
    ('ldap_access_group', 'text', False, 0, 0, ()),
    ('ldap_nested', 'text', False, 0, 0, ('direct', 'ad')),
    ('ldap_project_group', 'text', False, 0, 0, ()),
    ('ldap_starttls', 'bool', False, 0, 0, ()),
    ('ldap_ca_ref', 'text', False, 0, 0, ()),
)


def typed(node):
    valid = models.Q(pk__isnull=True)
    for key, kind, override, low, high, choices in DEFINITIONS:
        if node and not override:
            continue
        clause = models.Q(key=key, value_kind=kind)
        for column in ('text', 'int', 'bool'):
            clause &= models.Q(**{column + '_value__isnull': column != kind})
        if kind == 'int':
            clause &= models.Q(int_value__gte=low, int_value__lte=high)
        if choices:
            clause &= models.Q(**{kind + '_value__in': choices})
        valid |= clause
    return valid


class Migration(migrations.Migration):
    dependencies = [('catalog', '0002_action_permissions')]
    operations = [
        migrations.RemoveConstraint(model_name='installationsetting', name='installation_setting_typed'),
        migrations.RemoveConstraint(model_name='nodesetting', name='node_setting_typed'),
        migrations.AddConstraint(model_name='installationsetting', constraint=models.CheckConstraint(
            condition=typed(False), name='installation_setting_typed')),
        migrations.AddConstraint(model_name='nodesetting', constraint=models.CheckConstraint(
            condition=typed(True), name='node_setting_typed')),
    ]
