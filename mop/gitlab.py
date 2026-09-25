"""Трекер проекта: issues GitLab.

Модуль возвращает данные и ничего не печатает — печатает `mop dev bug` (mop/cli/dev/bug).

Координаты берутся из ORIGIN рабочей копии, а не из настройки: проект в
трекере и проект в git — это один проект, и записывать его вторым местом
значит завести источник правды, который однажды разойдётся с первым. Тот же
довод, по которому проект выводится из origin, а не задаётся. `.env`
переопределяет (`GITLAB_SITE`, `GITLAB_PROJECT`) — например, когда трекер
живёт не там, где код.

Секреты (`GITLAB_TOKEN` либо `GITLAB_USER` с `GITLAB_PASSWORD`) читаются из
`.env` через config.get, но в `config.SETTINGS` не объявлены намеренно: список настроек
уезжает в ansible через --extra-vars и печатается `mop server config`, а паролю не
место ни там, ни там.
"""
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import config, driver

TIMEOUT = 20

# Словарь меток. Три группы — scoped labels GitLab: у задачи ровно одна из
# каждой, вторая молча вытесняет первую. Значения проверяются здесь, а не в
# GitLab: опечатка в метке заводит новую метку, и задача пропадает из выборки.
VOCAB = {
    "status": ("live", "wip", "fixed", "noise", "dup", "parked"),
    "sev": ("high", "med", "low"),
    "component": ("bus", "nomad", "agent", "session", "puppets", "driver",
                  "deploy", "mcp", "master", "cli", "docs"),
    "type": ("bug", "feature", "refactor", "docs", "ci", "epic"),
}
# Незакрытые состояния: задача в очереди (live), взята в работу (wip) либо
# отложена (parked) — отложенная открыта, но не в работе; закрытая отложенная
# пропадает из `mop dev bug list` и выглядит сделанной (#202).
OPEN_STATUSES = ("live", "wip", "parked")
CLOSE_STATUSES = ("fixed", "noise", "dup")
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


def _parse_origin(url):
    """origin -> (хост, путь проекта). Пусто, если это не похоже на origin.

    Разбор общий (driver.parse_origin, #154): scp, ssh:// с портом, https.
    Порт и схема отбрасываются — они не часть пути проекта. Без хоста или
    пути (локальный репозиторий) координат GitLab нет."""
    _, _, host, _, path = driver.parse_origin(url) or (None, None, "", None, "")
    return (host, path) if host and path else ("", "")


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

    Отказ громкий и с именем переменной: без него первый же запрос вернёт 401,
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
    """Запрос к API проекта, ответ — разобранный JSON."""
    raw = request(method, base() + path, path, payload, params)
    return json.loads(raw) if raw else None


def call_site(method, path, payload=None, params=None):
    """Запрос к API инстанса, мимо проекта: /runners/<id> у GitLab не под
    проектом, хотя раннер проекту и назначен."""
    raw = request(method, f"https://{site()}/api/v4" + path, path, payload, params)
    return json.loads(raw) if raw else None


def request(method, url, path, payload=None, params=None):
    """Запрос как есть -> тело байтами. Трасса джобы — текст, не JSON (#233).
    path — чем назвать запрос в отказе: полный URL в нём был бы шумом."""
    url += ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None)
    req.add_header(*auth_header())
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
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
    """Задачи как данные, тяжёлые первыми: сначала sev, потом номер."""
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


# ─── пайплайны ───────────────────────────────────────────────────────────
# Ещё идущие: у GitLab это и created/pending/running, и промежуточные
# waiting_for_resource/preparing — ответ «подожди», а не «красный».
PIPELINE_WAIT = ("created", "waiting_for_resource", "preparing", "pending", "running")


def has_credentials():
    """Есть ли в .env чем представиться GitLab — тот же выбор, что auth_header."""
    return bool(config.get("GITLAB_TOKEN")
                or config.get("GITLAB_USER") and config.get("GITLAB_PASSWORD"))


def pipeline(sha):
    """Последний пайплайн коммита -> {status, web_url, ...} либо None."""
    got = call("GET", "/pipelines", params={"sha": sha, "order_by": "id",
                                            "sort": "desc", "per_page": 1})
    return got[0] if got else None


