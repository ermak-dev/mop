"""Один словарь CONTROL-действий и их конъюнкций; вид Actor его не читает.

Отсутствие Permission в БД не даёт superuser обхода. Каждый action имеет
ровно одного владельца регистрации (canonical ContentType, #670/#685):
логический sessions-namespace и его execution-алиас удалены вместе с
дублирующими списками — ACTIONS/GLOBAL/MACHINE выведены из словаря.
"""
from types import MappingProxyType

from ..catalog.permissions import ACTION_PERMISSIONS as _CATALOG_ACTIONS

# Кастомные CONTROL-права каталога; CRUD-глаголы стандартного Django.
_CATALOG_CUSTOM = {
    'project': ('view_pool', 'configure_project', 'set_project_limit', 'build_project', 'recover_build'),
    'node': ('configure_node', 'drain_node', 'enable_node', 'forget_node',
             'record_node_application', 'observe_node'),
    'installationconfiguration': ('record_settings_application',),
    'projectpolicysnapshot': ('pin_project_policy',),
}
_CATALOG_MODELS = ('project', 'node', 'installationsetting', 'nodesetting', 'projectpolicysnapshot',
                   'imagerevision', 'nodeimage', 'clientregistration', 'workspacebinding')
_VERBS = ('add', 'view', 'change', 'delete')
# execution — app label владельца (Assignment/Endpoint/UsageSource/...);
# бывший логический sessions namespace удалён (#685).
_EXECUTION = ('assign_work',
    'create_puppet', 'update_puppet', 'restart_puppet', 'recycle_puppet', 'delete_puppet',
    'wipe_puppet', 'stop_allocation', 'send_message', 'dispatch_task', 'view_delivery',
    'register_endpoint', 'deliver_report', 'view_history', 'view_tail', 'read_terminal',
    'control_terminal', 'send_session_command', 'change_session_model', 'view_usage',
    'view_landing', 'acquire_landing', 'release_landing', 'reconcile_work', 'read_history',
    'poll_work', 'claim_work', 'report_result', 'read_facts', 'ingest_telemetry', 'ingest_observations')
_OPERATIONS = ('stop_puppet_job', 'override_assignment', 'recover_operation', 'cancel_operation',
    'view_operation', 'view_audit', 'diagnose_pool', 'treat_pool', 'collect_pool_garbage',
    'sweep_pool', 'view_node_disk')
_SECRETS = ('view_secret_metadata', 'manage_project_secret', 'deliver_secret', 'rotate_proxy_key')
_ACCESS = ('view_role', 'approve_role', 'issue_grant', 'revoke_grant')


def _identity(prefix, codes):
    return {prefix + code: (prefix + code,) for code in codes}


# Словарь — единственный источник: action → конъюнкция Django permissions.
# Конъюнкции каталога (владелец mop.server.catalog.permissions) перекрывают
# identity-заготовки; auth.* — логическое человеческое имя User/Group, строки
# permission живут под app label владельца модели (actor для User).
ACTION_PERMISSIONS = MappingProxyType({
    **_identity('catalog.', (code for codes in _CATALOG_CUSTOM.values() for code in codes)),
    **_identity('catalog.', (verb + '_' + model for model in _CATALOG_MODELS for verb in _VERBS)),
    **_CATALOG_ACTIONS,
    **{'auth.' + verb + '_user': ('actor.' + verb + '_actor',) for verb in _VERBS},
    **_identity('auth.', (verb + '_group' for verb in _VERBS)),
    **_identity('access.', _ACCESS),
    **_identity('execution.', _EXECUTION),
    **_identity('operations.', _OPERATIONS),
    **_identity('secrets.', _SECRETS),
    'preparation.approve_bootstrap': ('preparation.approve_bootstrap',),
    'controller.manage_proxy': ('controller.manage_proxy',),
    'controller.view_models': ('controller.view_models',),
    'builds.force_build': ('builds.force_build',),
})

ACTIONS = frozenset(ACTION_PERMISSIONS)

# #639: конечный машинный протокол; общие RPC/shell actions не регистрируются.
# Адресная выдача всё ещё требует конкретное текущее назначение и grant;
# enrollment этой одной permission не даёт secret management/human authority.
MACHINE_ACTIONS = frozenset(('catalog.record_node_application', 'catalog.observe_node',
    'catalog.record_settings_application', 'execution.poll_work', 'execution.claim_work',
    'execution.report_result', 'execution.read_facts', 'execution.ingest_telemetry',
    'execution.ingest_observations', 'secrets.deliver_secret'))

# Инвариант оставшейся scope-таблицы: у installation-wide действий нет строки
# проекта, поэтому проектная membership не может выразить их границу; право
# Django само по себе scope не несёт. Список именован и конечен.
GLOBAL_ACTIONS = frozenset(
    [x for x in ACTIONS if x.startswith('auth.')] +
    ['catalog.' + x for x in ('add_project', 'change_project', 'delete_project', 'configure_project',
      'set_project_limit', 'add_node', 'view_node', 'change_node', 'delete_node', 'configure_node',
      'drain_node', 'enable_node', 'forget_node', 'build_project', 'recover_build',
      'view_installationsetting', 'change_installationsetting', 'view_nodesetting', 'change_nodesetting')] +
    ['execution.' + x for x in ('view_history', 'view_tail', 'read_terminal', 'control_terminal')] +
    ['operations.' + x for x in ('stop_puppet_job', 'override_assignment', 'recover_operation', 'view_audit',
      'diagnose_pool', 'treat_pool', 'collect_pool_garbage', 'sweep_pool', 'view_node_disk')] +
    ['secrets.rotate_proxy_key', 'controller.manage_proxy', 'builds.force_build', 'execution.view_usage'])


def permission_names(action):
    """Конъюнкция прав действия из единственного словаря; () для неизвестного."""
    return ACTION_PERMISSIONS.get(action, ())
