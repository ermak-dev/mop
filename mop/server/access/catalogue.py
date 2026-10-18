"""Конечный advisory-каталог действий #633; authority остаётся у use case.

Ни kind, ни содержимое программы, ни список Django permissions не заменяют
Access.authorize. Отсутствующая область не означает все проекты установки.
"""
from uuid import UUID

from . import services as access
from .permissions import ACTION_PERMISSIONS


def action_permissions(actor, *, project_id=None, node_id=None, client_id=None, grants=None):
    """Материализованный снимок одного явного scope, без кэша authority.

    Второй проход пересекает результат с живым решением: отзыв во время
    построения не оставляет ранее разрешённое действие. Текущий credential
    проверяется до, между и после проходов; каждый authorize читает свежий
    Actor и scope. Это не обещание будущего допуска команды/физического эффекта.
    """
    values = (project_id, node_id, client_id)
    if any(value is not None and type(value) is not UUID for value in values):
        raise ValueError('canonical scope ids required')
    actor = access.current_actor(actor, grants=grants)
    scope = {name: () if value is None else (value,) for name, value in zip(
        ('project_ids', 'node_ids', 'client_ids'), values)}
    gate = access.Access(grants=grants)

    def permitted(actions):
        result = []
        for action in actions:
            try:
                gate.authorize(actor, action, **scope)
            except access.Denied:
                continue
            result.append(action)
        return tuple(result)

    actions = permitted(sorted(ACTION_PERMISSIONS)) if any(values) else ()
    access.current_actor(actor, grants=grants)
    actions = permitted(actions)
    access.current_actor(actor, grants=grants)
    return actions
