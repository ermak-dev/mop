"""Что едет плейбукам --extra-vars (#156).

Жило в config, но тянуло оттуда вверх operators (права NATS) и deps: слой
настроек знал о правах шины. Здесь -- над config: настройки плюс списки,
которые живут в коде одним местом.
"""
from . import config, deps, operators


def playbook_vars():
    """Что едет плейбукам --extra-vars: настройки плюс списки, которые живут
    в коде одним местом. Одна функция на `mop deploy` (через `mop config
    --json`) и на сборку образа (mop/image.py): пока их было две, список
    доезжал до узла и не доезжал до тела, молча.

    MOP_NODE_SCOPED -- узловые настройки, по нему прогон рендерит node.env.
    MOP_SERVER_SCOPED -- {юнит: настройки}, по нему рендерится env юнитов
    сервисов сервера (#176).
    MOP_PIP_DEPS -- python-библиотеки mop (mop/deps.py) для узла, тела и
    `mop setup`."""
    # Настройки процесса (PROCESS_SCOPED) не едут: они про этот процесс, а не
    # про установку -- MOP_PROJECT из шелла мастера ansible ни к чему.
    out = {k: v for k, (v, _) in config.effective().items()
           if k not in config.PROCESS_SCOPED}
    out["MOP_NODE_SCOPED"] = ",".join(config.NODE_SCOPED)
    out["MOP_SERVER_SCOPED"] = {u: list(v) for u, v in config.SERVER_SCOPED.items()}
    out["MOP_PIP_DEPS"] = ",".join(deps.PIP)
    # Права операторов — субъектами, уже разобранные: шаблон конфига NATS
    # не должен разбирать настройку второй раз, иначе два разбора разойдутся
    # молча, и разойдутся они В ПРАВАХ.
    out["MOP_OPERATOR_SUBJECTS"] = {
        name: operators.permissions(op)
        for name, op in operators.parse(out.get("MOP_OPERATORS", "")).items()}
    return out
