"""Реестр кредитов: запись кредита и её чтение, чистая часть (#283).

Кредит -- именованная авторизация у провайдера LLM (логин claude.ai, ключ
GLM, токен `setup-token`), которую сервер выдаёт папетам. Дом кредита на
сервере -- `~/.config/mop/creds/<имя>/`; рядом с секретом лежит `cred.json`
этой формы:

    {"name", "profile", "kind": login|token|key, "owner", "added_at",
     "status": {"kind", "resets_at", "percent", "detail", "probed_at"} | null}

Три статуса, которые видит оператор: активен, ждёт квоты до сброса, ждёт
ручной авторизации. Секрета в записи нет никогда: запись едет по шине в
`mop cred list` и на страницу дашборда.

Только stdlib и без сети: сервер (mop/server/credreg.py) читает и пишет
дома, здесь -- что лежит в файле и как это показать.
"""
import re

from .domain import CredStatus

KINDS = ("login", "token", "key")
# Имя кредита -- имя каталога: буквы, цифры, точка внутри, дефис, подчёркивание.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# За сколько до истечения токена продлевать (#283): claude обновляет его
# сам при запросе, и сервер зовёт `claude -p` в доме кредита заранее.
KEEPALIVE_AHEAD = 2 * 3600
HEADER = ("name", "profile", "kind", "owner", "status", "resets", "used", "age")
WORDS = {"active": "active", "quota_wait": "quota wait", "needs_login": "needs login"}


def check_name(name):
    """Имя кредита либо ValueError: имя становится каталогом."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"credential name {name!r}: letters, digits, '.', '-', '_' only")
    return name


def record(name, profile, kind, owner="", now=0):
    """Новая запись кредита без пробы. Форма закреплена tests/credreg.py."""
    check_name(name)
    if kind not in KINDS:
        raise ValueError(f"credential kind {kind!r}: one of {', '.join(KINDS)}")
    return {"name": name, "profile": profile, "kind": kind, "owner": owner or "",
            "added_at": int(now), "status": None}


def merge_status(rec, status, now):
    """Запись с влитой пробой -- новая запись, вход не трогается."""
    st = status if isinstance(status, CredStatus) else CredStatus(**status)
    return {**rec, "status": {"kind": st.kind, "resets_at": st.resets_at,
                              "percent": st.percent, "detail": st.detail,
                              "probed_at": int(now)}}


def status_of(rec):
    """CredStatus из записи либо None, если пробы не было."""
    st = (rec or {}).get("status")
    if not st:
        return None
    return CredStatus(st.get("kind", "needs_login"), resets_at=st.get("resets_at"),
                      percent=st.get("percent"), detail=st.get("detail") or "")


def span(secs):
    """Промежуток по-человечески: 0m, 59m, 3h, 3d."""
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def row(rec, now):
    """Строка `mop cred list` по записи."""
    st = rec.get("status") or {}
    kind = st.get("kind")
    if not kind:
        word = "unknown"
    elif kind == "needs_login" and st.get("detail"):
        word = f"needs login: {st['detail']}"
    else:
        word = WORDS.get(kind, kind)
    resets = "-"
    if st.get("resets_at"):
        left = int(st["resets_at"]) - now
        resets = "now" if left <= 0 else f"in {span(left)}"
    used = f"{st['percent']}%" if st.get("percent") is not None else "-"
    return (rec["name"], rec.get("profile") or "-", rec.get("kind") or "-",
            rec.get("owner") or "-", word, resets, used,
            span(now - int(rec.get("added_at") or now)))


def rows(records, now):
    """Заголовок и строка на кредит -- для render.table."""
    return [HEADER] + [row(r, now) for r in records]


def needs_keepalive(expires_at, now):
    """Пора ли продлевать токен: срок ближе KEEPALIVE_AHEAD или уже вышел.
    Без срока (ключ, setup-token) продлевать нечего."""
    if expires_at is None:
        return False
    return int(expires_at) - int(now) < KEEPALIVE_AHEAD


def expires_at(credentials):
    """Срок access-токена из .credentials.json (expiresAt в мс) -> секунды|None."""
    try:
        ms = credentials["claudeAiOauth"]["expiresAt"]
        return int(ms) // 1000
    except (KeyError, TypeError, ValueError):
        return None
