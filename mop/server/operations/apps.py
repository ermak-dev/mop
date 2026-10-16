"""Durable operations — отдельный предметный app."""
from django.apps import AppConfig


class OperationsConfig(AppConfig):
    name = 'mop.server.operations'
    label = 'operations'
