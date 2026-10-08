"""Единственный LLM-сервер установки: прокси на контроллере (#379, #390).

Профилей больше нет: окружение сессии одно для папетов и мастеров --
адрес прокси, карта моделей и клиентский ключ. Ключ ездит узлам строкой
MOP_PROXY_KEY в secrets.env узлов (раздаёт сервис кластера, #391);
мастер берёт URL и ключ из client.json выбранного joined-сервера.

URL-контракт (#547): канонический адрес -- origin сервера (scheme/host/
port, без /llm и без /v1), /v1/* надстраивает потребитель, и ровно один
раз. Легаси-форма издателя -- https://host:port/llm в уже провалидированном
профиле: canonical() снимает ровно её, и только при ЯВНОМ совпадении
authority (выбранный сервер + сохранённый порт): /llm на чужом хосте --
законный кастомный префикс, обрезание догадкой запрещено. Второго
парсера профиля здесь нет: urllib.parse над строкой URL, уже проверенной
_client_values, -- работа с URL, не разбор формата.
"""
import urllib.parse

from . import config

# Имя ключа в secrets.env узла и переменная, которую читает claude.
KEY = "MOP_PROXY_KEY"
AUTH_VAR = "ANTHROPIC_AUTH_TOKEN"

# Единственный легаси-префикс, который издатель когда-то выдавал (#547):
# nginx отвечал на нём до канонического корня /v1 (#529).
_LEGACY_PREFIX = "/llm"

# Ресурсы прокси, которые mop запрашивает сам; произвольные абсолютные
# пути и обход каталога -- не ресурс, а повод отказаться.
RESOURCES = ("models", "messages")

# Порт HTTPS по умолчанию: implicit в URL равен сохранённому "443".
_DEFAULT_TLS_PORT = "443"


def _authority_matches(url, host, port):
    """Совпадает ли authority URL с явно известным издателем (#547).

    scheme обязан быть https (легаси-форму издатель писал только так),
    hostname -- выбранному серверу, порт -- сохранённому в профиле
    (implicit 443 равен явному :443). Обе подсказки обязательны: один
    хост без порта снял бы /llm с чужого порта того же имени, один порт --
    с чужого хоста; известна только одна -- НЕ наш издатель. Несовпадение
    -- не ошибка: просто не наш случай, вызывающий оставляет URL как
    есть. Источник подсказок -- проверенное значение (порт проверенного
    RPC/профиля), не порт самого URL как сам себе доказательство."""
    if not host or not port:
        return False
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if (parsed.hostname or "").lower() != host.lower():
        return False
    if str(parsed.port or _DEFAULT_TLS_PORT) != str(port):
        return False
    return True


def canonical(url, host=None, port=None):
    """Канонический origin из адреса прокси (#547): -> строка.

    Снимает РОВНО легаси-префикс /llm (с хвостовым слэшем или без) и
    ТОЛЬКО при явном совпадении authority известному издателю (host --
    выбранный сервер, port -- порт из профиля). scheme/hostname/port
    сохраняются в точности. Чужой authority, кастомный путь, http или
    отсутствие подсказок -- вход возвращается неизменным: обрезать
    догадкой нельзя, /llm на чужом хосте бывает законным префиксом."""
    if url is None:
        return None
    if host is None and port is None:
        return url
    if not _authority_matches(url, host, port):
        return url
    parsed = urllib.parse.urlsplit(url)
    if parsed.query or parsed.fragment:
        return url
    path = parsed.path
    if path in (_LEGACY_PREFIX, _LEGACY_PREFIX + "/"):
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return url


def endpoint(url, resource, host=None, port=None):
    """URL ресурса /v1/<resource> по адресу прокси (#547): -> строка.

    База -- origin (канонический или с кастомным путём: custom base
    надстраивается своим /v1, как всегда). Уже-/v1 база не удваивается:
    resource-aware, https://x/v1 + models -> https://x/v1/models, не
    /v1/v1. Ресурс -- один сегмент из RESOURCES: произвольный путь,
    обход каталога, абсолютный URL -- ValueError до сети и до ключа."""
    if resource not in RESOURCES:
        raise ValueError(f"unknown proxy resource {resource!r}: expected one of "
                         f"{', '.join(RESOURCES)}")
    base = canonical(url, host, port)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"invalid proxy base URL {base!r}")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"invalid proxy base URL {base!r}")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path + "/" + resource, "", ""))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path + "/v1/" + resource, "", ""))


def origin(url, host=None, port=None):
    """Origin для ANTHROPIC_BASE_URL (#547): -> строка.

    База с /v1 (например https://x/v1) -- ValueError: claude сам строит
    /v1/messages от origin, и молчаливая передача /v1-базы отдала бы
    /v1/v1 на первом же запросе. Явный отказ лучше тихой поломки; custom
    base без /v1 проходит как есть (никогда не перезаписывать чужой
    выбор), легаси /llm известного издателя снимается канонически."""
    base = canonical(url, host, port)
    parsed = urllib.parse.urlsplit(base)
    if parsed.path.rstrip("/") == "/v1":
        raise ValueError(f"the proxy base {base!r} ends with /v1: ANTHROPIC_BASE_URL "
                         f"needs the origin, the client appends /v1 itself")
    return base


def env():
    """Статическое окружение LLM сессии -> {переменная: значение}.

    Карта моделей -- сегодняшняя стенда (glm-5.3 через прокси, #382);
    снимется алиасами opus/sonnet/haiku в самом прокси -- тогда здесь
    останется один адрес. [1m] -- окно контекста: claude срезает суффикс
    перед запросом, без него жмёт auto-compact по 200k.

    Адрес (#547) -- origin по контракту этой же версии: легаси /llm
    установки на ЕЁ ЖЕ authority снимается, внутренний корень 8317 и
    чужой кастомный путь проходят насквозь."""
    return {
        "ANTHROPIC_BASE_URL": origin(config.get("MOP_PROXY_URL"),
                                     host=config.get("MOP_SERVER_LAN"),
                                     port=config.get("MOP_HTTPS_PORT")),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
        "API_TIMEOUT_MS": "3000000",
    }


def catalog_ids(payload):
    """Ответ GET /v1/models -> [id]. Общий парсер каталога (#448, #468):
    клиент и сервер читают одним кодом, второй копии быть не должно.
    Неожиданная форма -- ValueError, не тихий пустой список."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("invalid proxy model catalog: expected data list")
    out = []
    for model in payload["data"]:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) \
                or not model["id"]:
            raise ValueError("invalid proxy model catalog: expected model id")
        out.append(model["id"])
    return out
