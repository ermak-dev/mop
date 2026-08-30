"""z.ai GLM coding plan, https://docs.z.ai/devpack/tool/claude"""

KEY = "Z_AI_KEY"
AUTH_VAR = "ANTHROPIC_AUTH_TOKEN"
ENV = {
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    # [1m] — окно контекста модели, claude срезает суффикс перед
    # запросом (проверено на z.ai). Без него claude считает незнакомую
    # модель 200-килотокенной и жмёт auto-compact вчетверо раньше,
    # чем нужно: у glm-5.3 контекст 1M.
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
    # длинные ходы у GLM легко перебивают дефолтный таймаут клиента
    "API_TIMEOUT_MS": "3000000",
}
