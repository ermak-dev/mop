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
import json
import os
import shutil
import subprocess
import threading
import time

from ..common import credreg, fsutil, llm, paths
from ..common.domain import CredStatus
from . import credlogin

ROOT = paths.local("creds")
RECORD = "cred.json"
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
        r = subprocess.run(["claude", "-p", KEEPALIVE_PROMPT, "--model", KEEPALIVE_MODEL],
                           capture_output=True, text=True, timeout=KEEPALIVE_TIMEOUT, env=env)
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
def login_start(name, mode="login"):
    """Начать логин: клиент в pty, -> адрес авторизации. Прежний
    незавершённый логин того же имени снимается."""
    credreg.check_name(name)
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
