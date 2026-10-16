"""Единый субъект #683: люди, агенты и сервисы — один Actor, kind классифицирует.

Это стандартный Django AbstractUser: штатный manager, createsuperuser и
UserAdmin работают без замен. kind — минимальный дискриминатор субъекта;
он ничего не разрешает: права — только стандартные Group/Permission,
проверяемые одним авторизатором. Агент/сервис стабилен между инкарнациями
исполнения: рестарт — запись об исполнении, не новый субъект.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class Actor(AbstractUser):
    """AUTH_USER_MODEL контроллера; второй user-модели нет (#624, #683)."""

    # Human — интерактивный вход; service — техническая учётная с жизненным
    # циклом владеющего приложения; agent — агентская сущность со стабильным
    # субъектом между инкарнациями. Ни одно значение не даёт прав или scope.
    kind = models.CharField(max_length=8, choices=(
        ('human', 'Human'), ('service', 'Service'), ('agent', 'Agent')), default='human')
