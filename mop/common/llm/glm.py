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


# ─── проба квоты (#287) ──────────────────────────────────────────────────
# Ручки страницы z.ai/manage-apikey/coding-plan/personal/usage отвечают самому
# ключу API, без сессии браузера; те же вызовы делает официальный плагин
# glm-plan-usage. Документации нет, поэтому разбор терпим к форме: окна
# различаются по unit/number, тип лимита -- CREDIT_LIMIT у нынешних тарифов,
# TOKENS_LIMIT у прежних. Квота -- на аккаунт, не на ключ.
import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15
QUOTA_PATH = "/api/monitor/usage/quota/limit"
# Окна: (unit, number) -> имя. 3 -- часы, 6 -- недели.
WINDOWS = {(3, 5): "5h", (6, 1): "weekly"}


def host_of(env):
    """Хост ручек мониторинга: ANTHROPIC_BASE_URL профиля без пути."""
    base = (env or {}).get("ANTHROPIC_BASE_URL") or ENV["ANTHROPIC_BASE_URL"]
    u = urllib.parse.urlsplit(base)
    return f"{u.scheme}://{u.netloc}"


def _get(key, path, params=None):
    """GET с ключом -> разобранный JSON; ошибка HTTP/сети -- {"error": …}."""
    url = host_of(ENV) + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json",
        "Accept-Language": "en-US,en"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _window(limit):
    return WINDOWS.get((limit.get("unit"), limit.get("number")),
                       f"{limit.get('number')}x{limit.get('unit')}")


def status_of(payload):
    """Ответ quota/limit -> CredStatus. Чистая: разбираема без сети.

    Исчерпано -- remaining 0 или percentage >= 100 в любом окне: quota_wait
    до ближайшего сброса среди исчерпанных. Не 200/success -- ключ мёртв:
    needs_login. Иначе active с загрузкой худшего окна."""
    from ..domain import CredStatus
    if not isinstance(payload, dict):
        return CredStatus("needs_login", detail="no answer")
    if payload.get("error"):
        return CredStatus("needs_login", detail=str(payload["error"]))
    if not payload.get("success") or payload.get("code") != 200:
        return CredStatus("needs_login", detail=str(payload.get("msg") or "refused"))
    limits = (payload.get("data") or {}).get("limits") or []
    rows, exhausted, worst = [], [], 0
    order = list(WINDOWS.values())
    for lim in limits:
        pct = int(lim.get("percentage") or 0)
        worst = max(worst, pct)
        name = _window(lim)
        rank = order.index(name) if name in order else len(order)
        if lim.get("remaining") == 0 or pct >= 100:
            exhausted.append((int((lim.get("nextResetTime") or 0) // 1000), name))
            rows.append((0, rank, f"{name} window exhausted"))
        else:
            rows.append((1, rank, f"{name} {pct}%"))
    # Исчерпанные окна первыми, дальше короткое окно раньше длинного: строка
    # читается одинаково, в каком бы порядке провайдер ни отдал лимиты.
    parts = [text for _, _, text in sorted(rows)]
    if exhausted:
        resets, _ = min(exhausted)
        return CredStatus("quota_wait", resets_at=resets or None, percent=worst,
                          detail=", ".join(parts))
    return CredStatus("active", percent=worst, detail=", ".join(parts))


def probe(key):
    """Жив ли ключ и что с квотой -> CredStatus."""
    return status_of(_get(key, QUOTA_PATH))
