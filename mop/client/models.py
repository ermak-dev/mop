"""Каталог моделей выбранного LLM-прокси для клиентских команд."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from mop.common import config, creds

TIMEOUT = 10


def ids(payload):
    """Ответ /v1/models -> идентификаторы; неожиданную форму не скрывать."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("invalid proxy model catalog: expected data list")
    result = []
    for model in payload["data"]:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
            raise ValueError("invalid proxy model catalog: expected model id")
        result.append(model["id"])
    return result


def available():
    """ID моделей выбранного joined-сервера; ключ не покидает HTTPS и localhost."""
    host = config.get("MOP_SERVER_LAN")
    directory = creds.server_dir(host)
    joined = creds.client(directory)
    if joined is None:
        raise RuntimeError(f"no joined proxy configuration for {host}: run mop login --server {host}")
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
