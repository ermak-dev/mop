"""Обязательные capabilities для portable conditional UniqueConstraints."""
from django.core.checks import Error, Tags, register
from django.db import connections


@register(Tags.database)
def secret_guarantees(app_configs=None, databases=None, **kwargs):
    return [Error('Secret lifecycle requires conditional unique constraints.', id='secrets.E001')
            for alias in databases or ['default']
            if not connections[alias].features.supports_partial_indexes]
