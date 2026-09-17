"""Трекер проекта: issues GitLab.

Модуль ВОЗВРАЩАЕТ ДАННЫЕ и ничего не печатает — печатает `bin/bug`.

Координаты берутся из ORIGIN РАБОЧЕЙ КОПИИ, а не из настройки: проект в
трекере и проект в git — это один проект, и записывать его вторым местом
значит завести источник правды, который однажды разойдётся с первым. Тот же
довод, по которому шард выводится из origin, а не задаётся. `.env`
переопределяет (`GITLAB_SITE`, `GITLAB_PROJECT`) — например, когда трекер
живёт не там, где код.

Секреты (`GITLAB_TOKEN` либо `GITLAB_USER` с `GITLAB_PASSWORD`) читаются из
`.env` через config.get, но в `config.SETTINGS` НЕ объявлены намеренно: список настроек
уезжает в ansible через --extra-vars и печатается `mop config`, а паролю не
место ни там, ни там.
"""
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import config

TIMEOUT = 20

# Словарь меток. Три группы — scoped labels GitLab: у задачи ровно одна из
# каждой, вторая молча вытесняет первую. Значения ПРОВЕРЯЮТСЯ здесь, а не в
# GitLab: опечатка в метке заводит новую метку, и задача пропадает из выборки.
VOCAB = {
    "status": ("live", "wip", "fixed", "noise", "dup", "parked"),
    "sev": ("high", "med", "low"),
    "component": ("bus", "nomad", "agent", "session", "puppets", "driver",
                  "deploy", "mcp", "master", "cli", "docs"),
    "type": ("bug", "feature", "refactor", "docs", "ci", "epic"),
}
# Незакрытые состояния: задача в очереди (live) либо взята в работу (wip).
OPEN_STATUSES = ("live", "wip")
CLOSE_STATUSES = ("fixed", "noise", "dup", "parked")
SEV_ORDER = {"sev::high": 0, "sev::med": 1, "sev::low": 2}

TYPES = {"bug": "bug", "feature": "feat", "refactor": "refactor",
         "docs": "docs", "ci": "ci"}

_token = None


# ─── координаты ──────────────────────────────────────────────────────────
def origin():
    """Origin репозитория mop. Пусто — не рабочая копия."""
    try:
        return subprocess.run(["git", "-C", config.PROJECT, "remote", "get-url", "origin"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# Формы записи origin, которые надо понимать: scp-подобная
# (git@host:group/proj.git), со схемой и портом (ssh://git@host:2222/g/p.git)
# и https. Порт и схема отбрасываются — они не часть пути проекта; без этого
# в путь уезжало «2222/group/...», а из https-формы хостом становилось «https».
_ORIGIN = re.compile(r"""^(?:[a-z+]+://)?      # схема, если есть
                          (?:[^@/]+@)?         # пользователь
                          (?P<host>[^:/]+)     # хост
                          (?::\d+)?            # порт — только вместе со схемой
                          [:/](?P<path>.+?)(?:\.git)?/?$""", re.X)


def _parse_origin(url):
    """origin -> (хост, путь проекта). Пусто, если это не похоже на origin."""
    m = _ORIGIN.match(url or "")
    return (m.group("host"), m.group("path")) if m else ("", "")


def site():
    s = config.get("GITLAB_SITE") or _parse_origin(origin())[0]
    if not s:
        raise RuntimeError("no GitLab site: set GITLAB_SITE in .env or add a git origin")
    return s


def project():
    p = config.get("GITLAB_PROJECT") or _parse_origin(origin())[1]
    if not p:
        raise RuntimeError("no GitLab project: set GITLAB_PROJECT in .env or add a git origin")
    return p


def base():
    return f"https://{site()}/api/v4/projects/{urllib.parse.quote(project(), safe='')}"


# ─── запросы ─────────────────────────────────────────────────────────────
def auth_header():
    """Чем представляться GitLab. Личный токен (`GITLAB_TOKEN`) старше пары
    логин/пароль: password grant инстанс может и отключить, а у включённой
    двухфакторки он не работает вовсе."""
    pat = config.get("GITLAB_TOKEN")
    return ("PRIVATE-TOKEN", pat) if pat else ("Authorization", f"Bearer {token()}")


def token():
    """Короткоживущий OAuth-токен по логину и паролю из .env.

    Отказ ГРОМКИЙ и с именем переменной: без него первый же запрос вернёт 401,
    и разбираться придётся по HTTP-коду вместо одной строки."""
    global _token
    if _token is None:
        user, pw = config.get("GITLAB_USER"), config.get("GITLAB_PASSWORD")
        if not user or not pw:
            raise RuntimeError("no GitLab credentials in .env: GITLAB_TOKEN, "
                               "or GITLAB_USER and GITLAB_PASSWORD")
        body = urllib.parse.urlencode({"grant_type": "password", "username": user,
                                       "password": pw}).encode()
        try:
            with urllib.request.urlopen(f"https://{site()}/oauth/token", data=body,
                                        timeout=TIMEOUT) as r:
                _token = json.load(r)["access_token"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitLab auth failed: HTTP {e.code} "
                               f"{e.read().decode(errors='replace')[:200]}")
    return _token


def call(method, path, payload=None, params=None):
    url = base() + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None)
    req.add_header(*auth_header())
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: "
                           f"{e.read().decode(errors='replace')[:300]}")


