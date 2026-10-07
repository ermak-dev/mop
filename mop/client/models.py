"""Каталог моделей выбранного LLM-прокси для клиентских команд."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from mop.common import config, creds

TIMEOUT = 10


def ids(payload):
    """Ответ /v1/models -> идентификаторы (#448). Парсер общий (#468):
    серверному фасаду каталога нужен тот же разбор, копии не должно быть."""
    from ..common.llm import catalog_ids
    return catalog_ids(payload)


def available():
    """ID моделей выбранного joined-сервера; ключ не покидает HTTPS и localhost."""
    host = config.get("MOP_SERVER_LAN")
    directory = creds.server_dir(host)
    # Один снимок профиля (#533): читаем готовый человеческий вход, не половины.
    try:
        joined = creds.human_profile(directory)
    except ValueError as e:
        raise RuntimeError(str(e)) from e
    if joined is None:
        # Служебный каталог: человеческий login в него сам запрещён.
        raise RuntimeError(f"no human login for {host} (a machine service "
                           f"directory): use a separate operator account "
                           f"or connection directory")
    url = joined["proxy_url"].rstrip("/") + "/v1/models"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and
                                          parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise RuntimeError("the proxy key needs HTTPS outside localhost")
    pin = creds.cafile(directory) if parsed.hostname == host else None
    tls = ssl.create_default_context(cafile=pin)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            raise RuntimeError("proxy model request must not redirect the client key")

    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls))
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {joined['proxy_key']}"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise RuntimeError(f"proxy model request returned HTTP {response.status}")
            return ids(json.load(response))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"proxy model request returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"proxy model request failed: {exc.reason}") from exc
