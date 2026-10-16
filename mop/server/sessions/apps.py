"""Отдельная метка: sessions уже занята стандартным Django app."""
from django.apps import AppConfig


class ExecutionConfig(AppConfig):
    name = 'mop.server.sessions'
    label = 'execution'
