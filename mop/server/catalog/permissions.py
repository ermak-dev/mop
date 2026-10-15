"""Конечный контракт action → все Django permissions; scope принадлежит #633."""
from types import MappingProxyType


# Это данные для authorizer, не backend и не вывод прав из имени/флагов.
ACTION_PERMISSIONS = MappingProxyType({
    'catalog.add_project': ('catalog.add_project', 'catalog.configure_project'),
    'catalog.view_project': ('catalog.view_project',),
    'catalog.change_project': ('catalog.change_project', 'catalog.configure_project'),
    'catalog.set_project_limit': (
        'catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit'),
    'catalog.delete_project': ('catalog.delete_project', 'catalog.configure_project'),
    'catalog.add_node': ('catalog.add_node', 'catalog.configure_node'),
    'catalog.view_node': ('catalog.view_node',),
    'catalog.change_node': ('catalog.change_node', 'catalog.configure_node'),
    'catalog.delete_node': ('catalog.delete_node', 'catalog.configure_node'),
    'catalog.record_node_application': ('catalog.record_node_application',),
    'catalog.observe_node': ('catalog.observe_node',),
    'catalog.view_installationsetting': ('catalog.view_installationsetting',),
    'catalog.change_installationsetting': ('catalog.change_installationsetting',),
    'catalog.view_nodesetting': ('catalog.view_nodesetting',),
    'catalog.change_nodesetting': ('catalog.change_nodesetting', 'catalog.configure_node'),
    'catalog.record_settings_application': ('catalog.record_settings_application',),
    'catalog.pin_project_policy': ('catalog.pin_project_policy',),
    'catalog.view_projectpolicysnapshot': ('catalog.view_projectpolicysnapshot',),
    'catalog.add_imagerevision': ('catalog.add_imagerevision',),
    'catalog.change_nodeimage': ('catalog.change_nodeimage',),
    'catalog.add_clientregistration': ('catalog.add_clientregistration',),
    'catalog.change_workspacebinding': ('catalog.change_workspacebinding',),
    'catalog.view_workspacebinding': ('catalog.view_workspacebinding',),
})
