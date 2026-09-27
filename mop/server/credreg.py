"""Реестр кредитов на сервере: дома, пробы, продление, логины (#283).

Дом кредита -- `~/.config/mop/creds/<имя>/` (0700): `cred.json` (запись,
mop/common/credreg.py) и рядом секрет по виду кредита:

    login   дом клиента claude: `.claude/.credentials.json` кладёт
            `claude auth login` (#282); секрет -- access-токен из него
    token   файл `token` -- `claude setup-token`, на год, только inference
    key     файл `key` -- ключ API провайдера (GLM)

Секрет наружу (по шине, в строки списка) не выходит: secret() зовут только
проба и раздача на сервере. Проба -- хук `probe` профиля (llm.<профиль>),
записывается в `cred.json` вместе со временем. Продление (keepalive):
claude обновляет токен сам при запросе, поэтому за два часа до истечения
сервер зовёт `claude -p` в доме кредита -- один копеечный запрос, и файл
переписан; сами обмен и client id остаются внутри клиента.

Логин -- диалог с клиентом в pty (mop/server/credlogin.py): start даёт
адрес, code вводит код. Незавершённые логины держатся в памяти сервиса по
имени кредита и умирают по TTL драйвера.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time

from ..common import bus, credreg, fsutil, llm, paths, puppets
from ..common.domain import CredStatus, JobMeta
from . import credlogin, nomad

ROOT = paths.local("creds")
RECORD = "cred.json"
WRITE_TIMEOUT = 60           # раздача: агент пишет в узел и в каждое тело (#137)
STATES_TIMEOUT = 20
TICK = 300                   # цикл сервиса (#284): пробы, продление, раздача
KEEPALIVE_PROMPT = "reply with one word: ok"
KEEPALIVE_MODEL = "haiku"
KEEPALIVE_TIMEOUT = 120

_logins, _logins_lock = {}, threading.Lock()


def home(name):
    credreg.check_name(name)
    return os.path.join(ROOT, name)


def _record_path(name):
    return os.path.join(home(name), RECORD)


def load(name):
    """Запись кредита либо None, если дома или записи нет."""
    try:
        with open(_record_path(name)) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("name") == name else None


def save(rec):
    fsutil.make_private_dir(home(rec["name"]))
    fsutil.write_private(_record_path(rec["name"]), json.dumps(rec, ensure_ascii=False))
    return rec


def all():
    """Все записи, по имени. Дом без записи (логин, начатый до #283, или
    оборванный) -- запись без пробы вида login, чтобы он не пропал из виду."""
    out = []
    try:
        names = sorted(os.listdir(ROOT))
    except OSError:
        return out
    for name in names:
        if not os.path.isdir(os.path.join(ROOT, name)):
            continue
        try:
            credreg.check_name(name)
        except ValueError:
            continue
        rec = load(name)
        if rec is None:
            kind = "token" if os.path.exists(os.path.join(ROOT, name, "token")) else "login"
            rec = credreg.record(name, "claude", kind,
                                 now=os.path.getmtime(os.path.join(ROOT, name)))
        out.append(rec)
    return out


def add_key(name, profile, key, owner="", now=None):
    """Ключ провайдера как кредит вида key. Профиль обязан быть в реестре
    и ждать ключ (KEY): ключ профилю без ключа -- ошибка в аргументах."""
    prof = llm.require(profile)
    if not prof.get("key"):
        raise ValueError(f"profile {profile} takes no key")
    key = (key or "").strip()
    if not key or "\n" in key:
        raise ValueError("key: one non-empty line")
    if load(name) is not None:
        raise ValueError(f"credential {name} exists — mop cred rm first")
    rec = credreg.record(name, profile, "key", owner=owner, now=now or time.time())
    fsutil.make_private_dir(home(name))
    fsutil.write_private(os.path.join(home(name), "key"), key + "\n")
    return save(rec)


def register_login(name, profile="claude", kind="login", owner="", now=None):
    """Запись для дома, который наполнил логин (#282): сам логин записи не
    пишет, её кладёт тот, кто его вёл."""
    rec = load(name) or credreg.record(name, profile, kind, owner=owner,
                                       now=now or time.time())
    if owner:
        rec = {**rec, "owner": owner}
    return save(rec)


def remove(name):
    """Снять кредит целиком: дом с секретом. -> True, если было что снимать."""
    path = home(name)
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path)
    return True


def credentials(name):
    """Разобранный .credentials.json дома логина либо {}."""
    try:
        with open(credlogin.credentials_path(home(name))) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def secret(name, rec=None):
    """Секрет кредита: access-токен, токен или ключ; None, если его нет.
    Только серверу -- по шине не отдаётся никогда."""
    rec = rec or load(name)
    if rec is None:
        return None
    kind = rec.get("kind")
    if kind == "login":
        return (credentials(name).get("claudeAiOauth") or {}).get("accessToken")
    fname = "token" if kind == "token" else "key"
    try:
        with open(os.path.join(home(name), fname)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def expires_at(name, rec=None):
    """Срок access-токена дома логина; у ключа и токена срока нет."""
    rec = rec or load(name)
    if not rec or rec.get("kind") != "login":
        return None
    return credreg.expires_at(credentials(name))


def probe(name, now=None):
    """Спросить провайдера о кредите и записать ответ. -> запись."""
    now = now or time.time()
    rec = load(name)
    if rec is None:
        raise ValueError(f"no credential {name}")
    prof = llm.get(rec.get("profile"))
    fn = (prof or {}).get("probe")
    sec = secret(name, rec)
    if sec is None:
        status = CredStatus("needs_login", detail="no secret in the credential's home")
    elif fn is None:
        status = CredStatus("active", detail=f"profile {rec.get('profile')} has no probe")
    else:
        exp = expires_at(name, rec)
        if exp is not None and exp < now and rec.get("kind") == "login":
            # Истёкший access-токен: проба ушла бы с ним и вернула 401;
            # сперва продление, потом проба свежим.
            keepalive(name, rec, force=True)
            sec = secret(name, rec) or sec
        status = fn(sec)
    return save(credreg.merge_status(rec, status, now))


def probe_all(now=None):
    return [probe(r["name"], now) for r in all() if load(r["name"]) is not None]


def keepalive(name, rec=None, now=None, force=False):
    """Продлить токен дома логина, если пора: `claude -p` в этом доме.
    -> "refreshed" | "not due" | "not a login" | "failed: …"."""
    rec = rec or load(name)
    if not rec or rec.get("kind") != "login":
        return "not a login"
    exp = expires_at(name, rec)
    if not force and not credreg.needs_keepalive(exp, now or time.time()):
        return "not due"
    env = credlogin._env(home(name))
    try:
        # Клиент -- абсолютным путём, найденным один раз на все вызовы (#292).
        r = subprocess.run([credlogin.client(), "-p", KEEPALIVE_PROMPT, "--model", KEEPALIVE_MODEL],
                           capture_output=True, text=True, timeout=KEEPALIVE_TIMEOUT, env=env)
    except RuntimeError as e:
        return f"failed: {e}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"failed: {type(e).__name__}"
    if r.returncode != 0:
        return "failed: " + credlogin.mask((r.stderr or r.stdout).strip())[-200:]
    new = expires_at(name, rec)
    return "refreshed" if new and (exp is None or new > exp) else "not refreshed"


def keepalive_all(now=None):
    """Обход домов логинов: {имя: исход}."""
    return {r["name"]: keepalive(r["name"], r, now) for r in all() if r.get("kind") == "login"}


# ─── логин через шину (#283): диалог с клиентом по имени кредита ──────────
MODE_OF_KIND = {"login": "login", "token": "setup-token"}


def login_mode(rec, asked):
    """Режим входа -> (режим, None) либо (None, отказ). Чистая функция.

    Есть запись -- режим решает её вид (#339): вход другого режима сообщал
    успех, а register_login сохранял прежний вид, и новый секрет лежал мимо
    secret(), продления и раздачи. Записи нет (новый кредит `mop cred login`)
    -- спрошенный режим, по умолчанию login."""
    if rec is None:
        return asked or "login", None
    kind = rec.get("kind")
    mode = MODE_OF_KIND.get(kind)
    if mode is None:
        return None, f"credential {rec.get('name')} is a {kind}: it has no claude login"
    if asked and asked != mode:
        return None, f"credential {rec.get('name')} is a {kind}: log it in with {mode}"
    return mode, None


def login_start(name, mode=None):
    """Начать логин: клиент в pty, -> адрес авторизации. Прежний
    незавершённый логин того же имени снимается.

    Только для кредита из реестра (#295): этим путём ходят страница и глагол
    cred_login_start, а они переавторизуют, не добавляют (решение 27.09,
    #294). Без сверки удачный код заводил бы новый кредит через
    register_login -- любой в LAN мог бы подложить пулу свой аккаунт, -- а
    брошенный вход оставлял бы дом строкой «не проверялся». Новый кредит
    claude заводит `mop cred login` на сервере: он ведёт драйвер сам."""
    credreg.check_name(name)
    rec = load(name)
    if rec is None:
        raise RuntimeError(f"no such credential {name}: a new claude credential is added "
                           f"with mop cred login on the server")
    # Профиль сверяет сервер (#330): кнопка страницы -- её собственное
    # правило, а глагол и /api/creds/login/start принимают любое имя.
    if rec.get("profile") != "claude":
        raise RuntimeError(f"credential {name} is a {rec.get('profile')} credential: "
                           f"only claude credentials log in")
    mode, refusal = login_mode(rec, mode)
    if refusal:
        raise RuntimeError(refusal)
    with _logins_lock:
        old = _logins.pop(name, None)
    if old:
        old.close()
    login = credlogin.Login.start(home(name), mode)
    with _logins_lock:
        _logins[name] = login
    return login.url


def login_code(name, code, owner=""):
    """Ввести код логина. -> {"ok": True, "owner": почта|""} либо {"error"}."""
    with _logins_lock:
        login = _logins.pop(name, None)
    if login is None:
        return {"error": f"no login in progress for {name} — start it again"}
    if login.expired():
        login.close()
        return {"error": f"login for {name} expired — start it again"}
    if not login.submit(code):
        return {"error": login.error or "login failed"}
    if login.mode == "setup-token":
        fsutil.make_private_dir(home(name))
        fsutil.write_private(os.path.join(home(name), "token"), login.result + "\n")
        rec = register_login(name, kind="token", owner=owner)
        return {"ok": True, "owner": rec.get("owner") or ""}
    status = credlogin.auth_status(home(name))
    who = owner or status.get("email") or ""
    rec = register_login(name, kind="login", owner=who)
    return {"ok": True, "owner": rec.get("owner") or "",
            "subscription": status.get("subscriptionType") or ""}


def login_forget_expired():
    """Убрать логины старше TTL: клиент в pty не должен жить вечно."""
    with _logins_lock:
        gone = [n for n, l in _logins.items() if l.expired()]
        for n in gone:
            _logins.pop(n).close()
    return gone


# ─── аренда (#284): что уезжает в тело и кому ────────────────────────────
# Тело работает кредитом, а не логином оператора: в него уезжает access-токен
# без refresh (обновляет сервер в доме кредита) и метка `.local/state/mop/cred`
# с именем кредита -- по ней хук приписывает провал хода кредиту.
#
# Ограничение этого шага: глагол `write` кладёт файлы в узел и во ВСЕ его
# живые тела, поэтому все тела узла получают один и тот же файл. Двум
# кредитам одного профиля на одном узле пока не жить; адресная запись в
# одно тело -- следующий шаг (глагол write с именем тела).
def materialize(name, rec=None):
    """Файлы кредита для тела -> [(относительное имя, байты)]. ValueError,
    если нести нечего: setup-token нужен окружением при старте claude и
    этим шагом не раздаётся."""
    rec = rec or load(name)
    if rec is None:
        raise ValueError(f"no credential {name}")
    mark = (paths.CRED_MARK, (name + "\n").encode())
    kind = rec.get("kind")
    if kind == "login":
        creds = credentials(name)
        if not creds:
            raise ValueError(f"credential {name}: no credentials file in its home")
        return [(paths.CREDENTIALS, json.dumps(credreg.without_refresh(creds)).encode()), mark]
    if kind == "key":
        var = (llm.require(rec.get("profile")) or {}).get("key")
        key = secret(name, rec)
        if not var or not key:
            raise ValueError(f"credential {name}: no key to distribute")
        # Ключ уезжает в secrets.env целиком: на узле с двумя ключевыми
        # профилями последняя раздача побеждает (ограничение выше).
        return [(paths.NODE_SECRETS, credreg.secrets_line(var, key).encode()), mark]
    raise ValueError(f"credential {name} is a {kind}: not distributable "
                     f"(a setup-token needs the environment at claude start)")


def files_sha(files):
    """Отпечаток раздачи: что именно уехало, чтобы не гонять то же самое."""
    h = hashlib.sha256()
    for path, data in files:
        h.update(path.encode() + b"\0" + data + b"\0")
    return h.hexdigest()


def holders(api=None):
    """Кто держит аренду: {кредит: {папет: узел|None}} по мете джобов."""
    api = api or nomad
    out = {}
    for job in api.get_jobs(puppets.JOB_PREFIX, meta=True):
        cred = JobMeta.from_job(job).cred
        if not cred:
            continue
        try:
            alloc = api.latest_alloc(job["ID"])
        except Exception:
            alloc = None
        out.setdefault(cred, {})[job["ID"]] = (alloc or {}).get("NodeName")
    return out


def _changed_at(name, rec):
    """mtime секрета кредита: менялся ли дом после провала хода."""
    kind = rec.get("kind")
    path = credlogin.credentials_path(home(name)) if kind == "login" \
        else os.path.join(home(name), "token" if kind == "token" else "key")
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def distribute(name, api=None, now=None):
    """Раздать кредит держателям: на каждый узел, где стоит папет с этой
    арендой, -- адресная запись (push) в тела ЕГО держателей (#312).
    -> {узел: "OK" | "FAILED: …" | "NOT REACHED: …"}; пусто -- держателей нет.
    Отпечаток раздачи -- в записи.

    Раньше `write` уходил узлу без тел, и агент клал кредит в копию узла и
    во все тела: держатель соседнего кредита на том же узле работал на чужом
    аккаунте. Копию узла раздача больше не пишет: новое тело держателя
    получает аренду на подъёме (cred_push из bootstrap)."""
    rec = load(name)
    if rec is None:
        raise ValueError(f"no credential {name}")
    files = materialize(name, rec)
    by_node = {}
    for puppet, node in sorted((holders(api).get(name) or {}).items()):
        if node:
            by_node.setdefault(node, []).append(puppet)
    # Не путь подъёма: таймаут раздачи прежний (#137), а не PUSH_TIMEOUT.
    out = {node: push(name, node, bodies, timeout=WRITE_TIMEOUT)
           for node, bodies in sorted(by_node.items())}
    if by_node:
        save({**rec, "pushed": {"sha": files_sha(files), "at": int(now or time.time()),
                               "nodes": out}})
    return out


# Адресная запись на пути подъёма (#312): короче раздачи -- её ждёт
# bootstrap, а он держит подъём папета (45 с на весь cred_push).
PUSH_TIMEOUT = 30


def push(name, node, bodies, timeout=PUSH_TIMEOUT):
    """Кредит name -- адресной записью (`bodies`, #312) в тела bodies на узле
    node. -> "OK" | "FAILED: …" | "NOT REACHED: …".

    Узел называет вызывающий, и только из своей правды (Nomad), не из
    чужого запроса. Отказ одного тела агент кладёт строкой в written, а не в
    error: без её разбора непришедшая аренда читалась бы OK."""
    try:
        files = materialize(name)
    except ValueError as e:
        return f"FAILED: {str(e)[:120]}"
    payload = [[p, base64.b64encode(d).decode()] for p, d in files]
    try:
        got = bus.request(node, "write", timeout=timeout, project=bus.ADMIN,
                          files=payload, bodies=list(bodies))
    except bus.BusError as e:
        return f"NOT REACHED: {e}"
    bad = bus.verdict(got)
    if bad:
        return (f"NOT REACHED: {bad[1] or 'no answer'}" if bad[0] == bus.UNREACHED
                else f"FAILED: {bad[1][:120]}")
    missed = [w for w in got.get("written") or []
              if " FAILED — " in w or w.endswith(" NOT LIVE")]
    return f"FAILED: {missed[0][:120]}" if missed else "OK"


def add_login_file(name, text, owner="", now=None):
    """Логин claude файлом кредов (то, что оператор раздаёт `mop login`) --
    кредит вида login: дом с `.claude/.credentials.json`, refresh-токен
    остаётся здесь. Существующий кредит того же имени обновляется файлом."""
    try:
        creds = json.loads(text)
        assert isinstance(creds, dict) and creds.get("claudeAiOauth")
    except (ValueError, AssertionError):
        raise ValueError("credentials: not a claude .credentials.json")
    rec = load(name)
    if rec is not None and rec.get("kind") != "login":
        raise ValueError(f"credential {name} is a {rec.get('kind')}, not a login")
    fsutil.make_private_dir(home(name))
    path = credlogin.credentials_path(home(name))
    fsutil.make_private_dir(os.path.dirname(path))
    fsutil.write_private(path, json.dumps(creds))
    return register_login(name, kind="login", owner=owner, now=now)


# ─── приписывание провалов и цикл сервиса (#284) ──────────────────────────
def note_turn(name, record, now=None, api=None):
    """Запись хода держателя кредита name -> что сделано строкой либо None.
    Логин протух, а дом с тех пор обновился -- раздать свежее, не хоронить.

    Имя кредита даёт аренда (держатели name, мета джобов), а не запись: метку
    `.local/state/mop/cred` кладёт глагол `write`, который доступен мастеру
    любого проекта и пишет во все тела узла (#308). Запись с другой меткой
    -- подсказка, которая разошлась с правдой: игнорируется и называется."""
    mark = (record or {}).get("cred")
    if not mark:
        return None
    if mark != name:
        return f"{name}: turn names {mark}, ignored"
    rec = load(name)
    if rec is None:
        return None
    if int(record.get("at") or 0) <= int(rec.get("noted_at") or 0):
        return None
    now = now or time.time()
    status = credreg.turn_status(record, _changed_at(name, rec))
    if status is not None:
        save({**credreg.merge_status(rec, status, now), "noted_at": int(record["at"])})
        return f"{name}: {status.kind} ({status.detail})"
    if record.get("error") == "authentication_failed":
        save({**rec, "noted_at": int(record["at"])})
        got = distribute(name, api, now)
        return f"{name}: re-distributed after a stale login: " + ", ".join(
            f"{n} {r}" for n, r in sorted(got.items()))
    return None


def turns_of(holding, api=None):
    """Записи ходов держателей: {папет: запись} через `states` агентов."""
    by_node = {}
    for puppet, node in holding.items():
        if node:
            by_node.setdefault(node, []).append(puppet)
    out = {}
    for node, names in by_node.items():
        try:
            got = bus.request(node, "states", timeout=STATES_TIMEOUT, project=bus.ADMIN,
                              names=names)
        except bus.BusError:
            continue
        for puppet, facts in (got.get("puppets") or {}).items():
            turn = ((facts or {}).get("state") or {}).get("turn")
            if isinstance(turn, dict):
                out[puppet] = turn
    return out


# Уже названные записи ходов с чужой меткой (#308): (кредит, папет, at).
# В памяти сервиса: в реестр кредита чужое не пишется.
_ignored = set()


def tick(api=None, now=None, log=None):
    """Один круг цикла сервиса -> [строки журнала]: снять протухшие логины,
    продлить токены, пробы, раздать изменившееся держателям, приписать
    провалы ходов кредитам."""
    now = now or time.time()
    lines = []
    for gone in login_forget_expired():
        lines.append(f"cred {gone}: login abandoned (ttl)")
    held = holders(api)
    for rec in all():
        name = rec["name"]
        if load(name) is None:
            continue
        try:
            if rec.get("kind") == "login":
                got = keepalive(name, now=now)
                if got not in ("not due", "not a login"):
                    lines.append(f"cred {name}: keepalive {got}")
            probe(name, now)
            if name in held:
                files = materialize(name)
                if files_sha(files) != (load(name).get("pushed") or {}).get("sha"):
                    got = distribute(name, api, now)
                    lines.append(f"cred {name}: distributed: " + ", ".join(
                        f"{n} {r}" for n, r in sorted(got.items())))
                for puppet, turn in turns_of(held[name], api).items():
                    got = note_turn(name, turn, now, api)
                    if got and turn.get("cred") != name:
                        # Запись хода живёт до следующего хода: чужую метку
                        # называем раз, а не каждый круг (#308).
                        seen = (name, puppet, turn.get("at"))
                        if seen in _ignored:
                            continue
                        _ignored.add(seen)
                    if got:
                        lines.append(f"cred {got} (from {puppet})")
        except Exception as e:
            lines.append(f"cred {name}: {credlogin.mask(str(e))[:160]}")
    return lines


def ticker(log, api=None, every=TICK):
    """Поток цикла: раз в every секунд, ошибки -- в журнал, не наружу."""
    while True:
        try:
            for line in tick(api):
                log(f"mop-cluster: {line}")
        except Exception as e:
            log(f"mop-cluster: cred tick failed: {credlogin.mask(str(e))[:160]}")
        time.sleep(every)
