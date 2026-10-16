"""Регистрация только инфраструктурных проверок нового контроллера."""
from django.apps import AppConfig


class ControllerConfig(AppConfig):
    name = 'mop.server.controller'
    verbose_name = 'Controller foundation'

    def ready(self):
        from . import checks  # noqa: F401
