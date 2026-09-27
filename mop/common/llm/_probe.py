"""Общее у проб провайдеров (#316): GET ключом с разбором JSON и сборка
статуса из окон квоты. Не профиль: plugins.discover пропускает `_*`.

Что у провайдеров своё, остаётся в плагине: адрес и лишние заголовки,
разбор ответа в окна, их порядок в строке, отказ без окон."""
import json
import urllib.error
import urllib.request

from ..domain import CredStatus

TIMEOUT = 15


def get_json(url, token, headers):
    """GET токеном -> разобранный JSON; ошибка HTTP -- {"error", "body"}
    (тело отказа, если это JSON), ошибка сети или не-JSON -- {"error"}."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except (ValueError, OSError):
            body = None
        return {"error": f"HTTP {e.code}", "body": body}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def refused(payload):
    """Не ответ (не словарь) или отказ -> needs_login; иначе None."""
    if not isinstance(payload, dict):
        return CredStatus("needs_login", detail="no answer")
    if payload.get("error"):
        return CredStatus("needs_login", detail=str(payload["error"]))
    return None


def part(name, pct, out):
    """Окно в строке статуса."""
    return f"{name} window exhausted" if out else f"{name} {pct}%"


def status(windows):
    """Окна [(имя, процент, исчерпано, сброс epoch|0)] в порядке строки ->
    CredStatus. Исчерпанное окно -- quota_wait до ближайшего сброса среди
    исчерпанных; иначе active. Загрузка -- худшего окна."""
    parts = [part(name, pct, out) for name, pct, out, _ in windows]
    worst = max([0] + [pct for _, pct, _, _ in windows])
    resets = [reset for _, _, out, reset in windows if out]
    if resets:
        return CredStatus("quota_wait", resets_at=min(resets) or None, percent=worst,
                          detail=", ".join(parts))
    return CredStatus("active", percent=worst, detail=", ".join(parts))