def _paged(path, params):
    out, page = [], 1
    while True:
        batch = call("GET", path, params={**params, "per_page": 100, "page": page})
        out += batch
        if len(batch) < 100:
            return out
        page += 1


# ─── задачи ──────────────────────────────────────────────────────────────
def issues(state="opened", labels=(), search=None):
    """Задачи как ДАННЫЕ, тяжёлые первыми: сначала sev, потом номер."""
    params = {"state": state, "order_by": "created_at"}
    if labels:
        params["labels"] = ",".join(labels)
    if search:
        params["search"] = search
    return sorted(_paged("/issues", params), key=sev_key)


def issue(iid):
    return call("GET", f"/issues/{iid}")


def create(title, body, labels):
    return call("POST", "/issues", {"title": title, "description": body,
                                    "labels": ",".join(labels)})


def comment(iid, body):
    return call("POST", f"/issues/{iid}/notes", {"body": body})


def relabel(iid, labels):
    return call("PUT", f"/issues/{iid}", {"labels": ",".join(labels)})


def close(iid, labels):
    return call("PUT", f"/issues/{iid}", {"labels": ",".join(labels),
                                          "state_event": "close"})


def reopen(iid, labels):
    return call("PUT", f"/issues/{iid}", {"labels": ",".join(labels),
                                          "state_event": "reopen"})


def children(epic_iid):
    """Задачи эпика: те, у кого в первой строке тела стоит его маркер.

    Выборкой по телу, а не по связям GitLab: связи — платная редакция, а
    маркер виден и в вебе, и в выводе `mop bug show`."""
    return [i for i in issues(state="all") if epic_of(i.get("description")) == int(epic_iid)]


# ─── чистая логика (tests/gitlab.py) ─────────────────────────────────────
def sev_key(i):
    return (SEV_ORDER.get(scoped(i.get("labels") or [], "sev::"), 3), i["iid"])


def scoped(labels, prefix):
    return next((x for x in labels if x.startswith(prefix)), "")


def check_label(name):
    """Метка из словаря либо громкий отказ с перечнем. Опечатка в scoped-метке
    не ошибка для GitLab: он заведёт новую метку, и задача просто выпадет из
    выборок, которыми её ищут."""
    group, _, value = name.partition("::")
    if group not in VOCAB:
        raise RuntimeError(f"unknown label group {group}; one of: {', '.join(VOCAB)}")
    if value not in VOCAB[group]:
        raise RuntimeError(f"unknown {group} '{value}'; one of: {', '.join(VOCAB[group])}")
    return name


# Эпик — не сущность GitLab (она в платной редакции), а маркер первой строкой
# тела. Дёшево, видно человеку в вебе и не требует прав сверх обычной задачи.
_EPIC = re.compile(r"^\*\*Эпик:\*\*\s*#(\d+)\s*$", re.M)


def epic_of(body):
    """Родительский эпик задачи либо None. Считается ТОЛЬКО первая строка:
    ссылка на эпик где-то в теле — это ссылка, а не родство."""
    first = (body or "").lstrip().splitlines()[:1]
    m = _EPIC.match(first[0]) if first else None
    return int(m.group(1)) if m else None


def with_epic(body, iid):
    """Тело задачи с маркером эпика первой строкой. Идемпотентно, и СТАРЫЙ
    маркер заменяется: два маркера означали бы двух родителей, оба настоящих."""
    body = (body or "").lstrip()
    if epic_of(body) is not None:
        rest = body.splitlines()[1:]
        body = "\n".join(rest).lstrip("\n")
    marker = f"**Эпик:** #{iid}"
    return f"{marker}\n\n{body}" if body else marker


def slugify(s):
    """ASCII-слаг для имени ветки: не-ASCII ВЫБРАСЫВАЕТСЯ, не транслитерируется
    — заголовки русские, а имена веток остаются английскими."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40].rstrip("-")


def branch_type(labels):
    """Префикс ветки по меткам задачи. Дефект перевешивает: задача не должна
    нести и `bug`, и тип, но если несёт — правда о работе в дефекте."""
    if "bug" in labels or "type::bug" in labels:
        return "bug"
    for kind, prefix in TYPES.items():
        if kind in labels or f"type::{kind}" in labels:
            return prefix
    return "feat"


def branch_name(labels, iid, slug=None):
    clean = slugify(slug) if slug else ""
    return f"{branch_type(labels)}/{iid}" + (f"-{clean}" if clean else "")


def with_status(existing, status):
    """Метки после смены состояния: снять любую `status::` и поставить одну.

    Идемпотентно и чисто — переход проверяется без GitLab. Двух меток одной
    группы быть не должно: GitLab молча оставит одну, и какую — не скажет."""
    allowed = OPEN_STATUSES + CLOSE_STATUSES
    if status not in allowed:
        raise RuntimeError(f"unknown status '{status}'; one of: {', '.join(allowed)}")
    return [x for x in existing if not x.startswith("status::")] + [f"status::{status}"]
