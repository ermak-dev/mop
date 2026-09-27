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
import calendar
import os
import re
import time

from . import paths
from .domain import CredStatus

KINDS = ("login", "token", "key")
# Имя кредита -- имя каталога: буквы, цифры, точка внутри, дефис, подчёркивание.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# За сколько до истечения токена продлевать (#283): claude обновляет его
# сам при запросе, и сервер зовёт `claude -p` в доме кредита заранее.
KEEPALIVE_AHEAD = 2 * 3600
HEADER = ("name", "profile", "kind", "owner", "status", "resets", "used", "age", "holders")
WORDS = {"active": "active", "quota_wait": "quota wait", "needs_login": "needs login"}
# Логинится только claude (#317): кредит вида login и дом без записи --
# этого профиля; у остальных ключ.
LOGIN_PROFILE = "claude"


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


def span(secs):
    """Промежуток по-человечески: 0m, 59m, 3h, 3d."""
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def status_word(st, words, unknown):
    """Статус кредита словом: пробы не было -- unknown, needs_login несёт
    подробность, остальное -- слово таблицы words (или вид как есть). Правило
    одно (#317), таблица -- у фронтенда: `mop cred list` по-английски,
    страница по-русски."""
    kind = (st or {}).get("kind")
    if not kind:
        return unknown
    word = words.get(kind, kind)
    if kind == "needs_login" and st.get("detail"):
        return f"{word}: {st['detail']}"
    return word


def age(rec, now):
    """Возраст кредита по записи: с added_at до now, коротко (span)."""
    return span(now - int(rec.get("added_at") or now))


def row(rec, now, holders=()):
    """Строка `mop cred list` по записи; holders -- папеты с арендой (#284)."""
    st = rec.get("status") or {}
    word = status_word(st, WORDS, "unknown")
    resets = "-"
    if st.get("resets_at"):
        left = int(st["resets_at"]) - now
        resets = "now" if left <= 0 else f"in {span(left)}"
    used = f"{st['percent']}%" if st.get("percent") is not None else "-"
    return (rec["name"], rec.get("profile") or "-", rec.get("kind") or "-",
            rec.get("owner") or "-", word, resets, used, age(rec, now),
            ",".join(sorted(holders)) if holders else "-")


def rows(records, now, holders=None):
    """Заголовок и строка на кредит -- для render.table. holders --
    {кредит: [папеты]} (#284)."""
    holders = holders or {}
    return [HEADER] + [row(r, now, holders.get(r["name"]) or ()) for r in records]


def needs_keepalive(expires_at, now):
    """Пора ли продлевать токен: срок ближе KEEPALIVE_AHEAD или уже вышел.
    Без срока (ключ, setup-token) продлевать нечего."""
    if expires_at is None:
        return False
    return int(expires_at) - int(now) < KEEPALIVE_AHEAD


# ─── файл кредов claude (#317): путь и схема -- здесь одни ──────────────
def credentials_file(home):
    """Где лежит файл кредов claude в доме home (дом кредита, тела, свой)."""
    return os.path.join(home, paths.CREDENTIALS)


def is_login_file(credentials):
    """Похоже ли разобранное на файл кредов claude: словарь с claudeAiOauth."""
    return isinstance(credentials, dict) and bool(credentials.get("claudeAiOauth"))


def access_token(credentials):
    """Access-токен из разобранного файла кредов либо None."""
    return ((credentials or {}).get("claudeAiOauth") or {}).get("accessToken")


def expires_at(credentials):
    """Срок access-токена из .credentials.json (expiresAt в мс) -> секунды|None."""
    try:
        ms = credentials["claudeAiOauth"]["expiresAt"]
        return int(ms) // 1000
    except (KeyError, TypeError, ValueError):
        return None


# ─── аренда (#284) ────────────────────────────────────────────────────────
def pick(profile, records):
    """Кредит папету без явного --cred: первый по имени активный кредит
    профиля, либо None -- тогда папет живёт логином оператора, как прежде.
    Непробованный не считается: активность -- утверждение пробы, а не
    отсутствие плохих вестей."""
    names = usable(profile, ((r["name"], r.get("profile"), (r.get("status") or {}).get("kind"))
                             for r in records))
    return names[0] if names else None


def usable(profile, entries):
    """Годные кредиты профиля: пробой подтверждённые active, по имени --
    детерминизм при равных. entries -- (имя, профиль, вид статуса). Одно
    правило на выбор аренды (pick) и ярусы (tiers, #317)."""
    return sorted(n for n, p, kind in entries if p == profile and kind == "active")


def without_refresh(credentials):
    """Файл кредов claude без refreshToken -- то, что уезжает в тело.
    Обновляет токен сервер в доме кредита, тела не обновляют ничего:
    провайдер ротирует refresh-токен, и вторая копия умирала бы."""
    out = {}
    for k, v in (credentials or {}).items():
        if k == "claudeAiOauth" and isinstance(v, dict):
            v = {kk: vv for kk, vv in v.items() if kk != "refreshToken"}
        out[k] = v
    return out


_RESET = re.compile(r"reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def reset_time_of(text):
    """Время сброса из текста провала `rate_limit` («Your limit will reset
    at 2026-09-24 15:00:00») -> epoch либо None. Пояс в тексте не назван,
    и часы узла неизвестны: считаем UTC и говорим об этом здесь."""
    m = _RESET.search(text or "")
    if not m:
        return None
    try:
        return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def turn_status(record, changed_at):
    """Что провал хода говорит о кредите (#284) -> CredStatus либо None.

    authentication_failed -- логин протух: needs_login, если дом кредита не
    менялся после хода (changed_at -- mtime секрета либо None); менялся --
    None, вызывающий раздаёт свежее. rate_limit -- ждать сброса из текста,
    billing_error -- ждать без времени. Остальное кредита не касается."""
    if not record or record.get("event") != "StopFailure":
        return None
    code, detail = record.get("error"), record.get("detail") or ""
    if code == "authentication_failed":
        if changed_at is not None and int(changed_at) > int(record.get("at") or 0):
            return None
        return CredStatus("needs_login", detail="login expired")
    if code == "rate_limit":
        return CredStatus("quota_wait", resets_at=reset_time_of(detail), detail=detail[:120])
    if code == "billing_error":
        return CredStatus("quota_wait", detail=detail[:120])
    return None
