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
import urllib.parse

from ..domain import CredStatus
from . import _probe

# Ручки мониторинга -- на хосте ANTHROPIC_BASE_URL профиля, без его пути.
_BASE = urllib.parse.urlsplit(ENV["ANTHROPIC_BASE_URL"])
QUOTA_URL = f"{_BASE.scheme}://{_BASE.netloc}/api/monitor/usage/quota/limit"
# Окна: (unit, number) -> имя. 3 -- часы, 6 -- недели.
WINDOWS = {(3, 5): "5h", (6, 1): "weekly"}


def _window(limit):
    return WINDOWS.get((limit.get("unit"), limit.get("number")),
                       f"{limit.get('number')}x{limit.get('unit')}")


def status_of(payload):
    """Ответ quota/limit -> CredStatus. Чистая: разбираема без сети.

    Исчерпано -- remaining 0 или percentage >= 100 в любом окне: quota_wait
    до ближайшего сброса среди исчерпанных. Не 200/success -- ключ мёртв:
    needs_login. Иначе active с загрузкой худшего окна."""
    no = _probe.refused(payload)
    if no:
        return no
    if not payload.get("success") or payload.get("code") != 200:
        return CredStatus("needs_login", detail=str(payload.get("msg") or "refused"))
    limits = (payload.get("data") or {}).get("limits") or []
    rows = []
    order = list(WINDOWS.values())
    for lim in limits:
        pct = int(lim.get("percentage") or 0)
        name = _window(lim)
        rank = order.index(name) if name in order else len(order)
        out = lim.get("remaining") == 0 or pct >= 100
        reset = int((lim.get("nextResetTime") or 0) // 1000)
        rows.append(((not out, rank, _probe.part(name, pct, out)), (name, pct, out, reset)))
    # Исчерпанные окна первыми, дальше короткое окно раньше длинного, при
    # равенстве -- по тексту: строка читается одинаково, в каком бы порядке
    # провайдер ни отдал лимиты.
    return _probe.status([w for _, w in sorted(rows, key=lambda r: r[0])])


def probe(key):
    """Жив ли ключ и что с квотой -> CredStatus."""
    return status_of(_probe.get_json(QUOTA_URL, key, {"Accept-Language": "en-US,en"}))
