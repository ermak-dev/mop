"""Регистрация Access без запросов и side effects при импорте."""
from django.apps import AppConfig


class AccessConfig(AppConfig):
    name = 'mop.server.access'