# Стадия, в которой CI катит сам себя (#239): пока её джоба идёт, пайплайн
# running, и о зелёности коммита говорят только джобы остальных стадий.
DEPLOY_STAGE = "deploy"
# Начало ответа «жди». Одно место: по нему `mop server deploy --from-ci` отличает
# «коммит ещё тестируется» от прочих отказов (#239).
WAIT = "wait for pipeline "


def pipeline_verdict(pipeline, sha, jobs=None):
    """Можно ли катить коммит с таким пайплайном (#231). -> None либо отказ.

    Катить можно success. Неизвестный статус — отказ с его именем:
    отменённый или пропущенный пайплайн ничего не говорит о том, зелёный ли
    коммит, а молча принятый новый статус GitLab открыл бы дорогу красному.

    Идущий пайплайн с джобами (#239): его катит его же джоба deploy, и без
    этого гейт отвечал бы ей «жди» до конца её самой. Проходит, если джобы
    вне стадии deploy есть и каждая success (терпимый провал цвет не решает);
    провал вне deploy — отказ с именем джобы, ждать там нечего. Иначе — «жди»."""
    if pipeline is None:
        return f"no pipeline for {sha}"
    status, url = pipeline.get("status"), pipeline.get("web_url")
    if status == "success":
        return None
    if status == "failed":
        return f"pipeline failed for {sha[:12]}: {url}"
    if status in PIPELINE_WAIT:
        tests = [j for j in jobs or [] if j.get("stage") != DEPLOY_STAGE]
        broken = next((j for j in tests if is_failure(j)), None)
        if broken:
            return (f"pipeline {url} is {status} but job {broken['id']} "
                    f"{broken.get('stage')}/{broken.get('name')} failed: refusing")
        if tests and all(j.get("status") == "success" or j.get("allow_failure")
                         and j.get("status") == "failed" for j in tests):
            return None
        return f"{WAIT}{url} ({status})"
    return f"pipeline {url} is {status}, not success: refusing"


# ─── ci: пайплайны, джобы, раннеры (#233) ───────────────────────────────
# Порт bin/ci из rugent: раньше «почему красный» здесь выяснялось разовым
# скриптом к API, мимо mop — ровно тот обход, который CLAUDE.md запрещает.
def pipelines(n=15):
    """Последние n пайплайнов проекта, новые первыми."""
    return call("GET", "/pipelines", params={"order_by": "id", "sort": "desc",
                                             "per_page": n})


def pipeline_by_id(pid):
    """Пайплайн целиком: yaml_errors есть только здесь, не в списке."""
    return call("GET", f"/pipelines/{int(pid)}")


def jobs(pid):
    """Джобы пайплайна в порядке стадий. Перезапущенные копии GitLab сам
    прячет: показывается последняя попытка, та, что решает цвет."""
    return sorted(_paged(f"/pipelines/{int(pid)}/jobs", {}), key=lambda j: j["id"])


def job(jid):
    return call("GET", f"/jobs/{int(jid)}")


def trace(jid):
    """Лог джобы текстом, как его показал бы терминал: у GitLab это не
    JSON, а сырой вывод раннера с \r секций."""
    raw = request("GET", base() + f"/jobs/{int(jid)}/trace", f"/jobs/{int(jid)}/trace")
    return screen_lines(raw.decode(errors="replace")) if raw else ""


def lint(content):
    """POST /ci/lint проекта: include в .gitlab-ci.yml разрешаются от него."""
    return call("POST", "/ci/lint", {"content": content, "include_jobs": True})


def retry_job(jid):
    return call("POST", f"/jobs/{int(jid)}/retry")


def retry_pipeline(pid):
    return call("POST", f"/pipelines/{int(pid)}/retry")


def cancel_pipeline(pid):
    return call("POST", f"/pipelines/{int(pid)}/cancel")


def runners():
    """Раннеры проекта с метками. Список метки не несёт — только карточка
    раннера, а она у GitLab не под проектом."""
    return [call_site("GET", f"/runners/{int(r['id'])}")
            for r in _paged("/runners", {})]


def running_jobs():
    """Идущие сейчас джобы проекта, каждая — с раннером, который её держит."""
    return _paged("/jobs", {"scope[]": "running"})


