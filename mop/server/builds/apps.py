"""Image build metadata без startup I/O."""
from django.apps import AppConfig


class BuildsConfig(AppConfig):
    name = 'mop.server.builds'
    default_auto_field = 'django.db.models.BigAutoField'
