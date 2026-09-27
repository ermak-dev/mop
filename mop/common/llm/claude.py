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
import json
import urllib.error
import urllib.request

TIMEOUT = 15
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
BETA = "oauth-2025-04-20"
WINDOWS = (("five_hour", "5h"), ("seven_day", "weekly"))


def _get(token, url):
    """GET токеном -> разобранный JSON; ошибка HTTP -- {"error", "body"}."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": BETA,
        "Accept": "application/json"})
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
    from ..domain import CredStatus
    if not isinstance(payload, dict):
        return CredStatus("needs_login", detail="no answer")
    if payload.get("error"):
        if _scope_insufficient(payload):
            return CredStatus("active", detail="no profile scope: status unknown")
        return CredStatus("needs_login", detail=str(payload["error"]))
    parts, exhausted, worst, seen = [], [], 0, False
    for field, name in WINDOWS:
        win = payload.get(field)
        if not isinstance(win, dict):
            continue
        seen = True
        pct = int(round(float(win.get("utilization") or 0)))
        worst = max(worst, pct)
        if pct >= 100:
            exhausted.append(reset_epoch(win.get("resets_at")) or 0)
            parts.append(f"{name} window exhausted")
        else:
            parts.append(f"{name} {pct}%")
    if not seen:
        return CredStatus("needs_login", detail="no usage windows in the answer")
    if exhausted:
        return CredStatus("quota_wait", resets_at=min(exhausted) or None, percent=worst,
                          detail=", ".join(parts))
    return CredStatus("active", percent=worst, detail=", ".join(parts))


def probe(token):
    """Жив ли логин и что с окнами -> CredStatus."""
    return status_of(_get(token, USAGE_URL))


def profile(token):
    """Кто это: почта и тип подписки из api/oauth/profile, без uuid.
    -> {"email", "subscription"} либо {} при отказе (setup-token без области)."""
    got = _get(token, PROFILE_URL)
    if not isinstance(got, dict) or got.get("error"):
        return {}
    acc, org = got.get("account") or {}, got.get("organization") or {}
    return {"email": acc.get("email") or "",
            "subscription": org.get("rate_limit_tier") or org.get("organization_type") or ""}
