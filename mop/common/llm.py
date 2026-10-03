"""Единственный LLM-сервер установки: прокси на контроллере (#379, #390).

Профилей больше нет: окружение сессии одно для папетов и мастеров --
адрес прокси, карта моделей и клиентский ключ. Ключ ездит узлам строкой
MOP_PROXY_KEY в secrets.env (раздаёт сервис кластера, #391) и лежит в
.env мастерских копий; здесь -- только его имя.
"""
from . import config

# Имя ключа в .env и secrets.env, и переменная, куда его читает claude.
KEY = "MOP_PROXY_KEY"
AUTH_VAR = "ANTHROPIC_AUTH_TOKEN"


def env():
    """Статическое окружение LLM сессии -> {переменная: значение}.

    Карта моделей -- сегодняшняя стенда (glm-5.3 через прокси, #382);
    снимется алиасами opus/sonnet/haiku в самом прокси -- тогда здесь
    останется один адрес. [1m] -- окно контекста: claude срезает суффикс
    перед запросом, без него жмёт auto-compact по 200k."""
    return {
        "ANTHROPIC_BASE_URL": config.get("MOP_PROXY_URL"),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
        "API_TIMEOUT_MS": "3000000",
    }
