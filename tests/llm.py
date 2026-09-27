#!/usr/bin/env python3
"""Проверка реестра LLM-профилей без пула: python3 tests/llm.py

Реестр — чистая функция над каталогом плагинов, и ошибка контракта обязана
находиться здесь, у мастера, а не на узле: KEY уезжает в sed-шаблон врапера,
кривое имя там молча совпадёт нигде, и папет умрёт с «нет ключа» вдали от
причины.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import io
import json
import os
import sys
import types
import urllib.error
import urllib.request

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, llm  # noqa: E402
from mop.common.domain import CredStatus  # noqa: E402
from mop.common.llm import claude, glm  # noqa: E402


def plugin(**attrs):
    """Модуль-плагин с заданными атрибутами; остальное берёт контрактом."""
    mod = types.ModuleType("fake")
    mod.__dict__.update(attrs)
    return mod


# Контракт знает один хук, probe (#310): usage не звал никто, и он снят.
DEF = {"key": None, "auth_var": "ANTHROPIC_AUTH_TOKEN", "env": {}, "doc": "",
       "probe": None}

CASES = [
    # (что проверяем, модуль, ожидание: контракт | None = громкий отказ)
    # ENV обязателен явно, даже пустой: контракт не угадывает намерений.
    ("minimum: empty ENV", plugin(ENV={}), DEF),
    ("key and auth_var read", plugin(ENV={}, KEY="K", AUTH_VAR="VAR"),
     {**DEF, "key": "K", "auth_var": "VAR"}),
    ("description — first line of docstring", plugin(ENV={}, __doc__="one\ntwo"),
     {**DEF, "doc": "one"}),
    ("ENV not declared", plugin(), None),
    ("ENV not a dict", plugin(ENV="A=b"), None),
    ("ENV with a non-string value", plugin(ENV={"A": 5}), None),
    ("KEY not a variable name", plugin(KEY="плохое-имя"), None),
    ("AUTH_VAR not a variable name", plugin(AUTH_VAR="1bad"), None),
    # Пробы провайдера (#287) -- необязательны: плагин без них проходит
    # контракт как прежде, с ними -- контракт их называет; не функция -- отказ.
    ("probe absent -> None", plugin(ENV={}), DEF),
    # usage у плагина (#310) -- не хук: контракт его не читает и не несёт.
    ("a usage attribute is not a hook", plugin(ENV={}, usage=lambda k, s, e: {}), DEF),
    ("probe not callable", plugin(ENV={}, probe="yes"), None),
]

# Ответ z.ai `GET /api/monitor/usage/quota/limit` (26.09.2026, ключ установки):
# пятичасовое окно исчерпано, недельное на 53 %. Числа настоящие.
QUOTA = {"code": 200, "msg": "Operation successful", "success": True, "data": {
    "level": "max", "limits": [
        {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 28000,
         "currentValue": 28052, "remaining": 0, "percentage": 100,
         "nextResetTime": 1790425418306},
        {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 140000,
         "currentValue": 75249, "remaining": 64750, "percentage": 53,
         "nextResetTime": 1790864188983}]}}


def quota(per_window):
    """Тот же ответ с другими окнами: {(unit, number): (percentage, remaining)}."""
    limits = []
    for (unit, number), (pct, remaining) in per_window.items():
        limits.append({"type": "TOKENS_LIMIT", "unit": unit, "number": number,
                       "usage": 100, "currentValue": 100 - remaining,
                       "remaining": remaining, "percentage": pct,
                       "nextResetTime": 1790425418306 + unit * 1000})
    return {"code": 200, "success": True, "data": {"level": "pro", "limits": limits}}


# Статус кредита GLM из ответа квоты (#287): три исхода, которые видит
# оператор. HYPOTHESIS: у плагина нет пробы, и реестр кредитов узнал бы об
# исчерпанной квоте только по провалу хода. SOLUTION: чистая status_of над
# ответом квоты, probe/usage -- необязательные хуки контракта.
# STATUS: FIXED — see #287
STATUS = [
    ("5h window exhausted -> quota_wait until its reset",
     QUOTA, CredStatus("quota_wait", resets_at=1790425418, percent=100,
                       detail="5h window exhausted, weekly 53%")),
    ("nothing exhausted -> active with the worst window",
     quota({(3, 5): (29, 71), (6, 1): (33, 67)}),
     CredStatus("active", resets_at=None, percent=33, detail="5h 29%, weekly 33%")),
    ("TOKENS_LIMIT with remaining 0 -> quota_wait (older plans)",
     quota({(3, 5): (40, 60), (6, 1): (100, 0)}),
     CredStatus("quota_wait", resets_at=1790425424, percent=100,
                detail="weekly window exhausted, 5h 40%")),
    ("two exhausted windows -> the earliest reset",
     quota({(6, 1): (100, 0), (3, 5): (100, 0)}),
     CredStatus("quota_wait", resets_at=1790425421, percent=100,
                detail="5h window exhausted, weekly window exhausted")),
    ("success false -> needs_login",
     {"code": 401, "success": False, "msg": "invalid api key"},
     CredStatus("needs_login", detail="invalid api key")),
    ("HTTP error marker -> needs_login",
     {"error": "HTTP 401"}, CredStatus("needs_login", detail="HTTP 401")),
    ("garbage -> needs_login, not a crash",
     None, CredStatus("needs_login", detail="no answer")),
]


# Ответ `GET api.anthropic.com/api/oauth/usage` токеном интерактивного
# логина (26.09.2026): пятичасовое окно на 29 %, недельное на 33 %. Лишние
# поля срезаны, форма настоящая.
USAGE = {"five_hour": {"utilization": 29.0, "resets_at": "2026-09-26T11:10:00.078381+00:00",
                       "limit_dollars": None, "locked_reason": None},
         "seven_day": {"utilization": 33.0, "resets_at": "2026-09-28T05:00:00.078400+00:00"},
         "limits": [{"kind": "session", "percent": 29, "severity": "normal",
                     "resets_at": "2026-09-26T11:10:00.078381+00:00", "is_active": False},
                    {"kind": "weekly_all", "percent": 33, "severity": "normal",
                     "resets_at": "2026-09-28T05:00:00.078400+00:00", "is_active": False}],
         "extra_usage": {"is_enabled": False}}


def usage(five, seven):
    """Тот же ответ с другой загрузкой окон (проценты)."""
    u = {k: dict(v) for k, v in USAGE.items() if isinstance(v, dict)}
    u["five_hour"]["utilization"], u["seven_day"]["utilization"] = five, seven
    return u


# Статус кредита claude из ручки usage (#283): те же три исхода, что у GLM.
# HYPOTHESIS: у профиля claude нет пробы, и реестр видит логин claude.ai
# только по провалу хода. SOLUTION: хуки probe/usage у claude.py, чистая
# status_of над ответом usage; 403 без области user:profile (setup-token) --
# не мёртвый кредит, а «статус неизвестен». STATUS: FIXED — see #283
CLAUDE_STATUS = [
    ("nothing exhausted -> active with the worst window",
     USAGE, CredStatus("active", percent=33, detail="5h 29%, weekly 33%")),
    ("5h window at 100 -> quota_wait until its reset",
     usage(100, 40), CredStatus("quota_wait", resets_at=1790421000, percent=100,
                                detail="5h window exhausted, weekly 40%")),
    ("both exhausted -> the earliest reset",
     usage(100, 100), CredStatus("quota_wait", resets_at=1790421000, percent=100,
                                 detail="5h window exhausted, weekly window exhausted")),
    ("403 without the profile scope -> active, status unknown (setup-token)",
     {"error": "HTTP 403", "body": {"type": "error", "error": {
         "type": "permission_error", "details": {"error_code": "oauth_scope_insufficient"}}}},
     CredStatus("active", detail="no profile scope: status unknown")),
    ("401 -> needs_login", {"error": "HTTP 401"}, CredStatus("needs_login", detail="HTTP 401")),
    ("403 for another reason -> needs_login",
     {"error": "HTTP 403", "body": {"error": {"type": "permission_error"}}},
     CredStatus("needs_login", detail="HTTP 403")),
    ("garbage -> needs_login, not a crash", None, CredStatus("needs_login", detail="no answer")),
    ("answer without windows -> needs_login", {"foo": 1},
     CredStatus("needs_login", detail="no usage windows in the answer")),
]



# ── #316: пробы через HTTP целиком, до и после общего _probe ───────────────
# Характеристика: тот же ответ сервера -> тот же CredStatus (вид, подробность,
# сброс, загрузка) и тот же запрос (адрес, заголовки, таймаут). urlopen
# подменён: ответ -- (код, тело) либо исключение сети.
def probe_through_http(mod, answer):
    """probe(mod) над подменённым urlopen -> (CredStatus, запрос)."""
    seen = {}

    class Body(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        seen.update(url=req.full_url, headers=dict(req.header_items()), timeout=timeout)
        if isinstance(answer, Exception):
            raise answer
        code, body = answer
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "refused", {}, io.BytesIO(body))
        return Body(body)
    with patched(urllib.request, urlopen=urlopen):
        return mod.probe("tok"), seen


def _json(obj):
    return json.dumps(obj).encode()


SCOPE_BODY = {"type": "error", "error": {"type": "permission_error",
                                         "details": {"error_code": "oauth_scope_insufficient"}}}
NEEDS = lambda detail: CredStatus("needs_login", detail=detail)  # noqa: E731
# Ответы, одинаковые для обоих провайдеров, и что из них выходит у каждого.
HTTP_SHARED = [
    ("401", (401, _json({"error": {"type": "authentication_error"}})),
     NEEDS("HTTP 401"), NEEDS("HTTP 401")),
    ("403 for another reason", (403, _json({"error": {"type": "permission_error"}})),
     NEEDS("HTTP 403"), NEEDS("HTTP 403")),
    ("403 oauth_scope_insufficient", (403, _json(SCOPE_BODY)),
     CredStatus("active", detail="no profile scope: status unknown"), NEEDS("HTTP 403")),
    ("500 with a non-JSON body", (500, b"<html>"), NEEDS("HTTP 500"), NEEDS("HTTP 500")),
    ("URLError", urllib.error.URLError("down"),
     NEEDS("URLError: <urlopen error down>"), NEEDS("URLError: <urlopen error down>")),
    ("timeout", TimeoutError("timed out"),
     NEEDS("TimeoutError: timed out"), NEEDS("TimeoutError: timed out")),
    ("non-JSON body", (200, b"<html>"),
     NEEDS("JSONDecodeError: Expecting value: line 1 column 1 (char 0)"),
     NEEDS("JSONDecodeError: Expecting value: line 1 column 1 (char 0)")),
    ("non-dict body", (200, b"[1, 2]"), NEEDS("no answer"), NEEDS("no answer")),
]
HTTP_CLAUDE = [
    ("two windows", (200, _json(USAGE)),
     CredStatus("active", percent=33, detail="5h 29%, weekly 33%")),
    ("5h exhausted", (200, _json(usage(100, 40))),
     CredStatus("quota_wait", resets_at=1790421000, percent=100,
                detail="5h window exhausted, weekly 40%")),
    # Порядок окон у claude -- свой, исчерпанное вперёд не выходит.
    ("weekly exhausted", (200, _json(usage(40, 100))),
     CredStatus("quota_wait", resets_at=1790571600, percent=100,
                detail="5h 40%, weekly window exhausted")),
    ("both exhausted -> the nearest reset", (200, _json(usage(100, 100))),
     CredStatus("quota_wait", resets_at=1790421000, percent=100,
                detail="5h window exhausted, weekly window exhausted")),
    ("no windows", (200, _json({"foo": 1})), NEEDS("no usage windows in the answer")),
]
HTTP_GLM = [
    ("two windows", (200, _json(quota({(3, 5): (29, 71), (6, 1): (33, 67)}))),
     CredStatus("active", percent=33, detail="5h 29%, weekly 33%")),
    ("5h exhausted", (200, _json(QUOTA)),
     CredStatus("quota_wait", resets_at=1790425418, percent=100,
                detail="5h window exhausted, weekly 53%")),
    # У glm исчерпанное окно -- первым.
    ("weekly exhausted", (200, _json(quota({(3, 5): (40, 60), (6, 1): (100, 0)}))),
     CredStatus("quota_wait", resets_at=1790425424, percent=100,
                detail="weekly window exhausted, 5h 40%")),
    ("both exhausted -> the nearest reset",
     (200, _json(quota({(6, 1): (100, 0), (3, 5): (100, 0)}))),
     CredStatus("quota_wait", resets_at=1790425421, percent=100,
                detail="5h window exhausted, weekly window exhausted")),
    ("success false", (200, _json({"code": 401, "success": False, "msg": "invalid api key"})),
     NEEDS("invalid api key")),
]
HTTP_REQUEST = {
    "claude": {"url": "https://api.anthropic.com/api/oauth/usage", "timeout": 15,
               "headers": {"Authorization": "Bearer tok", "Anthropic-beta": "oauth-2025-04-20",
                           "Accept": "application/json"}},
    "glm": {"url": "https://api.z.ai/api/monitor/usage/quota/limit", "timeout": 15,
            "headers": {"Authorization": "Bearer tok", "Accept": "application/json",
                        "Accept-language": "en-US,en"}},
}


def _limits(*lims):
    return {"code": 200, "success": True, "data": {"limits": [
        {"unit": u, "number": n, "percentage": pct, "remaining": rem, "nextResetTime": at}
        for u, n, pct, rem, at in lims]}}


# Порядок строки glm при равенстве (исчерпанность и ранг одни) -- по тексту
# окна, как у прежней сортировки кортежей; пустой список окон -- active без
# подробности. Значения сняты со старого кода.
GLM_ORDER = [
    ("unknown windows tie -> by text",
     _limits((3, 9, 10, 90, 0), (3, 2, 20, 80, 0)),
     CredStatus("active", percent=20, detail="2x3 20%, 9x3 10%")),
    ("unknown windows both exhausted -> by text, the nearest reset",
     _limits((3, 9, 100, 0, 5000), (3, 2, 100, 0, 9000)),
     CredStatus("quota_wait", resets_at=5, percent=100,
                detail="2x3 window exhausted, 9x3 window exhausted")),
    ("one window twice -> by text", _limits((3, 5, 7, 1, 0), (3, 5, 30, 1, 0)),
     CredStatus("active", percent=30, detail="5h 30%, 5h 7%")),
    ("no limits -> active, nothing to say", _limits(),
     CredStatus("active", percent=0, detail="")),
]


def check_probe_http_316(c):
    for what, payload, want in GLM_ORDER:
        c.expect(f"glm.status_of: {what}", glm.status_of(payload), want)
    cases = {"claude": [(w, a, want) for w, a, want in HTTP_CLAUDE]
             + [(w, a, cl) for w, a, cl, _ in HTTP_SHARED],
             "glm": [(w, a, want) for w, a, want in HTTP_GLM]
             + [(w, a, gl) for w, a, _, gl in HTTP_SHARED]}
    for name, mod in (("claude", claude), ("glm", glm)):
        for what, answer, want in cases[name]:
            try:
                got, seen = probe_through_http(mod, answer)
            except Exception as e:
                got, seen = f"{type(e).__name__}: {e}", {}
            c.expect(f"{name}.probe over HTTP: {what}", got, want)
            c.expect(f"{name}.probe request: {what}", seen, HTTP_REQUEST[name])


def main():
    c = Checks()
    for what, mod, want in CASES:
        try:
            got = llm.contract("fake", mod)
        except RuntimeError:
            got = None
        c.expect(what, got, want)

    for name in ("claude", "glm"):
        c.check(f"registry must find profile {name}", isinstance(llm.get(name), dict))
    c.expect("get() of an unknown name must return None", llm.get("no-such"), None)
    try:
        llm.require("no-such")
        refused = False
    except RuntimeError:
        refused = True
    c.check("require() of an unknown name must refuse", refused)

    # Правило выбора профиля (#147): одно на все места, где его раньше
    # набирали руками. Умолчание берём из настройки, а не литералом: .env
    # инсталляции вправе его сменить.
    # STATUS: FIXED — see #147
    default = config.get("MOP_DEFAULT_LLM")
    other = next(p for p in llm.profiles() if p != default)
    resolve, of_meta = llm.resolve, llm.of_meta
    RULE = [
        ("explicit given -> explicit", lambda: resolve(other, default), other),
        ("no explicit, old exists -> old", lambda: resolve(None, other), other),
        ("no explicit, old deleted from registry -> default",
         lambda: resolve(None, "no-such"), default),
        ("nothing -> default", lambda: resolve(), default),
        ("meta without llm -> default", lambda: of_meta({}), default),
        ("meta with llm -> its profile, even a deleted one",
         lambda: of_meta({"llm": "no-such"}), "no-such"),
        ("no meta at all -> default", lambda: of_meta(None), default),
        # Пустая строка: места расходятся сегодня, и рефакторинг обязан это
        # сохранить. `x or default` (явный профиль в job_spec/add/code/master,
        # image.clear) и откат update превращают "" в умолчание; `meta.get(
        # "llm", default)` (строка ростера, старый профиль в update, recycle)
        # отдаёт "" как есть. Спека в итоге всё равно умолчание: job_spec
        # снова пропускает профиль через resolve.
        ("empty explicit -> default (job_spec, add, code, master, image.clear)",
         lambda: resolve(""), default),
        ("empty old -> default (update fallback)",
         lambda: resolve(None, ""), default),
        ("meta llm empty -> empty (roster row, update's old, recycle)",
         lambda: of_meta({"llm": ""}), ""),
    ]
    for what, run, want in RULE:
        try:
            got = run()
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        c.expect(what, got, want)

    for what, payload, want in STATUS:
        try:
            got = glm.status_of(payload)
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        c.expect(f"glm.status_of: {what}", got, want)
    for what, payload, want in CLAUDE_STATUS:
        try:
            got = claude.status_of(payload)
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        c.expect(f"claude.status_of: {what}", got, want)
    c.check("claude profile declares probe and no usage hook (#310)",
            callable(getattr(claude, "probe", None)) and not hasattr(claude, "usage"))
    c.expect("claude.reset_epoch: ISO with offset and microseconds -> seconds",
             claude.reset_epoch("2026-09-26T11:10:00.078381+00:00"), 1790421000)
    c.expect("claude.reset_epoch: garbage -> None", claude.reset_epoch("soon"), None)
    # host_of(env) звался только с ENV самого модуля (#316): адрес ручки --
    # константа из ANTHROPIC_BASE_URL профиля без пути.
    c.expect("glm.QUOTA_URL is the host of ANTHROPIC_BASE_URL without its path",
             glm.QUOTA_URL, "https://api.z.ai/api/monitor/usage/quota/limit")
    got = llm.contract("fake", plugin(ENV={}, probe=lambda k: None, usage=lambda k, s, e: {}))
    c.check("contract names probe, and only probe (#310)",
            callable(got["probe"]) and "usage" not in got, got)
    c.check("the glm profile declares probe and no usage hook (#310)",
            callable(llm.get("glm")["probe"]) and "usage" not in llm.get("glm")
            and not hasattr(glm, "usage"))
    check_probe_http_316(c)
    return c.report("llm")


if __name__ == "__main__":
    sys.exit(main())
