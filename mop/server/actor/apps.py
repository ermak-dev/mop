"""Регистрация Actor без запросов и side effects при импорте."""
from django.apps import AppConfig


class ActorConfig(AppConfig):
    name = 'mop.server.actor'
    verbose_name = 'Actor'
