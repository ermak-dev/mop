"""Регистрация только инфраструктурных проверок нового контроллера."""
from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig


class ControllerAdminConfig(AdminConfig):
    default_site = 'mop.server.controller.adminsite.AccessAdminSite'


class ControllerConfig(AppConfig):
    name = 'mop.server.controller'
    verbose_name = 'Controller foundation'

    def ready(self):
        from . import checks  # noqa: F401
