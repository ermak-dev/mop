"""Anthropic — authorization via claude.ai login (mop login)"""

KEY = None
ENV = {}


# ─── проба квоты (#283) ──────────────────────────────────────────────────
# Ручка `GET api.anthropic.com/api/oauth/usage` -- та, на которой стоит
# `/usage` в клиенте: пятичасовое окно и неделя с загрузкой и временем
# сброса. Не документирована и требует области user:profile, которую даёт
# интерактивный логин (`claude auth login`), но не `setup-token`: тому она
# отвечает 403 oauth_scope_insufficient, и это не мёртвый кредит, а «статус
# неизвестен». Кредит здесь -- access-токен из .credentials.json дома.
import datetime

from ..domain import CredStatus
from . import _probe

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA = "oauth-2025-04-20"
WINDOWS = (("five_hour", "5h"), ("seven_day", "weekly"))


def reset_epoch(text):
    """ISO 8601 со смещением (как отдаёт ручка) -> epoch в секундах, либо None."""
    try:
        return int(datetime.datetime.fromisoformat(str(text)).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _scope_insufficient(payload):
    err = ((payload.get("body") or {}).get("error") or {}) if isinstance(payload.get("body"), dict) else {}
    return (err.get("details") or {}).get("error_code") == "oauth_scope_insufficient"


def status_of(payload):
    """Ответ usage -> CredStatus. Чистая: разбираема без сети.

    Окно с utilization >= 100 -- quota_wait до ближайшего сброса среди
    исчерпанных; 403 без области -- active со словом «статус неизвестен»;
    иная ошибка HTTP или ответ без окон -- needs_login."""
    if isinstance(payload, dict) and payload.get("error") and _scope_insufficient(payload):
        return CredStatus("active", detail="no profile scope: status unknown")
    no = _probe.refused(payload)
    if no:
        return no
    windows = []
    for field, name in WINDOWS:
        win = payload.get(field)
        if not isinstance(win, dict):
            continue
        pct = int(round(float(win.get("utilization") or 0)))
        windows.append((name, pct, pct >= 100, reset_epoch(win.get("resets_at")) or 0))
    if not windows:
        return CredStatus("needs_login", detail="no usage windows in the answer")
    return _probe.status(windows)


def probe(token):
    """Жив ли логин и что с окнами -> CredStatus."""
    return status_of(_probe.get_json(USAGE_URL, token, {"anthropic-beta": BETA}))
