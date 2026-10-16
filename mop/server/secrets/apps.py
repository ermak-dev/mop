"""Регистрация encrypted secret core без подключения delivery/auth adapters."""
from django.apps import AppConfig


class SecretsConfig(AppConfig):
    name = 'mop.server.secrets'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from . import checks  # noqa: F401
