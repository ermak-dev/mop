"""Shim над LLM-прокси установки (#382, эпик #379): все профили одинаковы.

Прокси на контроллере -- единственный LLM-сервер: подписки claude/codex и
Anthropic-совместимые ключи за одним /v1/messages, пул аккаунтов и ротация
внутри. Карта моделей -- сегодняшняя стенда (glm): меняется только дорога,
z.ai напрямую -> прокси. Когда имена opus/sonnet/haiku обслужат алиасы в
самом прокси, карту снимут вместе с механизмом профилей (#390).
"""

from .. import config

KEY = "MOP_PROXY_KEY"
ENV = {
    "ANTHROPIC_BASE_URL": config.get("MOP_PROXY_URL"),
    # [1m] — окно контекста модели, claude срезает суффикс перед
    # запросом (проверено на z.ai). Без него claude считает незнакомую
    # модель 200-килотокенной и жмёт auto-compact вчетверо раньше,
    # чем нужно: у glm-5.3 контекст 1M.
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
    # длинные ходы легко перебивают дефолтный таймаут клиента
    "API_TIMEOUT_MS": "3000000",
}