def children(epic_iid):
    """Задачи эпика: те, у кого в первой строке тела стоит его маркер.

    Выборкой по телу, а не по связям GitLab: связи — платная редакция, а
    маркер виден и в вебе, и в выводе `mop dev bug show`."""
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
    """Родительский эпик задачи либо None. Считается только первая строка:
    ссылка на эпик где-то в теле — это ссылка, а не родство."""
    first = (body or "").lstrip().splitlines()[:1]
    m = _EPIC.match(first[0]) if first else None
    return int(m.group(1)) if m else None


def with_epic(body, iid):
    """Тело задачи с маркером эпика первой строкой. Идемпотентно, и старый
    маркер заменяется: два маркера означали бы двух родителей, оба настоящих."""
    body = (body or "").lstrip()
    if epic_of(body) is not None:
        rest = body.splitlines()[1:]
        body = "\n".join(rest).lstrip("\n")
    marker = f"**Эпик:** #{iid}"
    return f"{marker}\n\n{body}" if body else marker


def slugify(s):
    """ASCII-слаг для имени ветки: не-ASCII выбрасывается, не транслитерируется
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


def relabel_action(state, status):
    """Чем `mop dev bug relabel` меняет задачу: "reopen" либо "relabel".

    Открытый статус на закрытой задаче открывает её — иначе задача с меткой
    очереди молча сидит среди закрытых и пропадает из `mop dev bug list` (#202).
    Закрывать relabel не умеет: это `mop dev bug close`, с комментарием."""
    return "reopen" if status in OPEN_STATUSES and state == "closed" else "relabel"


# ─── ci: чистое (tests/ci.py, #233) ──────────────────────────────────────
# Метка — слово, а не значок: вывод читает модель через MCP, а у неё нет
# глаз, чтобы отличить зелёную галку от красного крестика. Провал — капсом:
# его и ищут в списке.
MARKS = {"success": "ok", "failed": "FAILED"}
# Законченные: отменять нечего. GitLab на отмену такого отвечает успехом и
# не делает ничего — молчаливое «сделано» хуже отказа.
PIPELINE_DONE = ("success", "failed", "canceled", "skipped")


def mark(status):
    return MARKS.get(status, status or "?")


def duration(secs):
    """Секунды GitLab -> "-" / "Ns" / "NmSSs". null — джоба не стартовала,
    и ноль там был бы враньём."""
    if secs is None:
        return "-"
    secs = int(secs)
    return f"{secs}s" if secs < 60 else f"{secs // 60}m{secs % 60:02d}s"


def tail(text, n):
    """Последние n строк; None — всё. Перевод строки в конце трассы строкой
    не считается: иначе «последние 60» были бы 59."""
    lines = (text or "").rstrip("\n").splitlines()
    return "\n".join(lines if n is None else lines[-n:] if n > 0 else [])


def screen_lines(text):
    """Трасса как её видит терминал: от строки — то, что после последнего \r.
    GitLab пишет секции как «section_start:<t>:<имя>\r\x1b[0K<текст>»,
    прогресс — через \r; без этого хвост лога — мусор маркеров. \r\n —
    просто конец строки. Цвета остаются: их снимает тот, кто печатает."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in lines)


def is_failure(job):
    """Провал, который валит пайплайн: failed без allow_failure. Терпимый
    провал цвет не решает, и «почему красный» не должен звать его причиной."""
    return job.get("status") == "failed" and not job.get("allow_failure")


def _wait_refusal(kind, obj):
    status = obj.get("status")
    if status in PIPELINE_WAIT:
        return f"{kind} {obj.get('id')} is {status}: wait for it to finish before retrying"
    return None


def retry_blocked(job):
    """Отказ повтора идущей джобы либо None. GitLab ответил бы 403 без
    объяснения, и искать пришлось бы по коду ответа."""
    return _wait_refusal("job", job)


def pipeline_retry_blocked(p):
    return _wait_refusal("pipeline", p)


def pipeline_cancel_blocked(p):
    """Отказ отмены законченного пайплайна либо None: GitLab принял бы её
    молча и ничего не сделал."""
    status = p.get("status")
    if status in PIPELINE_DONE:
        return f"pipeline {p.get('id')} is {status}: nothing to cancel"
    return None


def pipeline_failure(p):
    """Причина провала от самого пайплайна -> [строка]. Отвергнутый
    .gitlab-ci.yml валит пайплайн без единой джобы (#230): смотреть на
    джобы тогда бесполезно, причина лежит в пайплайне."""
    out = []
    if p.get("yaml_errors"):
        out.append(f"yaml_errors: {p['yaml_errors']}")
    if p.get("failure_reason"):
        out.append(f"failure_reason: {p['failure_reason']}")
    text = (p.get("detailed_status") or {}).get("text")
    if text:
        out.append(f"detailed_status: {text}")
    return out


def _when(stamp):
    """2026-09-24T10:11:12.345Z -> 2026-09-24 10:11 (UTC, как отдаёт GitLab)."""
    return stamp[:16].replace("T", " ") if stamp else "-"


def pipeline_line(p):
    return (mark(p.get("status")), str(p["id"]), (p.get("sha") or "")[:8] or "-",
            p.get("ref") or "-", _when(p.get("created_at")), p.get("web_url") or "-")


def job_line(j):
    """Строка джобы. Терпимый провал — не капсом: пайплайн он не валил."""
    allowed = j.get("status") == "failed" and j.get("allow_failure")
    return ("failed" if allowed else mark(j.get("status")), str(j["id"]),
            j.get("stage") or "-", j.get("name") or "-", duration(j.get("duration")),
            "(allow_failure)" if j.get("allow_failure") else "")


def runner_line(r):
    """ON/OFF — принимает ли раннер работу; status — видит ли его GitLab.
    Разные вещи: остановленный раннер бывает online, и джобы тогда стоят."""
    on = r.get("active", True) and not r.get("paused", False)
    return ("ON" if on else "OFF", str(r["id"]), r.get("description") or "-",
            r.get("status") or "-", ",".join(r.get("tag_list") or []) or "-")


def running_line(j):
    runner = j.get("runner") or {}
    held = (f"runner {runner['id']} {runner.get('description') or ''}".rstrip()
            if runner.get("id") else "no runner")
    return (str(j["id"]), j.get("stage") or "-", j.get("name") or "-",
            j.get("ref") or "-", held)


def lint_report(result):
    """Ответ /ci/lint -> ([строка], годен ли)."""
    if result.get("valid"):
        names = [j.get("name", "?") for j in result.get("jobs") or []]
        lines = [f"valid — {len(names)} job(s): {', '.join(names)}"]
    else:
        lines = ["invalid"] + [f"error: {e}" for e in result.get("errors") or []]
    lines += [f"warning: {w}" for w in result.get("warnings") or []]
    return lines, bool(result.get("valid"))


def why_lines(p, jobs, tails):
    """Почему пайплайн такой, какой есть -> [строка].

    jobs — джобы пайплайна, tails — {id джобы: хвост трассы} провалившихся.
    Терпимые провалы названы отдельно и прямо: иначе их красный цвет в
    вебе принимают за причину. Без провалившихся джоб — причина самого
    пайплайна: отвергнутый .gitlab-ci.yml джоб не заводит вовсе (#230)."""
    status = p.get("status")
    out = [f"pipeline {p['id']} {status}: {p.get('web_url')}"]
    failed = [j for j in jobs if is_failure(j)]
    tolerated = [j for j in jobs if j.get("status") == "failed" and j.get("allow_failure")]
    for j in failed:
        out += ["", f"job {j['id']} {j.get('stage')}/{j.get('name')} failed "
                    f"({duration(j.get('duration'))}): {j.get('web_url')}"]
        text = tails.get(j["id"])
        out += text.splitlines() if text else ["(empty trace)"]
    for j in tolerated:
        out += ["", f"job {j['id']} {j.get('stage')}/{j.get('name')} failed "
                    f"(allow_failure): did NOT fail the pipeline"]
    if failed:
        return out
    if status == "failed":
        reasons = pipeline_failure(p)
        return out + (["no jobs failed; the pipeline itself says:"] + reasons if reasons
                      else ["no jobs failed, and GitLab gives no reason"])
    return out if tolerated else out + ["nothing failed"]
