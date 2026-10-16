"""Конечный словарь CONTROL; отсутствие Permission в БД не даёт superuser обхода."""
CATALOG_CUSTOM = {
    'project': ('view_pool', 'configure_project', 'set_project_limit', 'build_project', 'recover_build'),
    'node': ('configure_node', 'drain_node', 'enable_node', 'forget_node',
             'record_node_application', 'observe_node'),
    'installationconfiguration': ('record_settings_application',),
    'projectpolicysnapshot': ('pin_project_policy',),
}
CATALOG_MODELS = ('project', 'node', 'installationsetting', 'nodesetting', 'projectpolicysnapshot',
                  'imagerevision', 'nodeimage', 'clientregistration', 'workspacebinding')
SERVICE_ACTIONS = frozenset('catalog.' + x for x in (
    'record_node_application', 'observe_node', 'record_settings_application'))
ACTIONS = frozenset(
    ['catalog.' + code for codes in CATALOG_CUSTOM.values() for code in codes] +
    ['catalog.' + verb + '_' + model for model in CATALOG_MODELS for verb in ('add', 'view', 'change', 'delete')] +
    ['auth.' + verb + '_' + model for model in ('user', 'group') for verb in ('add', 'view', 'change', 'delete')] +
    ['access.' + code for code in ('view_role', 'approve_role', 'issue_grant', 'revoke_grant')] +
    ['sessions.' + code for code in ('create_puppet', 'update_puppet', 'restart_puppet', 'recycle_puppet',
      'delete_puppet', 'wipe_puppet', 'stop_allocation', 'send_message', 'dispatch_task', 'view_delivery',
      'register_endpoint', 'deliver_report', 'view_history', 'view_tail', 'read_terminal', 'control_terminal',
      'send_session_command', 'change_session_model', 'view_usage', 'view_landing', 'acquire_landing', 'release_landing')] +
    ['operations.' + code for code in ('stop_puppet_job', 'override_assignment', 'recover_operation',
      'cancel_operation', 'view_operation', 'view_audit', 'diagnose_pool', 'treat_pool',
      'collect_pool_garbage', 'sweep_pool', 'view_node_disk')] +
    ['secrets.' + code for code in ('view_secret_metadata', 'manage_project_secret', 'deliver_secret', 'rotate_proxy_key')] +
    ['preparation.approve_bootstrap', 'controller.manage_proxy', 'controller.view_models', 'builds.force_build'])
# #639: конечный машинный протокол; общие RPC/shell actions не регистрируются.
SERVICE_ACTIONS = SERVICE_ACTIONS | frozenset('execution.' + code for code in (
    'poll_work', 'claim_work', 'report_result', 'read_facts'))
ACTIONS = ACTIONS | SERVICE_ACTIONS | frozenset('execution.' + code for code in (
    'assign_work', 'acquire_landing', 'release_landing', 'reconcile_work', 'view_usage'))

GLOBAL_ACTIONS = frozenset(
    [x for x in ACTIONS if x.startswith('auth.')] +
    ['catalog.' + x for x in ('add_project', 'change_project', 'delete_project', 'configure_project',
      'set_project_limit', 'add_node', 'view_node', 'change_node', 'delete_node', 'configure_node',
      'drain_node', 'enable_node', 'forget_node', 'build_project', 'recover_build',
      'view_installationsetting', 'change_installationsetting', 'view_nodesetting', 'change_nodesetting')] +
    ['sessions.' + x for x in ('view_history', 'view_tail', 'read_terminal', 'control_terminal')] +
    ['operations.' + x for x in ('stop_puppet_job', 'override_assignment', 'recover_operation', 'view_audit',
      'diagnose_pool', 'treat_pool', 'collect_pool_garbage', 'sweep_pool', 'view_node_disk')] +
    ['secrets.rotate_proxy_key', 'controller.manage_proxy', 'execution.view_usage', 'builds.force_build'])
