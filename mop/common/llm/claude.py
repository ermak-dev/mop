"""Shim над LLM-прокси установки (#382, эпик #379): все профили одинаковы.

Имя живёт, пока жив механизм профилей (#390): меты джобов и --llm ссылаются
на него, и оно обязано резолвиться. Содержимое -- тот же прокси и тот же
ключ, что у glm: адрес из настройки, карта моделей glm-5.3[1m]. Прежний
логин claude.ai и его проба ушли: квоты видит панель прокси.
"""

from .. import config

KEY = "MOP_PROXY_KEY"
ENV = {
    "ANTHROPIC_BASE_URL": config.get("MOP_PROXY_URL"),
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
    "API_TIMEOUT_MS": "3000000",
}
